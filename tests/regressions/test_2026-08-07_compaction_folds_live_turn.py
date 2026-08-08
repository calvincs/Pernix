"""Regression: compaction could fold the turn it was called from.

Shipped defect (architecture review 2026-08-07, Appendix C §3): the boundary
scan in compact_with_llm walked backwards accumulating tokens until it passed
settings.compaction_keep_tokens. When the whole unfolded conversation fit
inside that budget the loop never broke, boundary_idx stayed at len(convo),
and `to_summarize` was EVERYTHING — including the current turn's user message
and its in-flight tool results. compacted_up_to was then set to the newest
message id, and the next compile filtered that user message out (compiler
filters on `id > compacted_up_to` *before* its active-turn pin can protect
it). The agent resumed a live turn holding a summary and no idea what it had
been asked.

compaction_keep_tokens defaults to 51,000 and was never clamped against the
model's history budget, while compaction fires at 75% of that budget — so on
any model with a sub-51k window (every Ollama model at the default
ollama_num_ctx_cap of 65,536) this was the *normal* path, not an edge case.

Fix: keep_tokens is clamped to a fraction of the derivable budget, and the
boundary can never advance past the active turn's root user message.

Also pinned here: apply_view_pruning crashed with `len(None)` on a NULL
content column because it used `msg.get("content", "")` where the rest of the
compiler uses `msg.get("content") or ""`.
"""

import json

import pytest

from core.context.compaction import (
    _active_turn_root_index,
    _resolve_keep_tokens,
    apply_view_pruning,
    compact_with_llm,
)
from core.llm.types import ChatResponse, TokenUsage


def _summary_response() -> ChatResponse:
    return ChatResponse(
        content='```json\n{"goal": "testing", "progress": ["did things"]}\n```\nShort summary of prior work.',
        tool_calls=None,
        usage=TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
        model="test",
        provider="fake",
        finish_reason="stop",
    )


# ---------------------------------------------------------------------------
# keep_tokens clamp
# ---------------------------------------------------------------------------


def test_keep_tokens_clamped_to_small_model_budget(monkeypatch):
    """A 65k-window Ollama model derives a budget far under the configured
    51,000 keep_tokens; keeping 51k of a ~20k history folds all of it."""
    monkeypatch.setattr("config.settings.compaction_keep_tokens", 51_000)
    monkeypatch.setattr("config.settings.context_budget", 58_982)
    monkeypatch.setattr("config.settings.context_auto", False)

    assert _resolve_keep_tokens("no-such-session", None) < 51_000


def test_keep_tokens_honors_configured_value_when_budget_is_large(monkeypatch):
    monkeypatch.setattr("config.settings.compaction_keep_tokens", 51_000)
    monkeypatch.setattr("config.settings.context_budget", 1_000_000)
    monkeypatch.setattr("config.settings.context_auto", False)

    assert _resolve_keep_tokens("no-such-session", None) == 51_000


def test_keep_tokens_uses_explicit_history_budget(monkeypatch):
    monkeypatch.setattr("config.settings.compaction_keep_tokens", 51_000)

    assert _resolve_keep_tokens("no-such-session", 10_000) == 5_000


# ---------------------------------------------------------------------------
# Active-turn boundary
# ---------------------------------------------------------------------------


def test_active_turn_root_is_the_user_with_work_after_it():
    convo = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": "live"},
        {"role": "assistant", "content": "b"},
        {"role": "tool", "content": "c"},
    ]
    assert _active_turn_root_index(convo) == 2


def test_queued_user_messages_do_not_move_the_root():
    """A user message that landed while the turn was running is queued for a
    future turn — it must not be mistaken for the live turn's root, or the
    turn actually in flight becomes summarizable."""
    convo = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": "live"},
        {"role": "assistant", "content": "b"},
        {"role": "tool", "content": "c"},
        {"role": "user", "content": "queued"},
    ]
    assert _active_turn_root_index(convo) == 2


def test_root_falls_back_to_first_trailing_user():
    convo = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": "live, no reply yet"},
    ]
    assert _active_turn_root_index(convo) == 0


def test_root_is_negative_without_user_messages():
    assert _active_turn_root_index([{"role": "assistant", "content": "a"}]) == -1


@pytest.mark.asyncio
async def test_live_turn_user_message_survives_compaction(mock_llm_client, monkeypatch):
    """The headline case: a whole conversation that fits inside keep_tokens.
    Before the fix compacted_up_to landed on the newest message and the live
    turn's user message vanished from the next compile."""
    from db import models as db

    # Small enough that the entire conversation fits inside keep_tokens —
    # exactly the condition that made boundary_idx stay at len(convo).
    monkeypatch.setattr("config.settings.compaction_keep_tokens", 51_000)
    monkeypatch.setattr("config.settings.context_auto", False)

    sid = db.create_session(title="Live Turn")
    for i in range(6):
        db.add_message(sid, "user", f"old question {i} " * 12)
        db.add_message(sid, "assistant", f"old answer {i} " * 12)
    live_id = db.add_message(sid, "user", "LIVE_TURN_PROMPT: build the thing")
    db.add_message(sid, "assistant", "working on it")
    db.add_message(sid, "tool", "tool output for the live turn")

    mock_llm_client.responses = [_summary_response()]
    stripped = [{"role": m["role"], "content": m["content"]} for m in db.get_messages(sid)]
    assert await compact_with_llm(sid, stripped) is True

    marker = [m for m in db.get_messages(sid) if m["role"] == "compaction"][-1]
    compacted_up_to = json.loads(marker["metadata"])["compacted_up_to"]
    assert compacted_up_to < live_id, "compaction folded the live turn's own user message"

    # And the message really is still in the compiler's active window.
    survivors = [m for m in db.get_messages(sid) if m["id"] > compacted_up_to]
    assert any("LIVE_TURN_PROMPT" in (m["content"] or "") for m in survivors)


# ---------------------------------------------------------------------------
# NULL content
# ---------------------------------------------------------------------------


def test_view_pruning_tolerates_null_content():
    """NULL content columns come back as None; `.get("content", "")` returned
    None and len() raised TypeError mid-compile."""
    messages = [{"role": "user", "content": f"msg {i}"} for i in range(10)]
    messages.insert(0, {"role": "tool", "content": None})

    result = apply_view_pruning(messages, keep_recent=2, min_chars=10)

    assert result[0]["content"] is None
