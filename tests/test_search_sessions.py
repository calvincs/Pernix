"""Tests for the search_sessions and session_read tools."""

from core.tools.builtin.session_search import search_sessions, session_read
from db import models as db

# ---------------------------------------------------------------------------
# search_sessions — default behavior (current session only)
# ---------------------------------------------------------------------------


def test_search_sessions_defaults_to_current_session():
    sid_self = db.create_session(title="Current Work")
    sid_other = db.create_session(title="Earlier Work")
    db.add_message(sid_self, "user", "marker_default_current_alpha database")
    db.add_message(sid_other, "user", "marker_default_current_alpha database")
    out = search_sessions(query="marker_default_current_alpha", _context={"session_id": sid_self})
    # Only the current session should appear; the other must be filtered out.
    assert sid_self[:8] in out
    assert sid_other[:8] not in out
    assert "current session" in out


def test_search_sessions_no_context_returns_error():
    """Default scope needs a current session id; without _context that's an error, not a silent cross-session fallback."""
    out = search_sessions(query="anything")
    assert out.startswith("Error: no current session context")


# ---------------------------------------------------------------------------
# search_sessions — explicit cross-session ("*" / "all")
# ---------------------------------------------------------------------------


def test_search_sessions_star_searches_all_excluding_self_by_default():
    sid_self = db.create_session(title="Self")
    sid_other = db.create_session(title="Other")
    db.add_message(sid_self, "user", "marker_star_xyz123 database")
    db.add_message(sid_other, "user", "marker_star_xyz123 database")
    out = search_sessions(
        query="marker_star_xyz123",
        session_id="*",
        _context={"session_id": sid_self},
    )
    assert sid_other[:8] in out
    assert sid_self[:8] not in out


def test_search_sessions_star_with_exclude_self_false_includes_self():
    sid_self = db.create_session(title="Self")
    db.add_message(sid_self, "user", "marker_all_inclusive_qqq database")
    out = search_sessions(
        query="marker_all_inclusive_qqq",
        session_id="*",
        exclude_self=False,
        _context={"session_id": sid_self},
    )
    assert sid_self[:8] in out


# ---------------------------------------------------------------------------
# search_sessions — explicit other-session id
# ---------------------------------------------------------------------------


def test_search_sessions_explicit_session_id_restricts_to_that_session():
    sid_target = db.create_session(title="Target")
    sid_other = db.create_session(title="Decoy")
    db.add_message(sid_target, "user", "marker_explicit_qqq database")
    db.add_message(sid_other, "user", "marker_explicit_qqq database")
    # Caller is in a third session, but explicitly asks for sid_target.
    sid_caller = db.create_session(title="Caller")
    out = search_sessions(
        query="marker_explicit_qqq",
        session_id=sid_target,
        _context={"session_id": sid_caller},
    )
    assert sid_target[:8] in out
    assert sid_other[:8] not in out


# ---------------------------------------------------------------------------
# search_sessions — input validation and basic plumbing
# ---------------------------------------------------------------------------


def test_search_sessions_empty_query():
    out = search_sessions(query="")
    assert out.startswith("Error: query is required")


def test_search_sessions_no_results_in_current_session():
    sid = db.create_session(title="Empty test")
    out = search_sessions(query="zzzzz_no_match_at_all_qqqq", _context={"session_id": sid})
    assert "No matching messages found" in out


def test_search_sessions_clamps_limit():
    sid = db.create_session(title="Limit test")
    for i in range(5):
        db.add_message(sid, "user", f"marker_clamp_zzz_test message {i}")
    out = search_sessions(query="marker_clamp_zzz_test", limit=999, _context={"session_id": sid})
    assert "Error" not in out
    assert sid[:8] in out


def test_search_sessions_includes_msg_id():
    """Every result line should carry a msg_id= field (so session_read is the deterministic next step)."""
    sid = db.create_session(title="MsgId test")
    db.add_message(sid, "user", "marker_msgid_lookup_token database")
    out = search_sessions(query="marker_msgid_lookup_token", _context={"session_id": sid})
    assert "msg_id=" in out


def test_search_sessions_registered():
    """Both tools must be registered by the auto-loader."""
    from core.tools.builtin import load_builtin_tools
    from core.tools.registry import ToolRegistry

    reg = ToolRegistry()
    load_builtin_tools(reg)

    tool = reg.get("search_sessions")
    assert tool is not None
    assert tool.parallel_safe is True
    assert "sessions" in tool.tags

    rd = reg.get("session_read")
    assert rd is not None
    assert rd.parallel_safe is True


# ---------------------------------------------------------------------------
# session_read
# ---------------------------------------------------------------------------


