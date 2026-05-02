"""Tests for core/context/compaction.py: view pruning, orphan exclusion, LLM compaction."""

import json

import pytest

from core.context.compaction import (
    _serialize_messages,
    apply_view_pruning,
    compact_with_llm,
    exclude_orphans,
)

# ---------------------------------------------------------------------------
# apply_view_pruning
# ---------------------------------------------------------------------------


def test_view_pruning_short_list():
    """Lists shorter than keep_recent are returned as-is."""
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "tool", "content": "a" * 500},
    ]
    result = apply_view_pruning(messages, keep_recent=10)
    assert len(result) == 2
    assert result[1]["content"] == "a" * 500


def test_view_pruning_stubs_old_tools():
    """Old tool results > 300 chars are stubbed."""
    messages = []
    for i in range(15):
        messages.append({"role": "user", "content": f"msg {i}"})
        messages.append({"role": "tool", "content": f"output {'x' * 500}"})

    result = apply_view_pruning(messages, keep_recent=4)
    # Old tool messages should be stubbed
    old_tool = result[1]  # Second message (first tool)
    assert "[pruned" in old_tool["content"]
    # Recent tool messages should be intact
    recent_tool = result[-1]
    assert "output" in recent_tool["content"]


def test_view_pruning_preserves_short_tools():
    """Old tool results <= 300 chars are kept intact."""
    messages = []
    for i in range(15):
        messages.append({"role": "user", "content": f"msg {i}"})
        messages.append({"role": "tool", "content": "short output"})

    result = apply_view_pruning(messages, keep_recent=4)
    assert all("[pruned" not in m.get("content", "") for m in result)


def test_view_pruning_non_tool_untouched():
    """Non-tool messages are never pruned."""
    messages = []
    for i in range(15):
        messages.append({"role": "user", "content": f"long user message {'x' * 500}"})
        messages.append({"role": "assistant", "content": f"long assistant {'x' * 500}"})

    result = apply_view_pruning(messages, keep_recent=4)
    for m in result:
        assert "[pruned" not in m.get("content", "")


# ---------------------------------------------------------------------------
# exclude_orphans
# ---------------------------------------------------------------------------


def test_exclude_orphans_keeps_valid():
    messages = [
        {"role": "assistant", "tool_calls": json.dumps([{"id": "tc1"}])},
        {"role": "tool", "tool_call_id": "tc1", "content": "result"},
        {"role": "user", "content": "hi"},
    ]
    result = exclude_orphans(messages)
    assert len(result) == 3


def test_exclude_orphans_removes_orphan():
    messages = [
        {"role": "tool", "tool_call_id": "nonexistent", "content": "orphan"},
        {"role": "user", "content": "hi"},
    ]
    result = exclude_orphans(messages)
    assert len(result) == 1
    assert result[0]["role"] == "user"


def test_exclude_orphans_tool_calls_as_list():
    messages = [
        {"role": "assistant", "tool_calls": [{"id": "tc1"}, {"id": "tc2"}]},
        {"role": "tool", "tool_call_id": "tc1", "content": "r1"},
        {"role": "tool", "tool_call_id": "tc2", "content": "r2"},
        {"role": "tool", "tool_call_id": "tc3", "content": "orphan"},
    ]
    result = exclude_orphans(messages)
    assert len(result) == 3  # assistant + 2 valid tools


def test_exclude_orphans_no_tool_calls():
    """Tool messages without tool_call_id are kept."""
    messages = [
        {"role": "tool", "content": "no id"},
        {"role": "user", "content": "hi"},
    ]
    result = exclude_orphans(messages)
    assert len(result) == 2  # both kept (no tool_call_id means not orphaned)


# ---------------------------------------------------------------------------
# _serialize_messages
# ---------------------------------------------------------------------------


def test_serialize_basic():
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "world"},
    ]
    result = _serialize_messages(messages)
    assert "[user] hello" in result
    assert "[assistant] world" in result


def test_serialize_truncates_long():
    messages = [
        {"role": "user", "content": "x" * 1000},
    ]
    result = _serialize_messages(messages)
    assert len(result) <= 850  # 800 char content + role prefix


def test_serialize_budget():
    messages = [{"role": "user", "content": "x" * 500} for _ in range(100)]
    result = _serialize_messages(messages, max_chars=2000)
    assert "truncated" in result
    assert len(result) <= 3000  # slightly over due to last line + marker


def test_serialize_list_content():
    """Handle list-format content (vision messages)."""
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe this"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
            ],
        },
    ]
    result = _serialize_messages(messages)
    assert "describe this" in result


# ---------------------------------------------------------------------------
# compact_with_llm (async)
# ---------------------------------------------------------------------------


async def test_compact_with_llm_success(mock_llm_client):
    """LLM compaction writes a compaction marker."""
    from core.llm.types import ChatResponse, TokenUsage
    from db import models as db

    sid = db.create_session(title="Compact Test")
    for i in range(10):
        db.add_message(sid, "user", f"Message {i} with some content " * 10)
        db.add_message(sid, "assistant", f"Response {i} with analysis " * 10)

    # Configure fake LLM to return a good summary
    mock_llm_client.responses = [
        ChatResponse(
            content='```json\n{"goal": "testing", "progress": ["msg sent"]}\n```\nGood summary.',
            tool_calls=None,
            usage=TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
            model="test",
            provider="fake",
            finish_reason="stop",
        )
    ]

    messages = db.get_messages(sid)
    msg_dicts = [{"role": m["role"], "content": m["content"], "id": m["id"]} for m in messages]
    result = await compact_with_llm(sid, msg_dicts)
    assert result is True

    # Verify compaction marker was written
    all_msgs = db.get_messages(sid)
    compaction_msgs = [m for m in all_msgs if m["role"] == "compaction"]
    assert len(compaction_msgs) == 1


async def test_compact_with_llm_too_few_messages(mock_llm_client):
    """Rejects compaction if fewer than 4 messages."""
    from db import models as db

    sid = db.create_session(title="Short")
    db.add_message(sid, "user", "hi")
    db.add_message(sid, "assistant", "hello")

    messages = db.get_messages(sid)
    msg_dicts = [{"role": m["role"], "content": m["content"], "id": m["id"]} for m in messages]
    result = await compact_with_llm(sid, msg_dicts)
    assert result is False


async def test_compact_with_llm_failure(mock_llm_client):
    """Handles LLM failure gracefully."""
    from db import models as db

    sid = db.create_session(title="Fail")
    for i in range(10):
        db.add_message(sid, "user", f"Message {i} " * 20)
        db.add_message(sid, "assistant", f"Response {i} " * 20)

    # Make LLM raise an error
    async def failing_chat(*args, **kwargs):
        raise ConnectionError("LLM down")

    mock_llm_client.chat = failing_chat

    messages = db.get_messages(sid)
    msg_dicts = [{"role": m["role"], "content": m["content"], "id": m["id"]} for m in messages]
    result = await compact_with_llm(sid, msg_dicts)
    assert result is False
