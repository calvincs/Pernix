"""Pernix — Append-only compaction. Never modifies stored messages.

Phase 1: View-based pruning (at assembly time, zero cost)
Phase 2: Orphan exclusion (at assembly time, zero cost)
Phase 3: LLM summarization (append-only, writes new compaction marker)
"""

from __future__ import annotations

import json
import logging

from config import settings
from core.context.tokens import get_estimator
from db import models as db

logger = logging.getLogger("pernix.context.compaction")

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


def apply_view_pruning(messages: list[dict], keep_recent: int = 10) -> list[dict]:
    """Prune old tool results in a VIEW — original messages unchanged.

    Returns a new list where old tool results > 300 chars are stubbed.
    This is a view transform for context assembly, NOT a DB mutation.
    """
    if len(messages) <= keep_recent:
        return list(messages)

    cutoff = len(messages) - keep_recent
    result = []
    for i, msg in enumerate(messages):
        if i < cutoff and msg.get("role") == "tool":
            content = msg.get("content", "")
            if len(content) > 300:
                preview = content[:80].replace("\n", " ")
                stub = f"[pruned — {len(content)} chars] {preview}..."
                result.append({**msg, "content": stub})
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
    return result


# ---------------------------------------------------------------------------
# Phase 3: LLM summarization (append-only)
# ---------------------------------------------------------------------------


async def compact_with_llm(
    session_id: str,
    messages: list[dict],
    existing_summary: str | None = None,
) -> bool:
    """Run LLM summarization and append compaction marker. Never deletes messages.

    Returns True if compaction was performed.
    """
    from core.llm.client import get_llm_client

    estimator = get_estimator()

    # Find compaction boundary: keep recent messages totaling compaction_keep_tokens
    keep_tokens = settings.compaction_keep_tokens
    total = 0
    boundary_idx = len(messages)
    for i in range(len(messages) - 1, -1, -1):
        tokens = estimator.count_message(messages[i])
        if total + tokens > keep_tokens:
            boundary_idx = i + 1
            break
        total += tokens

    to_summarize = messages[:boundary_idx]
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

    # Call LLM
    client = get_llm_client()
    model = settings.background_model or settings.llm_model
    try:
        response = await client.chat(
            messages=[
                {"role": "system", "content": "You are a conversation summarizer."},
                {"role": "user", "content": prompt},
            ],
            model=model,
            max_tokens=2000,
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

    # Get the last message ID being summarized
    last_summarized_id = to_summarize[-1].get("id", 0) if to_summarize else 0

    # Append compaction marker (NEVER delete original messages)
    db.add_compaction(
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


def _serialize_messages(messages: list[dict], max_chars: int = 30000) -> str:
    """Serialize messages for LLM summarization prompt."""
    lines = []
    total = 0
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        line = f"[{role}] {content[:800]}"
        if total + len(line) > max_chars:
            lines.append("[... truncated ...]")
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines)