def test_session_read_happy_path():
    sid = db.create_session(title="ReadTest")
    mid = db.add_message(sid, "user", "the exact content body to retrieve")
    out = session_read(mid)
    assert f"msg_id={mid}" in out
    assert "role=user" in out
    assert "the exact content body to retrieve" in out


def test_session_read_missing_id_returns_clean_error():
    out = session_read(999_999_999)
    assert "No message found" in out
    assert "999999999" in out


def test_session_read_bad_id_type_returns_clean_error():
    out = session_read("not-an-int")
    assert out.startswith("Error: msg_id must be an integer")


# ---------------------------------------------------------------------------
# search_messages_fts — include_session filter (db-level)
# ---------------------------------------------------------------------------


def test_search_messages_fts_include_session_restricts():
    sid_target = db.create_session(title="Target")
    sid_other = db.create_session(title="Other")
    db.add_message(sid_target, "user", "marker_include_db_aaa beta")
    db.add_message(sid_other, "user", "marker_include_db_aaa beta")
    rows = db.search_messages_fts("marker_include_db_aaa", include_session=sid_target)
    assert rows, "expected at least one result"
    assert all(r["session_id"] == sid_target for r in rows)


def test_search_messages_fts_include_wins_over_exclude():
    sid_target = db.create_session(title="Target")
    db.add_message(sid_target, "user", "marker_include_wins_zzz gamma")
    # Both filters set — include should win.
    rows = db.search_messages_fts(
        "marker_include_wins_zzz",
        include_session=sid_target,
        exclude_session=sid_target,
    )
    assert rows
    assert all(r["session_id"] == sid_target for r in rows)


# ---------------------------------------------------------------------------
# Regression: special chars (%), prefix-id resolution, full-id display
# ---------------------------------------------------------------------------


def test_search_sessions_handles_percent_in_query():
    """`40%` would raise an FTS5 syntax error and previously was swallowed silently."""
    sid = db.create_session(title="PctTest")
    db.add_message(sid, "user", "marker_pct_aaa 40 concentration rule")
    out = search_sessions(query="marker_pct_aaa 40% concentration", _context={"session_id": sid})
    # Must not return "no matching" or an Error — should find the row.
    assert "marker_pct_aaa" in out


def test_search_sessions_handles_url_in_query():
    """URLs contain `:` (FTS5 column syntax) and `/` — must not be parsed as columns."""
    sid = db.create_session(title="UrlTest")
    db.add_message(sid, "user", "marker_url_aaa video reference")
    out = search_sessions(
        query="marker_url_aaa https://youtu.be/kwEtOyaFhCA si=nZGTpeemQsHiqIVv",
        _context={"session_id": sid},
    )
    assert "marker_url_aaa" in out


def test_search_sessions_handles_hyphen_and_dotted_tokens():
    """`Re-run`, `baseline-research`, `watch_items.md` raised "no such column" / syntax errors."""
    sid = db.create_session(title="HyphenTest")
    db.add_message(sid, "user", "marker_hyphen_aaa Re-run baseline-research watch_items.md notes")
    out = search_sessions(
        query="Re-run baseline-research watch_items.md marker_hyphen_aaa",
        _context={"session_id": sid},
    )
    assert "marker_hyphen_aaa" in out


def test_search_sessions_resolves_short_prefix():
    """Agent often copies the short id it sees in earlier tool output."""
    sid_target = db.create_session(title="PrefixTarget")
    db.add_message(sid_target, "user", "marker_prefix_resolve_xyz database")
    caller = db.create_session(title="Caller")
    out = search_sessions(
        query="marker_prefix_resolve_xyz",
        session_id=sid_target[:8],  # short prefix only
        _context={"session_id": caller},
    )
    assert sid_target in out
    assert "marker_prefix_resolve_xyz" in out


def test_search_sessions_rejects_unknown_prefix():
    sid = db.create_session(title="Caller")
    out = search_sessions(
        query="anything",
        session_id="zzzzzz_no_such_prefix",
        _context={"session_id": sid},
    )
    assert out.startswith("Error: session_id")


def test_search_sessions_output_shows_full_session_id():
    """The agent must see the full session id, not a truncated prefix it can't pass back."""
    sid = db.create_session(title="FullIdTest")
    db.add_message(sid, "user", "marker_full_id_token database")
    out = search_sessions(query="marker_full_id_token", _context={"session_id": sid})
    assert sid in out  # full id, not just sid[:8]


def test_resolve_session_id_helper():
    sid = db.create_session(title="ResolveTest")
    # Exact match
    assert db.resolve_session_id(sid) == sid
    # Unambiguous prefix
    assert db.resolve_session_id(sid[:8]) == sid
    # Nonexistent
    assert db.resolve_session_id("zzzzzzz_does_not_exist") is None
    # Empty
    assert db.resolve_session_id("") is None
