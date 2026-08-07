"""Pernix — Composed internal recall (memory + cross-session FTS).

A single entry point that gathers persistent memory hits AND prior-session
hits for a query, formats them for tool output, and flags whether the
internal signal is strong enough to nudge the agent away from blindly
trusting external results.

Composes existing search primitives — no new search logic:
  - core.memory.store.MemoryStore.search()      (BM25 + temporal)
  - core.scout.search.gather_cross_session_data (FTS5 over message history,
                                                 with context expansion)

This is invoked at the moment of `search_web` to honor the user's intent:
"if we are going to do an external search, also search our memories and
sessions." Memory recall already happens at scout time into the system
prompt; this re-surfaces it at the point of decision so the agent can
weigh it against the live web result.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("pernix.memory.internal_recall")


@dataclass
class InternalRecall:
    """Bundle of internal-knowledge findings for a query.

    Fields:
      memory_text:        Formatted memory entries (BM25-ranked), "" if none.
      memory_seen_footer: Reference footer for memory entries that were
                          already surfaced earlier in this session
                          (per-session dedup ledger). "" if none.
      session_text:       Formatted cross-session hits, "" if none.
      memory_strong:      True if any memory entry scored > 3.0 (matches
                          RULES.md threshold — strong vs weak vs noise).
      session_strong:     True if any cross-session hit returned (any FTS
                          match against prior messages is signal worth
                          surfacing — bar is intentionally lower than memory).
      queried:            Always True when this object is returned (call
                          sites can distinguish "we asked" from "we skipped").
    """

    memory_text: str = ""
    memory_seen_footer: str = ""
    session_text: str = ""
    memory_strong: bool = False
    session_strong: bool = False
    queried: bool = False


# Score threshold matching the documentation in data/agent/RULES.md:
# "> 3.0 strong · 1.0–3.0 weak · < 1.0 noise"
# Scores are length-normalized (per query token) by search_bm25, so this
# absolute threshold holds across query lengths — un-normalized, a long
# query crossed 3.0 on summed token noise alone. Hybrid search keeps the
# same scale when embeddings are on (RRF orders, it does not score — see
# core.memory.search._rrf_fuse), so this comparison stays meaningful.
_MEMORY_STRONG_SCORE = 3.0

# Per-entry character cap when formatting memory hits. Mirrors the cap
# scout uses (scout_preload_memory_char_limit, default 300) so the agent
# sees consistent excerpt lengths whether it reads the scout baseline or
# this search_web augmentation.
_MEMORY_ENTRY_CHAR_CAP = 800


def internal_recall(
    query: str,
    current_session_id: str | None = None,
    memory_limit: int = 8,
) -> InternalRecall:
    """Run memory + cross-session recall for `query`. Failure-quiet.

    `current_session_id` is excluded from the cross-session search to
    avoid self-reference noise when the agent searches mid-turn.

    Never raises — on any backend error, returns a partially or fully
    empty InternalRecall and logs at DEBUG.
    """
    result = InternalRecall(queried=True)

    if not query or not query.strip():
        return result

    # --- Memory (BM25 + hybrid) ---
    try:
        from core.memory.store import get_memory_store

        store = get_memory_store()
        if store is not None:
            # Automated augmentation (fires on search_web), not a deliberate
            # agent recall — don't inflate usage hit counts.
            mem_results = store.search(query, mode="hybrid", limit=memory_limit, _track_hits=False)
            if mem_results:
                # Score signal is computed before dedup so a strong-but-seen entry
                # still nudges the agent ("[!] Strong internal match") even when
                # its body is collapsed to the footer reference.
                max_score = max((r.score for r in mem_results), default=0.0)
                result.memory_strong = max_score > _MEMORY_STRONG_SCORE

                from core.memory.dedup import partition_seen
                from core.memory.search import format_result_line

                new_results, _seen, footer = partition_seen(mem_results, current_session_id or "")
                result.memory_seen_footer = footer

                lines = [format_result_line(r, char_cap=_MEMORY_ENTRY_CHAR_CAP) for r in new_results]
                result.memory_text = "\n\n".join(lines)
    except Exception as e:
        logger.debug("Internal memory recall failed: %s", e)

    # --- Cross-session FTS ---
    try:
        from core.scout.search import gather_cross_session_data

        # gather_cross_session_data requires a session id string; pass empty
        # string for unattached callers (no session context to exclude).
        sid = current_session_id or ""
        session_text = gather_cross_session_data(query, sid)
        if session_text:
            result.session_text = session_text
            result.session_strong = True
    except Exception as e:
        logger.debug("Internal cross-session recall failed: %s", e)

    return result


def format_for_tool_output(recall: InternalRecall) -> str:
    """Format an InternalRecall as a model-facing text block.

    Returns "" when memory (including dedup footer) and sessions all came
    back empty so callers can skip prepending a useless header.
    """
    if not recall.memory_text and not recall.memory_seen_footer and not recall.session_text:
        return ""

    parts = ["=== INTERNAL KNOWLEDGE (memory + prior sessions) ==="]

    if recall.memory_text:
        parts.append("MEMORY:")
        parts.append(recall.memory_text)
        if recall.memory_seen_footer:
            parts.append(recall.memory_seen_footer)
    elif recall.memory_seen_footer:
        # All matching memory entries were already surfaced earlier in this
        # session — show the footer so the model knows they exist.
        parts.append("MEMORY:")
        parts.append(recall.memory_seen_footer)
    else:
        parts.append("MEMORY: no matching entries.")

    if recall.session_text:
        parts.append("")
        parts.append(recall.session_text)
    else:
        parts.append("")
        parts.append("PRIOR SESSIONS: no matching hits.")

    if recall.memory_strong or recall.session_strong:
        parts.append("")
        parts.append(
            "[!] Strong internal match — synthesize from the above before "
            "treating the external results below as the source of truth."
        )

    return "\n".join(parts)
