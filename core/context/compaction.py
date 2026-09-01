"""Pernix — Append-only compaction. Never modifies stored messages.

Phase 1: View-based pruning (at assembly time, zero cost)
Phase 2: Orphan exclusion (at assembly time, zero cost)
Phase 3: LLM summarization (append-only, writes new compaction marker)
"""

from __future__ import annotations

import asyncio
import json
import logging

from config import settings
from core.context.tokens import get_estimator
from db import models as db

logger = logging.getLogger("pernix.context.compaction")

# Roles that are context-assembly markers rather than real conversation turns.
# Mirror compile_context's active-window filter so the boundary id we record
# lines up with the messages the compiler will keep after compaction.
_MARKER_ROLES = frozenset({"compaction", "scout", "notice", "reflect", "model_divider", "eval"})

COMPACTION_PROMPT = """Summarize this conversation. Output structured JSON followed by a prose paragraph:

```json
{
  "goal": "one sentence describing the overall task",
  "progress": ["completed item 1", "completed item 2"],
  "files_created": ["path/to/file1.html", "path/to/file2.py"],
  "decisions": [{"decision": "...", "rationale": "..."}],
  "active_context": ["ongoing preference or instruction"],
  "next_steps": ["what remains to be done"]
}
```

Then write a 2-3 sentence prose summary below the JSON block for natural reading.

RULES:
- Be concise. Every token matters.
- Preserve workspace file paths exactly.
- Capture key decisions with rationale.
- Do not include raw code or tool output in the summary.
"""

COMPACTION_UPDATE_PROMPT = """Update this existing conversation summary with new information.

EXISTING SUMMARY:
{existing_summary}

NEW CONVERSATION (since last summary):
{new_content}

Output an updated summary in the same JSON + prose format. Merge new information, don't repeat old details."""


# ---------------------------------------------------------------------------
# Phase 1: View-based pruning (applied at assembly time)
# ---------------------------------------------------------------------------


def apply_view_pruning(
    messages: list[dict],
    keep_recent: int | None = None,
    min_chars: int | None = None,
) -> list[dict]:
    """Prune old tool results in a VIEW — original messages unchanged.

    Returns a new list where old, large tool results are stubbed. Stubbed
    entries carry a private ``_view_pruned`` marker (stripped before the LLM
    sees them) so the compiler can count and surface what was dropped.
    This is a view transform for context assembly, NOT a DB mutation.
    The caller decides WHETHER to prune (budget pressure); this function
    only decides WHAT to prune.
    """
    if keep_recent is None:
        keep_recent = int(getattr(settings, "view_prune_keep_recent", 30))
    if min_chars is None:
        min_chars = int(getattr(settings, "view_prune_min_chars", 2000))
    if len(messages) <= keep_recent:
        return list(messages)

    cutoff = len(messages) - keep_recent
    result = []
    for i, msg in enumerate(messages):
        if i < cutoff and msg.get("role") == "tool":
            # `or ""` not a get() default: the column is nullable and a NULL
            # row returns None, which len() then raises on.
            content = msg.get("content") or ""
            if len(content) > min_chars:
                preview = content[:80].replace("\n", " ")
                stub = f"[pruned — {len(content)} chars] {preview}..."
                result.append({**msg, "content": stub, "_view_pruned": True})
                continue
        result.append(msg)
    return result


# ---------------------------------------------------------------------------
# Phase 2: Orphan exclusion (applied at assembly time)
# ---------------------------------------------------------------------------


