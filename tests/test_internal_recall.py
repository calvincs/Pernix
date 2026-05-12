"""Tests for core.memory.internal_recall — the composed memory + cross-session
helper that search_web uses to surface internal hits alongside web results.

The helper must NEVER raise; any backend failure degrades to an empty field
plus a DEBUG log. These tests pin that contract along with the strong-match
threshold and the format_for_tool_output rendering rules.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

# ---------------------------------------------------------------------------
# internal_recall()
# ---------------------------------------------------------------------------


def test_empty_query_returns_empty_result():
    from core.memory.internal_recall import internal_recall

    r = internal_recall("")
    assert r.queried is True
    assert r.memory_text == ""
    assert r.session_text == ""
    assert r.memory_strong is False
    assert r.session_strong is False


def test_whitespace_only_query_returns_empty_result():
    from core.memory.internal_recall import internal_recall

    r = internal_recall("   \t\n  ")
    assert r.queried is True
    assert r.memory_text == ""
    assert r.session_text == ""


def test_memory_store_unavailable_does_not_raise(monkeypatch):
    """get_memory_store() returning None must degrade quietly."""
    from core.memory import internal_recall as mod

    monkeypatch.setattr(mod, "__name__", mod.__name__)  # no-op anchor
    # Patch get_memory_store at its source module so the import in
    # internal_recall picks up the patched value.
    import core.memory.store as store_mod

    monkeypatch.setattr(store_mod, "get_memory_store", lambda: None)

    r = mod.internal_recall("anything", current_session_id="abc")
    assert r.memory_text == ""
    assert r.memory_strong is False


def test_memory_store_exception_logged_not_raised(monkeypatch, caplog):
    """If store.search() blows up, the call still returns; failure logged
    at DEBUG (not surfaced to the agent)."""
    import core.memory.store as store_mod
    from core.memory import internal_recall as mod

    class BoomStore:
        def search(self, *a, **kw):
            raise RuntimeError("FTS index corrupt")

    monkeypatch.setattr(store_mod, "get_memory_store", lambda: BoomStore())

    with caplog.at_level("DEBUG", logger="pernix.memory.internal_recall"):
        r = mod.internal_recall("anything", current_session_id="abc")

    assert r.queried is True
    assert r.memory_text == ""
    assert any("Internal memory recall failed" in rec.message for rec in caplog.records)


def test_cross_session_exception_logged_not_raised(monkeypatch, caplog):
    import core.scout.search as scout_search_mod
    from core.memory import internal_recall as mod

    monkeypatch.setattr(
        scout_search_mod,
        "gather_cross_session_data",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("DB locked")),
    )

    with caplog.at_level("DEBUG", logger="pernix.memory.internal_recall"):
        r = mod.internal_recall("anything", current_session_id="abc")

    assert r.session_text == ""
    assert r.session_strong is False
    assert any("cross-session recall failed" in rec.message.lower() for rec in caplog.records)


def test_strong_match_threshold(monkeypatch):
    """A score > 3.0 must flip memory_strong; score <= 3.0 must NOT."""
    import core.memory.store as store_mod
    from core.memory import internal_recall as mod

    def make_result(file_name: str, score: float, content: str = "snippet"):
        entry = SimpleNamespace(
            file_name=file_name,
            epoch="1",
            content=content,
            entry_type="finding",
        )
        return SimpleNamespace(entry=entry, score=score, source="bm25")

    # Below threshold
    class WeakStore:
        def search(self, *a, **kw):
            return [make_result("notes", 2.5), make_result("notes", 1.0)]

    monkeypatch.setattr(store_mod, "get_memory_store", lambda: WeakStore())
    monkeypatch.setattr(
        __import__("core.scout.search", fromlist=["x"]),
        "gather_cross_session_data",
        lambda *a, **kw: "",
    )
    r = mod.internal_recall("q", current_session_id="abc")
    assert r.memory_text != ""
    assert r.memory_strong is False, "score 2.5 must NOT be strong"

    # Above threshold
    class StrongStore:
        def search(self, *a, **kw):
            return [make_result("notes", 2.5), make_result("research", 5.0)]

    monkeypatch.setattr(store_mod, "get_memory_store", lambda: StrongStore())
    r = mod.internal_recall("q", current_session_id="abc")
    assert r.memory_strong is True


def test_excludes_current_session(monkeypatch):
    """current_session_id is forwarded to gather_cross_session_data so the
    caller's own session is not echoed back as a hit."""
    import core.scout.search as scout_search_mod
    from core.memory import internal_recall as mod

    captured: dict = {}

    def fake_gather(query, sid):
        captured["query"] = query
        captured["sid"] = sid
        return ""

    monkeypatch.setattr(scout_search_mod, "gather_cross_session_data", fake_gather)
    import core.memory.store as store_mod

    monkeypatch.setattr(store_mod, "get_memory_store", lambda: None)

    mod.internal_recall("hello world", current_session_id="my-session")
    assert captured["query"] == "hello world"
    assert captured["sid"] == "my-session"


