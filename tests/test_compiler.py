"""Tests for core/context/compiler.py: context assembly, trimming, normalization."""

import json

import pytest

from core.context.compiler import (
    BASE_SYSTEM_PROMPT,
    _build_temporal_context,
    _is_pinned,
    _trim_history,
    compile_context,
    normalize_for_openrouter,
)
from core.context.tokens import get_estimator

# ---------------------------------------------------------------------------
# _build_temporal_context
# ---------------------------------------------------------------------------


def test_temporal_context_has_time():
    text = _build_temporal_context()
    assert "Current time:" in text
    assert "TEMPORAL CONTEXT" in text


# ---------------------------------------------------------------------------
# _is_pinned
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "msg,expected",
    [
        ({"role": "system", "content": "sys"}, True),
        ({"role": "user", "content": "hi", "_pinned": True}, True),
        ({"role": "assistant", "content": "[Context was reset by user]"}, True),
        ({"role": "tool", "content": "[User answered the question]"}, True),
        ({"role": "user", "content": "normal message"}, False),
        ({"role": "assistant", "content": "ok"}, False),
    ],
)
def test_is_pinned(msg, expected):
    assert _is_pinned(msg) == expected


# ---------------------------------------------------------------------------
# _trim_history
# ---------------------------------------------------------------------------


def test_trim_history_no_trim_needed():
    estimator = get_estimator()
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    result, trimmed = _trim_history(messages, budget=100000, estimator=estimator)
    assert trimmed == 0
    assert len(result) == 3


def test_trim_history_drops_old_first():
    estimator = get_estimator()
    # Create many messages so they exceed budget
    messages = [{"role": "system", "content": "sys"}]
    for i in range(20):
        messages.append({"role": "user", "content": f"message {i} " * 50})
        messages.append({"role": "assistant", "content": f"response {i} " * 50})
    result, trimmed = _trim_history(messages, budget=500, estimator=estimator)
    assert trimmed > 0
    # System message should be preserved
    assert result[0]["role"] == "system"


def test_trim_history_preserves_pinned():
    estimator = get_estimator()
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "old " * 100},
        {"role": "assistant", "content": "[Context was reset by user]"},
        {"role": "user", "content": "new " * 100},
        {"role": "assistant", "content": "response " * 100},
    ]
    result, trimmed = _trim_history(messages, budget=200, estimator=estimator)
    # The context reset message should survive if possible
    has_reset = any("[Context was reset" in m.get("content", "") for m in result)
    assert result[0]["role"] == "system"


def test_trim_history_drops_tool_groups():
    estimator = get_estimator()
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "tc1", "name": "bash"}]},
        {"role": "tool", "content": "output " * 200, "tool_call_id": "tc1"},
        {"role": "user", "content": "recent " * 50},
        {"role": "assistant", "content": "answer " * 50},
    ]
    result, trimmed = _trim_history(messages, budget=200, estimator=estimator)
    # The tool group (assistant+tool) should be dropped together
    assert trimmed >= 2


# ---------------------------------------------------------------------------
# compile_context
# ---------------------------------------------------------------------------


def test_compile_context_basic():
    from db import models as db

    sid = db.create_session(title="Test")
    db.add_message(sid, "user", "Hello")
    db.add_message(sid, "assistant", "Hi there!")

    payload = compile_context(sid)
    assert len(payload.messages) >= 2  # system + user + assistant
    assert payload.messages[0]["role"] == "system"
    assert BASE_SYSTEM_PROMPT in payload.messages[0]["content"]
    assert payload.token_count > 0


def test_compile_context_with_scout():
    from db import models as db

    sid = db.create_session(title="Test")
    db.add_message(sid, "user", "Hello")

    payload = compile_context(sid, scout_report_text="[IDENTITY]\nBe helpful")
    assert "Be helpful" in payload.messages[0]["content"]


def test_compile_context_with_compaction():
    from db import models as db

    sid = db.create_session(title="Test")
    # Add messages, then a compaction marker
    msg1_id = db.add_message(sid, "user", "Old message 1")
    msg2_id = db.add_message(sid, "assistant", "Old response 1")
    db.add_compaction(sid, "Summary of old conversation", compacted_up_to=msg2_id, original_count=2)
    db.add_message(sid, "user", "New message")
    db.add_message(sid, "assistant", "New response")

    payload = compile_context(sid)
    # Should include the compaction summary
    assert payload.has_compaction_summary
    # Should include a system message with the summary
    summaries = [m for m in payload.messages if "Previous conversation summary" in m.get("content", "")]
    assert len(summaries) == 1


def test_compile_context_empty_session():
    from db import models as db

    sid = db.create_session(title="Empty")
    payload = compile_context(sid)
    assert len(payload.messages) == 1  # Just system
    assert payload.messages[0]["role"] == "system"


def test_compile_context_with_tools():
    from db import models as db

    sid = db.create_session(title="Test")
    db.add_message(sid, "user", "Hello")

    tools = [{"type": "function", "function": {"name": "test", "parameters": {}}}]
    payload = compile_context(sid, tool_schemas=tools)
    assert payload.tools == tools
    assert payload.metadata.tool_schema_tokens > 0


# ---------------------------------------------------------------------------
# normalize_for_openrouter
# ---------------------------------------------------------------------------


def test_normalize_removes_mid_system():
    messages = [
        {"role": "system", "content": "first system"},
        {"role": "user", "content": "hello"},
        {"role": "system", "content": "second system"},  # should be removed
        {"role": "assistant", "content": "hi"},
    ]
    result = normalize_for_openrouter(messages)
    system_msgs = [m for m in result if m["role"] == "system"]
    assert len(system_msgs) == 1


def test_normalize_fixes_none_content():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": None},
    ]
    result = normalize_for_openrouter(messages)
    assert result[1]["content"] == ""


def test_normalize_tool_calls_format():
    messages = [
        {"role": "system", "content": "sys"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "tc1", "name": "bash", "arguments": '{"command": "ls"}'}],
        },
        {"role": "tool", "content": "output", "tool_call_id": "tc1"},
    ]
    result = normalize_for_openrouter(messages)
    tc = result[1]["tool_calls"][0]
    assert tc["type"] == "function"
    assert tc["function"]["name"] == "bash"
    assert result[2]["role"] == "tool"


def test_normalize_drops_orphaned_tools():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "tool", "content": "orphan", "tool_call_id": "nonexistent"},
    ]
    result = normalize_for_openrouter(messages)
    assert len(result) == 1  # only system remains


def test_normalize_tool_calls_missing_id():
    """Tool calls without ID get generated IDs."""
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": "", "tool_calls": [{"name": "bash", "arguments": '{"command": "ls"}'}]},
    ]
    result = normalize_for_openrouter(messages)
    tc = result[1]["tool_calls"][0]
    assert tc["id"]  # Should have a generated ID
    assert tc["type"] == "function"