def exclude_orphans(messages: list[dict]) -> list[dict]:
    """Exclude tool messages whose tool_call_id doesn't match any assistant's tool_calls.

    Returns a new list with orphans removed. Original messages unchanged.
    """
    # Collect valid tool_call_ids from assistant messages
    valid_ids = set()
    for msg in messages:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            tcs = msg["tool_calls"]
            if isinstance(tcs, str):
                try:
                    tcs = json.loads(tcs)
                except json.JSONDecodeError:
                    continue
            if isinstance(tcs, list):
                for tc in tcs:
                    if isinstance(tc, dict):
                        valid_ids.add(tc.get("id", ""))

    result = []
    for msg in messages:
        if msg.get("role") == "tool":
            tcid = msg.get("tool_call_id", "")
            if tcid and tcid not in valid_ids:
                continue  # Orphan — exclude
        result.append(msg)
    return repair_unanswered_tool_calls(result)


ABORTED_CALL_STUB = "Error: tool call aborted before it returned — no result was recorded."


def _tool_call_ids(msg: dict) -> list[str]:
    tcs = msg.get("tool_calls")
    if isinstance(tcs, str):
        try:
            tcs = json.loads(tcs)
        except json.JSONDecodeError:
            return []
    if not isinstance(tcs, list):
        return []
    return [tc.get("id", "") for tc in tcs if isinstance(tc, dict) and tc.get("id")]


def repair_unanswered_tool_calls(messages: list[dict]) -> list[dict]:
    """Give every assistant tool_call a tool row, stubbing the ones that never got one.

    The reverse orphan of `exclude_orphans`: the assistant row with
    `tool_calls` is persisted *before* the round executes, so a round that
    dies mid-flight (cancel during a parallel batch, the executor backstop,
    a DB error while recording results) leaves it with no answers.
    OpenAI-format providers reject that transcript — "assistant message
    with tool_calls must be followed by tool messages" — on every later
    turn until compaction happens to fold the row; with an Ollama fallback
    configured the turn silently ran there instead. The stub is inserted
    after any results the round did record, so ordering stays intact.
    """
    out: list[dict] = []
    i, n = 0, len(messages)
    while i < n:
        msg = messages[i]
        out.append(msg)
        if msg.get("role") != "assistant" or not msg.get("tool_calls"):
            i += 1
            continue
        ids = _tool_call_ids(msg)
        answered: set[str] = set()
        j = i + 1
        while j < n and messages[j].get("role") == "tool":
            out.append(messages[j])
            answered.add(messages[j].get("tool_call_id", ""))
            j += 1
        for tcid in ids:
            if tcid not in answered:
                out.append({"role": "tool", "tool_call_id": tcid, "content": ABORTED_CALL_STUB})
        i = j
    return out


# ---------------------------------------------------------------------------
# Phase 3: LLM summarization (append-only)
# ---------------------------------------------------------------------------

# settings.compaction_keep_tokens (51k) is a global constant that knows
# nothing about the active model. The compiler's history share is roughly the
# context budget minus the output reservation, the system prompt and the tool
# schemas, and it fires compaction at settings.compaction_threshold of that.
# On any model whose window is smaller than ~57k — every Ollama model at the
# default ollama_num_ctx_cap — an unclamped keep_tokens exceeds the entire
# history the compiler will ever hold, the boundary scan below never breaks,
# and the whole live conversation gets folded into a summary.
_KEEP_HISTORY_FRACTION = 0.5
_KEEP_BUDGET_FRACTION = 0.25
_MIN_KEEP_TOKENS = 2_000


def _effective_context_budget(session_id: str) -> int:
    """Context budget for this session, mirroring the agent loop's derivation."""
    try:
        from sessions.manager import get_manager

        session = get_manager().get(session_id)
        override = int(getattr(session, "context_budget_override", 0) or 0) if session else 0
        if override:
            return override
    except Exception as e:
        logger.debug("Session budget override lookup failed for %s: %s", session_id, e)
    try:
        from core.llm.budget import derive_model_budget

        derived = derive_model_budget(settings.llm_model)
        if derived:
            return int(derived)
    except Exception as e:
        logger.debug("Model budget lookup failed: %s", e)
    return int(settings.context_budget)


