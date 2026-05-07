"""Tests for the search_sessions tool."""

from core.tools.builtin.session_search import search_sessions
from db import models as db


def test_search_sessions_basic_match():
    sid = db.create_session(title="Postgres setup")
    db.add_message(sid, "user", "How do I configure the postgres database connection")
    db.add_message(sid, "assistant", "Use the DATABASE_URL environment variable")
    out = search_sessions(query="database connection")
    assert "postgres" in out.lower() or sid[:8] in out
    assert "score=" in out


def test_search_sessions_excludes_self_by_default():
    sid_self = db.create_session(title="Current Work")
    sid_other = db.create_session(title="Earlier Work")
    db.add_message(sid_self, "user", "unique_marker_xyzabc database")
    db.add_message(sid_other, "user", "unique_marker_xyzabc database")
    out = search_sessions(query="unique_marker_xyzabc", _context={"session_id": sid_self})
    # Only the other session should appear
    assert sid_other[:8] in out
    assert sid_self[:8] not in out


def test_search_sessions_includes_self_when_disabled():
    sid_self = db.create_session(title="Current Work")
    db.add_message(sid_self, "user", "another_marker xyzqqq")
    out = search_sessions(
        query="another_marker xyzqqq",
        exclude_self=False,
        _context={"session_id": sid_self},
    )
    assert sid_self[:8] in out


def test_search_sessions_empty_query():
    out = search_sessions(query="")
    assert out.startswith("Error: query is required")


def test_search_sessions_no_results():
    db.create_session(title="Empty test")
    out = search_sessions(query="zzzzz_no_match_at_all_qqqq")
    assert "No matching messages found" in out


def test_search_sessions_clamps_limit():
    sid = db.create_session(title="Limit test")
    for i in range(5):
        db.add_message(sid, "user", f"unique_clamp_test message {i}")
    # Pass an absurdly high limit; should be clamped to 50 internally (we
    # only have 5 messages anyway, but it must not error out).
    out = search_sessions(query="unique_clamp_test", limit=999)
    # Don't error
    assert "Error" not in out
    # Should contain at least one match
    assert sid[:8] in out


def test_search_sessions_registered():
    """search_sessions must be registered by the auto-loader."""
    from core.tools.builtin import load_builtin_tools
    from core.tools.registry import ToolRegistry

    reg = ToolRegistry()
    load_builtin_tools(reg)
    tool = reg.get("search_sessions")
    assert tool is not None
    assert tool.parallel_safe is True
    # category is on the ToolDef; tag list should include 'sessions'
    assert "sessions" in tool.tags
