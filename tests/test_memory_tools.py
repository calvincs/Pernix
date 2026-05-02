"""Tests for memory tools, ingest parsing, and skill tools."""

import pytest

from core.memory.ingest import _clean_section_content, parse_sections, route_section_keywords

# ---------------------------------------------------------------------------
# parse_sections
# ---------------------------------------------------------------------------


def test_parse_sections_basic():
    text = "# Section One\nContent one here.\n# Section Two\nContent two here."
    sections = parse_sections(text)
    assert len(sections) == 2
    assert sections[0]["heading"] == "Section One"
    assert "Content one" in sections[0]["content"]
    assert sections[1]["heading"] == "Section Two"


def test_parse_sections_preamble():
    text = "This is preamble text.\n\n# First Heading\nContent here."
    sections = parse_sections(text)
    # Preamble should be captured
    assert len(sections) == 2
    assert sections[0]["heading"] == "preamble"
    assert "preamble text" in sections[0]["content"]


def test_parse_sections_multiple_levels():
    text = "# H1\nH1 content.\n## H2\nH2 content.\n### H3\nH3 content."
    sections = parse_sections(text)
    assert len(sections) == 3
    assert sections[0]["level"] == 1
    assert sections[1]["level"] == 2
    assert sections[2]["level"] == 3


def test_parse_sections_empty():
    assert parse_sections("") == []


def test_parse_sections_no_headings():
    text = "Just some text\nwithout any headings."
    sections = parse_sections(text)
    # Returns as single preamble section
    assert len(sections) == 1
    assert sections[0]["heading"] == "preamble"


def test_parse_sections_index_increments():
    text = "# A\ncontent\n# B\ncontent\n# C\ncontent"
    sections = parse_sections(text)
    assert sections[0]["index"] == 0
    assert sections[1]["index"] == 1
    assert sections[2]["index"] == 2


# ---------------------------------------------------------------------------
# _clean_section_content
# ---------------------------------------------------------------------------


def test_clean_section_content_removes_hr():
    text = "text\n---\nmore text"
    result = _clean_section_content(text)
    assert "---" not in result


def test_clean_section_content_collapses_newlines():
    text = "line1\n\n\n\nline2"
    result = _clean_section_content(text)
    assert "\n\n\n" not in result


def test_clean_section_content_strips():
    text = "\n  content  \n"
    result = _clean_section_content(text)
    assert result == "content"


# ---------------------------------------------------------------------------
# route_section_keywords
# ---------------------------------------------------------------------------


def test_route_section_keywords_user_profile():
    result = route_section_keywords("Who I Am", "My name is Calvin, I live in Seattle")
    assert result == "user.profile"


def test_route_section_keywords_debugging():
    result = route_section_keywords("Bug Fix", "Fixed the bug in the auth module, debug workaround")
    assert result == "pernix.debugging"


def test_route_section_keywords_default():
    result = route_section_keywords("Random", "no matching keywords here")
    assert result == "pernix.notes"


def test_route_section_keywords_lessons():
    result = route_section_keywords("Lessons", "lesson learned: never forget this critical mistake")
    assert result == "pernix.lessons"


# ---------------------------------------------------------------------------
# memory tools: remember / recall
# ---------------------------------------------------------------------------


def test_remember_basic(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from core.tools.builtin.memory_tools import remember

    result = remember("Important finding about the database schema")
    assert "Saved" in result or "Error" not in result


def test_remember_empty_content(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from core.tools.builtin.memory_tools import remember

    result = remember("")
    assert "Error" in result


def test_remember_high_weight_auto(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from core.tools.builtin.memory_tools import remember

    result = remember("CRITICAL: Never delete the production database")
    assert "Error" not in result or "Saved" in result


def test_recall_empty_store(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from core.tools.builtin.memory_tools import recall

    result = recall("anything")
    assert isinstance(result, str)


def test_recall_with_entries(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from core.tools.builtin.memory_tools import recall, remember

    remember("Database uses SQLite with WAL mode for concurrency")
    result = recall("database SQLite")
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# dialog tools: ask_user
# ---------------------------------------------------------------------------


def test_ask_user_basic():
    from core.tools.builtin.dialog_tools import ask_user
    from db import models as db

    sid = db.create_session(title="Ask User Test")
    result = ask_user("What is your name?", _context={"session_id": sid})
    assert isinstance(result, str)
    assert "question" in result.lower() or "pending" in result.lower() or "name" in result.lower()


def test_ask_user_no_session():
    from core.tools.builtin.dialog_tools import ask_user

    result = ask_user("What is your name?", _context=None)
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# truncation
# ---------------------------------------------------------------------------


def test_truncate_output_short():
    from core.tools.truncation import truncate_output

    result, meta = truncate_output("short content", "test")
    assert result == "short content"
    assert meta.get("truncated") is False or not meta.get("truncated")


def test_truncate_output_long():
    from core.tools.truncation import MAX_OUTPUT, truncate_output

    long_content = "x" * (MAX_OUTPUT + 1000)
    result, meta = truncate_output(long_content, "test")
    assert len(result) <= MAX_OUTPUT + 500  # some header overhead allowed
    assert "TRUNCATED" in result or "truncated" in result.lower()