def _resolve_keep_tokens(session_id: str, history_budget: int | None) -> int:
    """compaction_keep_tokens clamped to what the model can actually hold."""
    configured = int(settings.compaction_keep_tokens)
    if history_budget and history_budget > 0:
        ceiling = int(history_budget * _KEEP_HISTORY_FRACTION)
    else:
        ceiling = int(_effective_context_budget(session_id) * _KEEP_BUDGET_FRACTION)
    return max(min(configured, ceiling), _MIN_KEEP_TOKENS)


def _active_turn_root_index(convo: list[dict]) -> int:
    """Index of the user message that opened the turn currently in flight.

    The compaction boundary must never advance past it. compile_context
    filters history on ``id > compacted_up_to`` *before* its pin can protect
    the active turn's user message, so folding that message leaves the agent
    resuming a live turn with a summary and no idea what it was asked.

    The live root is the last user message that already has a non-user
    message after it. A trailing run of user messages with nothing after them
    is queued for future turns (the compiler hides those behind
    turn_user_msg_id), so the root is the first of that run instead. Both
    readings err toward keeping one turn too many, never one too few.
    Returns -1 when the window holds no user message at all.
    """
    last_nonuser = -1
    for i, msg in enumerate(convo):
        if msg.get("role") != "user":
            last_nonuser = i

    for i in range(last_nonuser - 1, -1, -1):
        if convo[i].get("role") == "user":
            return i

    for i in range(last_nonuser + 1, len(convo)):
        if convo[i].get("role") == "user":
            return i
    return -1


