"""Pernix — Scout search: cross-session data + deep memory gathering.

Both functions return formatted strings for the scout LLM's input.
Empty string on miss — zero noise in context.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger("pernix.scout.search")

# Common English stopwords to filter from keyword decomposition
_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "are",
        "but",
        "not",
        "you",
        "all",
        "can",
        "had",
        "her",
        "was",
        "one",
        "our",
        "out",
        "has",
        "have",
        "been",
        "from",
        "this",
        "that",
        "with",
        "they",
        "will",
        "what",
        "when",
        "make",
        "like",
        "just",
        "over",
        "such",
        "take",
        "than",
        "them",
        "very",
        "some",
        "could",
        "into",
        "other",
        "then",
        "its",
        "also",
        "after",
        "how",
        "about",
        "which",
        "each",
        "she",
        "does",
        "these",
        "most",
        "would",
        "should",
        "there",
        "their",
        "where",
        "being",
        "still",
        "help",
        "please",
        "want",
        "need",
        "using",
        "used",
        "use",
    }
)


def _extract_keywords(text: str, max_keywords: int = 5) -> list[str]:
    """Extract the most specific keywords from a message for sub-queries."""
    clean = re.sub(r"[^\w\s]", " ", text.lower())
    words = [w for w in clean.split() if len(w) > 3 and w not in _STOPWORDS]
    # Deduplicate preserving order
    seen = set()
    unique = []
    for w in words:
        if w not in seen:
            seen.add(w)
            unique.append(w)
    return unique[:max_keywords]


def gather_cross_session_data(message: str, current_session_id: str) -> str:
    """Search other sessions for relevant findings via FTS5.

    Uses multi-query decomposition (like deep memory) to catch keyword
    mismatches, then expands top hits with surrounding context so the
    scout LLM can see the full conversation thread around a match.

    Returns formatted text block or "" if nothing found.
    """
    from db import models as db

    # --- Multi-query FTS search ---
    # Collect raw FTS hits from the main query + keyword sub-queries, dedup
    all_hits: dict[tuple, dict] = {}  # (session_id, content_prefix) → hit

    def _collect(results: list[dict]) -> None:
        for r in results:
            key = (r["session_id"], (r["content"] or "")[:80])
            if key not in all_hits or r["score"] > all_hits[key]["score"]:
                all_hits[key] = r

    try:
        _collect(
            db.search_messages_fts(
                message,
                limit=12,
                exclude_session=current_session_id,
            )
        )
    except Exception as e:
        logger.debug("Cross-session FTS search failed: %s", e)

    # Sub-queries from extracted keywords
    keywords = _extract_keywords(message, max_keywords=5)
    for kw in keywords[:3]:
        try:
            _collect(
                db.search_messages_fts(
                    kw,
                    limit=5,
                    exclude_session=current_session_id,
                )
            )
        except Exception:
            continue

    # --- Context expansion for top hits ---
    # For the top N hits, pull surrounding messages to show the full thread
    sorted_hits = sorted(all_hits.values(), key=lambda h: h["score"], reverse=True)

    results_by_session: dict[str, list[str]] = {}
    seen_context: set[tuple] = set()  # (session_id, msg_id) to avoid dupes
    budget = 3500
    used = 0

    for hit in sorted_hits[:8]:
        sid = hit["session_id"]
        title = hit["session_title"]
        key = f'{sid[:8]} "{title}"'

        # Get surrounding messages for context
        try:
            msg_id = hit.get("msg_id", 0)
            context_msgs = db.get_message_context(sid, msg_id, window=2) if msg_id else []
        except Exception:
            context_msgs = []

        # If no context available, use the hit itself
        if not context_msgs:
            content = hit["content"].strip()
            if not content:
                continue
            line = f"  [{hit['role']}] {content}"
            if used + len(line) > budget:
                break
            ctx_key = (sid, content[:80])
            if ctx_key not in seen_context:
                seen_context.add(ctx_key)
                results_by_session.setdefault(key, []).append(line)
                used += len(line)
        else:
            for cm in context_msgs:
                content = (cm["content"] or "")[:300].strip()
                if not content:
                    continue
                ctx_key = (sid, cm["id"])
                if ctx_key in seen_context:
                    continue
                seen_context.add(ctx_key)
                line = f"  [{cm['role']}] {content}"
                if used + len(line) > budget:
                    break
                results_by_session.setdefault(key, []).append(line)
                used += len(line)
        if used >= budget:
            break

    if not results_by_session:
        return ""

    # Format grouped by session
    lines = ["CROSS-SESSION FINDINGS:"]
    for session_key, entries in results_by_session.items():
        # Truncate long session keys for cleaner output
        display_key = session_key[:60] + "..." if len(session_key) > 60 else session_key
        lines.append(f"Session ({display_key}):")
        lines.extend(entries)
    return "\n".join(lines)


def gather_deep_memory(message: str) -> str:
    """Multi-query memory search for broader recall.

    Decomposes the message into keywords and runs parallel BM25 searches
    to catch related concepts the single shallow search misses.
    Returns formatted text block or "" if nothing found beyond shallow results.
    """
    try:
        from core.memory.store import get_memory_store

        store = get_memory_store()
        if not store:
            return ""
    except Exception:
        return ""

    # Main search with broad limit
    all_results = store.search(message, limit=12)

    # Keyword decomposition sub-queries
    keywords = _extract_keywords(message, max_keywords=5)
    for kw in keywords[:3]:
        try:
            sub = store.search(kw, mode="bm25", limit=5)
            all_results.extend(sub)
        except Exception:
            continue

    if not all_results:
        return ""

    # Dedup by (file_name, epoch), keep highest score
    seen: dict[tuple, object] = {}
    for r in all_results:
        key = (r.entry.file_name, r.entry.epoch)
        if key not in seen or r.score > seen[key].score:
            seen[key] = r

    # Sort by score descending
    deduped = sorted(seen.values(), key=lambda r: r.score, reverse=True)

    # Budget to 4000 chars
    lines = []
    total = 0
    for r in deduped:
        line = f"[{r.entry.file_name} score={r.score:.1f}] {r.entry.content[:400]}"
        if total + len(line) > 4000:
            break
        lines.append(line)
        total += len(line)

    return "\n\n".join(lines) if lines else ""