def test_missing_session_id_passes_empty_string(monkeypatch):
    """gather_cross_session_data requires a string; None must be normalized."""
    import core.scout.search as scout_search_mod
    from core.memory import internal_recall as mod

    captured: dict = {}
    monkeypatch.setattr(
        scout_search_mod,
        "gather_cross_session_data",
        lambda q, sid: captured.setdefault("sid", sid) or "",
    )
    import core.memory.store as store_mod

    monkeypatch.setattr(store_mod, "get_memory_store", lambda: None)

    mod.internal_recall("q", current_session_id=None)
    assert captured["sid"] == ""


# ---------------------------------------------------------------------------
# format_for_tool_output()
# ---------------------------------------------------------------------------


def test_format_both_empty_returns_empty_string():
    from core.memory.internal_recall import InternalRecall, format_for_tool_output

    assert format_for_tool_output(InternalRecall(queried=True)) == ""


def test_format_memory_only():
    from core.memory.internal_recall import InternalRecall, format_for_tool_output

    out = format_for_tool_output(InternalRecall(memory_text="[file score=4.0] hit", memory_strong=True, queried=True))
    assert "INTERNAL KNOWLEDGE" in out
    assert "MEMORY:" in out
    assert "[file score=4.0] hit" in out
    assert "PRIOR SESSIONS: no matching hits." in out
    assert "Strong internal match" in out


def test_format_sessions_only():
    from core.memory.internal_recall import InternalRecall, format_for_tool_output

    out = format_for_tool_output(
        InternalRecall(session_text="CROSS-SESSION FINDINGS:\n...", session_strong=True, queried=True)
    )
    assert "MEMORY: no matching entries." in out
    assert "CROSS-SESSION FINDINGS:" in out
    assert "Strong internal match" in out


def test_format_neither_strong_omits_nudge():
    from core.memory.internal_recall import InternalRecall, format_for_tool_output

    out = format_for_tool_output(
        InternalRecall(
            memory_text="[file score=1.5] weak hit",
            memory_strong=False,
            session_strong=False,
            queried=True,
        )
    )
    assert "Strong internal match" not in out


def test_format_both_populated():
    from core.memory.internal_recall import InternalRecall, format_for_tool_output

    out = format_for_tool_output(
        InternalRecall(
            memory_text="MEM",
            session_text="SESS",
            memory_strong=True,
            session_strong=True,
            queried=True,
        )
    )
    # Memory section comes before sessions section
    assert out.index("MEMORY:") < out.index("SESS")
    assert "Strong internal match" in out


# ---------------------------------------------------------------------------
# FTS-hostile queries should not raise
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "AND OR NOT",  # FTS5 operators
        '"unterminated quote',  # malformed quoted phrase
        "col:value",  # column-filter syntax FTS5 might reject
        "foo*bar*",  # prefix-token spam
        "  ​  ",  # zero-width whitespace
        "a" * 10000,  # absurdly long single token
    ],
)
def test_hostile_queries_do_not_raise(query):
    """Whatever the agent throws at us via tool-args, we never raise."""
    from core.memory.internal_recall import internal_recall

    r = internal_recall(query, current_session_id="abc")
    # The contract: an InternalRecall is returned, queried may be True or
    # False (empty-query short-circuit), but no exception escapes.
    assert hasattr(r, "memory_text")
    assert hasattr(r, "session_text")