async def compact_with_llm(
    session_id: str,
    messages: list[dict],
    existing_summary: str | None = None,
    history_budget: int | None = None,
) -> bool:
    """Run LLM summarization and append compaction marker. Never deletes messages.

    Returns True if compaction was performed.
    """
    from core.llm.client import get_llm_client

    estimator = get_estimator()

    # The compiled `messages` handed in by the agent have been stripped of their
    # DB ids (_strip_private_fields in compile_context), so we CANNOT derive the
    # compaction boundary from them — doing so recorded compacted_up_to=0 on
    # every run, which never advances the compiler's active-window pointer and
    # drives an unbounded re-compaction loop. Read the authoritative rows from
    # the DB, which carry real `id`s. `messages` is retained only for signature
    # back-compat and is intentionally unused for boundary/id resolution.
    _ = messages
    # Off-loop: the full transcript, tool results and all. compact_with_llm is
    # awaited directly by the agent loop, so an inline read here stalls every
    # other session's SSE for as long as the query takes.
    raw = await asyncio.to_thread(db.get_messages, session_id)

    # Resume from the most recent compaction marker: only summarize messages
    # added since it, carry its summary forward for merging, and never rewind
    # the pointer (which would re-summarize already-folded history).
    prev_compacted_up_to = 0
    if existing_summary is None:
        for m in reversed(raw):
            if m["role"] == "compaction":
                existing_summary = m["content"]
                try:
                    raw_meta = m.get("metadata") or m.get("tool_calls") or "{}"
                    prev_compacted_up_to = int(json.loads(raw_meta).get("compacted_up_to", 0))
                except (json.JSONDecodeError, TypeError, ValueError):
                    prev_compacted_up_to = 0
                break

    # Conversational messages not yet folded into a summary, oldest -> newest.
    convo = [m for m in raw if m["role"] not in _MARKER_ROLES and m["id"] > prev_compacted_up_to]

    # Find compaction boundary: keep recent messages totaling keep_tokens
    keep_tokens = _resolve_keep_tokens(session_id, history_budget)
    total = 0
    boundary_idx = len(convo)
    for i in range(len(convo) - 1, -1, -1):
        tokens = estimator.count_message(convo[i])
        if total + tokens > keep_tokens:
            boundary_idx = i + 1
            break
        total += tokens

    # Hard floor: whatever the token arithmetic says, the live turn stays.
    root_idx = _active_turn_root_index(convo)
    if root_idx >= 0 and boundary_idx > root_idx:
        logger.info(
            "Compaction boundary clamped from %d to %d to preserve the active turn (session %s)",
            boundary_idx,
            root_idx,
            session_id,
        )
        boundary_idx = root_idx

    to_summarize = convo[:boundary_idx]
    if len(to_summarize) < 4:
        logger.debug("Too few messages to summarize (%d)", len(to_summarize))
        return False

    # Build summarization prompt
    if existing_summary:
        prompt = COMPACTION_UPDATE_PROMPT.format(
            existing_summary=existing_summary,
            new_content=_serialize_messages(to_summarize),
        )
    else:
        prompt = COMPACTION_PROMPT + "\n\nCONVERSATION:\n" + _serialize_messages(to_summarize)

    # Call LLM. The agent loop synchronously awaits this call mid-turn, so it
    # must queue with the session's own scheduling identity — the default
    # background identity sorts last in the fair queue (priority inversion).
    from core.llm.client import chat_with_backup, ensure_session_budget, sched_identity

    client = get_llm_client()
    # Criticality tier (audit P3): the summary becomes the session's
    # permanent memory for the primary model — never author it on a
    # silently-weaker background model.
    model = settings.llm_model or settings.background_model
    sched_created_at, sched_priority = sched_identity(session_id)
    # Carrying the session_id subjects this call to the session's wall-clock
    # budget; guarantee headroom so a budget-exhausted turn can still compact
    # instead of dying with compaction_failed.
    ensure_session_budget(session_id, 120)
    try:
        response = await chat_with_backup(
            client,
            messages=[
                {"role": "system", "content": "You are a conversation summarizer."},
                {"role": "user", "content": prompt},
            ],
            model=model,
            max_tokens=4000,
            session_id=session_id,
            session_created_at=sched_created_at,
            session_priority=sched_priority,
        )
        summary = response.content.strip()
    except Exception as e:
        logger.error("Compaction LLM call failed: %s", e)
        return False

    # Quality gates
    summary_tokens = estimator.count(summary)
    if summary_tokens < 20:
        logger.warning("Compaction summary too short (%d tokens), rejected", summary_tokens)
        return False
    if summary_tokens > 5000:
        summary = summary[:20000]  # ~5000 tokens
        logger.warning("Compaction summary truncated to ~5000 tokens")

    # H7: Compression ratio quality gate — reject summaries > 35% of original size
    original_tokens = sum(estimator.count_message(m) for m in to_summarize)
    if original_tokens > 0:
        compression_ratio = summary_tokens / original_tokens
        if compression_ratio > 0.35:
            logger.warning(
                "Compaction summary has poor compression (%.0f%% of original), rejected", compression_ratio * 100
            )
            return False

    # Real DB id of the newest message folded into this summary. `to_summarize`
    # rows come straight from db.get_messages, so `id` is always present.
    last_summarized_id = to_summarize[-1]["id"]

    # Append compaction marker (NEVER delete original messages)
    await asyncio.to_thread(
        db.add_compaction,
        session_id=session_id,
        summary=summary,
        compacted_up_to=last_summarized_id,
        original_count=len(to_summarize),
    )

    logger.info("Compaction complete: summarized %d messages (%d tokens)", len(to_summarize), summary_tokens)

    try:
        from sessions.manager import get_manager

        session = get_manager().get(session_id)
        if session:
            session.emit_event(
                {
                    "type": "context.compacted",
                    "summarized_messages": len(to_summarize),
                    "summary_tokens": summary_tokens,
                }
            )
    except Exception as e:
        logger.debug("context.compacted emit skipped: %s", e)

    return True


def _serialize_messages(messages: list[dict], max_chars: int = 60000) -> str:
    """Serialize messages for LLM summarization prompt."""
    lines = []
    total = 0
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        line = f"[{role}] {content[:2000]}"
        if total + len(line) > max_chars:
            lines.append("[... truncated ...]")
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines)
