"""Tests for core/context/compiler.py: context assembly, trimming, normalization."""

import json

import pytest

from core.context.compiler import (
    BASE_SYSTEM_PROMPT,
    _build_temporal_context,
    _build_trim_notice,
    _is_pinned,
    _strip_private_fields,
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
    assert "Current time (UTC):" in text
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
    result, trimmed, dropped = _trim_history(messages, budget=100000, estimator=estimator)
    assert trimmed == 0
    assert dropped == []
    assert len(result) == 3


def test_trim_history_drops_old_first():
    estimator = get_estimator()
    # Create many messages so they exceed budget
    messages = [{"role": "system", "content": "sys"}]
    for i in range(20):
        messages.append({"role": "user", "content": f"message {i} " * 50})
        messages.append({"role": "assistant", "content": f"response {i} " * 50})
    result, trimmed, dropped = _trim_history(messages, budget=500, estimator=estimator)
    assert trimmed > 0
    assert len(dropped) > 0
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
    result, trimmed, _dropped = _trim_history(messages, budget=200, estimator=estimator)
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
    result, trimmed, dropped = _trim_history(messages, budget=200, estimator=estimator)
    # The tool group (assistant+tool) should be dropped together
    assert trimmed >= 2
    assert any(g.get("kind") == "assistant_group" for g in dropped)


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


# ---------------------------------------------------------------------------
# Trim notice + pin-by-turn-id + private-field strip
# ---------------------------------------------------------------------------


def test_trim_history_returns_dropped_groups():
    estimator = get_estimator()
    messages = [{"role": "system", "content": "sys"}]
    # Build several assistant+tool groups that will exceed the budget.
    for i in range(6):
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": f"tc{i}", "type": "function", "function": {"name": "search_web"}}],
                "_db_id": 100 + (2 * i),
                "_tool_names": ["search_web"],
                "_created_at": "2026-05-11T19:20:00",
            }
        )
        messages.append(
            {
                "role": "tool",
                "content": "result body " * 50,
                "tool_call_id": f"tc{i}",
                "_db_id": 100 + (2 * i) + 1,
                "_created_at": "2026-05-11T19:20:01",
            }
        )

    result, trimmed, dropped = _trim_history(messages, budget=300, estimator=estimator)
    assert trimmed > 0
    assert len(dropped) > 0
    grp = dropped[0]
    assert grp["kind"] == "assistant_group"
    # Each snapshot should carry the metadata needed for the trim notice
    for s in grp["msgs"]:
        assert "_db_id" in s
        assert "role" in s


def test_build_trim_notice_quotes_user_message_in_full():
    user_content = "prove or disprove this thesis: https://x.com/example/status/12345"
    dropped = [
        {
            "kind": "user",
            "msgs": [
                {
                    "_db_id": 21045,
                    "role": "user",
                    "_tool_names": None,
                    "_created_at": "2026-05-11T19:18:25",
                    "content_len": len(user_content),
                    "content_preview": user_content[:80],
                    "content_full": user_content,
                }
            ],
        }
    ]
    notice = _build_trim_notice(dropped)
    assert "Context trim notice" in notice
    assert "21045" in notice
    assert "https://x.com/example/status/12345" in notice
    assert "session_read(msg_id)" in notice
    assert "search_sessions" in notice


def test_build_trim_notice_summarizes_tool_group_by_tool_name():
    dropped = [
        {
            "kind": "assistant_group",
            "msgs": [
                {
                    "_db_id": 21057,
                    "role": "assistant",
                    "_tool_names": ["search_web", "search_web", "search_web"],
                    "_created_at": "2026-05-11T19:21:30",
                    "content_len": 100,
                    "content_preview": "",
                    "content_full": None,
                },
                {
                    "_db_id": 21058,
                    "role": "tool",
                    "_tool_names": None,
                    "_created_at": "2026-05-11T19:21:35",
                    "content_len": 27093,
                    "content_preview": "<html>...",
                    "content_full": None,
                },
            ],
        }
    ]
    notice = _build_trim_notice(dropped)
    assert "21057" in notice
    assert "21058" in notice
    assert "search_web" in notice


def test_build_trim_notice_empty_when_no_drops():
    assert _build_trim_notice([]) == ""


def test_strip_private_fields_removes_underscore_keys():
    msgs = [
        {"role": "system", "content": "x", "_pinned": True, "_db_id": 1},
        {"role": "user", "content": "y", "_db_id": 2, "_tool_names": ["bash"]},
    ]
    out = _strip_private_fields(msgs)
    assert all("_" not in k or not k.startswith("_") for m in out for k in m.keys())
    assert out[0]["role"] == "system"
    assert out[1]["content"] == "y"


def test_compile_context_pins_active_turn_user_message():
    """When trim is forced, the active turn's user message must survive."""
    from db import models as db

    sid = db.create_session(title="PinTest")
    # Older user message — eligible for trim
    db.add_message(sid, "user", "old prompt " * 30)
    db.add_message(sid, "assistant", "old answer " * 30)
    # Active user message — must survive
    active_id = db.add_message(sid, "user", "ACTIVE_TURN_PROMPT https://example.com/needed-url")
    db.add_message(sid, "assistant", "padding " * 200)

    # Force a tiny budget so trim must run.
    payload = compile_context(sid, context_budget=4500, turn_user_msg_id=active_id)
    survived = any(
        "ACTIVE_TURN_PROMPT" in (m.get("content") or "") if isinstance(m.get("content"), str) else False
        for m in payload.messages
    )
    assert survived, "active turn's user message was evicted despite turn_user_msg_id pin"


def test_compile_context_emits_trim_notice_when_drops_occur():
    from db import models as db

    sid = db.create_session(title="NoticeTest")
    # history_budget floor is 4000 tokens, so each message needs to be hefty
    # for trim to actually trigger. Use ~3KB per message — 4 of them blow past.
    big = "alpha beta gamma delta " * 400  # ~9KB
    db.add_message(sid, "user", "first turn padding " + big)
    db.add_message(sid, "assistant", "first answer padding " + big)
    db.add_message(sid, "user", "second turn padding " + big)
    db.add_message(sid, "assistant", "second answer padding " + big)

    payload = compile_context(sid, context_budget=4500)
    has_notice = any(
        m.get("role") == "system" and "[Context trim notice" in (m.get("content") or "") for m in payload.messages
    )
    assert has_notice, f"messages: {[(m.get('role'), (m.get('content') or '')[:60]) for m in payload.messages]}"


def test_compile_context_no_notice_when_under_budget():
    from db import models as db

    sid = db.create_session(title="NoNoticeTest")
    db.add_message(sid, "user", "hi")
    db.add_message(sid, "assistant", "hello")
    payload = compile_context(sid)  # default huge budget
    has_notice = any(
        m.get("role") == "system" and "[Context trim notice" in (m.get("content") or "") for m in payload.messages
    )
    assert not has_notice


def test_compile_context_messages_are_strip_clean():
    """No `_`-prefixed top-level keys may leak to the LLM-bound messages."""
    from db import models as db

    sid = db.create_session(title="StripTest")
    db.add_message(sid, "user", "hello")
    db.add_message(sid, "assistant", "hi")

    payload = compile_context(sid)
    for m in payload.messages:
        leaked = [k for k in m.keys() if isinstance(k, str) and k.startswith("_")]
        assert not leaked, f"leaked private keys: {leaked}"
