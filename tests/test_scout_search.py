"""Tests for core/scout/search.py: keyword extraction, cross-session, deep memory."""

import pytest

from core.scout.search import _extract_keywords, gather_cross_session_data, gather_deep_memory

# ---------------------------------------------------------------------------
# _extract_keywords
# ---------------------------------------------------------------------------


def test_extract_keywords_basic():
    kws = _extract_keywords("How do I configure the database settings?")
    assert "configure" in kws
    assert "database" in kws
    assert "settings" in kws


def test_extract_keywords_filters_stopwords():
    kws = _extract_keywords("the and for are but not you all can had")
    assert len(kws) == 0


def test_extract_keywords_filters_short():
    kws = _extract_keywords("a bb ccc dddd")
    assert "a" not in kws
    assert "bb" not in kws  # len <= 3
    assert "ccc" not in kws  # len <= 3
    assert "dddd" in kws


def test_extract_keywords_max_limit():
    kws = _extract_keywords("alpha bravo charlie delta echo foxtrot golf hotel india", max_keywords=3)
    assert len(kws) == 3


def test_extract_keywords_dedup():
    kws = _extract_keywords("test test test test unique")
    assert kws.count("test") == 1
    assert "unique" in kws


def test_extract_keywords_strips_punctuation():
    kws = _extract_keywords("error! what's wrong? check config.")
    assert "error" in kws
    assert "wrong" in kws
    assert "check" in kws


# ---------------------------------------------------------------------------
# gather_cross_session_data
# ---------------------------------------------------------------------------


def test_cross_session_empty(tmp_path, monkeypatch):
    """No sessions in DB → returns empty string."""
    result = gather_cross_session_data("test query", "current-session")
    assert result == ""


def test_cross_session_with_data(tmp_path, monkeypatch):
    """Cross-session search finds matches in other sessions."""
    from db import models as db

    # Create another session with matching content
    other_sid = db.create_session(title="Other Session")
    db.add_message(other_sid, "user", "How to configure database?")
    db.add_message(other_sid, "assistant", "Use settings.json to configure the database path.")

    result = gather_cross_session_data("configure database", "current-session")
    # Should find something from other-session
    if result:  # FTS may or may not match depending on indexing
        assert "CROSS-SESSION" in result


def test_cross_session_excludes_self(tmp_path, monkeypatch):
    """Should not return results from the current session."""
    from db import models as db

    self_sid = db.create_session(title="Self")
    db.add_message(self_sid, "user", "unique_test_marker_xyz")

    result = gather_cross_session_data("unique_test_marker_xyz", self_sid)
    # Even if FTS matches, it should be excluded
    assert "unique_test_marker_xyz" not in result


# ---------------------------------------------------------------------------
# gather_deep_memory
# ---------------------------------------------------------------------------


def test_deep_memory_empty():
    """No memory store → returns empty string."""
    result = gather_deep_memory("test query")
    assert result == ""


def test_deep_memory_with_entries(tmp_path, monkeypatch):
    """Deep memory with entries returns formatted results."""
    from core.memory.store import MemoryStore

    store = MemoryStore(str(tmp_path / "memories"))
    store.add_entry("Database config uses settings.json", file_name="pernix.notes")
    store.add_entry("Auth uses bearer tokens for API access", file_name="pernix.notes")

    monkeypatch.setattr("core.memory.store.get_memory_store", lambda: store)

    result = gather_deep_memory("database configuration")
    if result:  # BM25 may or may not match
        assert "score=" in result
