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
    result = route_section_keywords("Who I Am", "My name is Alice, I live in Portland")
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
    assert result.startswith("SAVED file=")
    assert result.endswith("VERIFY=OK")


def test_remember_empty_content(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from core.tools.builtin.memory_tools import remember

    result = remember("")
    assert result == "NOT SAVED — Empty content"


def test_remember_high_weight_auto(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from core.tools.builtin.memory_tools import remember

    result = remember("CRITICAL: Never delete the production database")
    assert result.startswith("SAVED file=")


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


# ---------------------------------------------------------------------------
# _coerce_epoch — scientific-notation epochs from local-model serializers
# (session 1e2806e0d2ea: 12 failed update_memory retries on '1.777e+09')
# ---------------------------------------------------------------------------


def test_coerce_epoch_accepts_int_float_and_strings():
    from core.tools.builtin.memory_tools import _coerce_epoch

    assert _coerce_epoch(1777154774) == 1777154774
    assert _coerce_epoch(1777154774.0) == 1777154774
    assert _coerce_epoch("1777154774") == 1777154774
    assert _coerce_epoch("1.777154774e+09") == 1777154774
    assert _coerce_epoch(" 1.78690871e+09 ") == 1786908710


def test_coerce_epoch_rejects_fractional_and_garbage():
    from core.tools.builtin.memory_tools import _coerce_epoch

    for bad in (1777154774.5, "1.7771547745e+09", "not-an-epoch", True):
        with pytest.raises(ValueError):
            _coerce_epoch(bad)


def test_update_memory_accepts_scientific_notation_epoch(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from core.tools.builtin.memory_tools import remember, update_memory

    remember("The worker cap is three and that is a hard limit forever.", file="pernix.test_workers")
    from core.memory.store import get_memory_store

    entry = get_memory_store().search("worker cap", limit=1)[0].entry
    out = update_memory(
        "pernix.test_workers", f"{float(entry.epoch):.9e}", "The worker cap is max_concurrent_workers, currently 4."
    )
    assert out.startswith("UPDATED file=pernix.test_workers")


def test_update_memory_rejects_lossy_epoch(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from core.tools.builtin.memory_tools import update_memory

    out = update_memory("pernix.test_workers", "1.5", "x")
    assert out.startswith("NOT UPDATED — epoch must be")


# ---------------------------------------------------------------------------
# Write verdicts — a leading SAVED/NOT SAVED token plus a separate VERIFY
# read-back token. Session 2026-08: a dedup refusal was neither "Error" nor
# "Saved", and the model reported a save that never landed.
# ---------------------------------------------------------------------------


def test_remember_verifies_read_back(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from core.memory.store import get_memory_store
    from core.tools.builtin.memory_tools import remember

    content = "The heartbeat interval is 90 seconds and the watchdog fires after three misses."
    out = remember(content, file="pernix.test_verify")
    assert out.startswith("SAVED file=pernix.test_verify epoch=")
    assert out.endswith(" VERIFY=OK")

    epoch = int(out.split("epoch=")[1].split()[0])
    assert get_memory_store().get_entry("pernix.test_verify", epoch).content == content


def test_remember_duplicate_is_not_saved_and_names_the_supersede_call(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from core.tools.builtin.memory_tools import remember

    content = "The nightly backup runs at 03:00 UTC and retains fourteen daily snapshots."
    first = remember(content, file="pernix.test_dup")
    assert first.startswith("SAVED file=")

    second = remember(content, file="pernix.test_dup")
    assert second.startswith("NOT SAVED — duplicate of pernix.test_dup@")
    assert "update_memory(file='pernix.test_dup', epoch=" in second
    assert "SAVED file=" not in second


def test_remember_reports_missing_read_back(tmp_path, monkeypatch):
    """The write path claimed success but nothing is on disk — never SAVED."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from core.memory.store import MemoryStore
    from core.tools.builtin.memory_tools import remember

    monkeypatch.setattr(MemoryStore, "get_entry", lambda self, f, e: None)
    out = remember("A finding long enough to clear the dedup floor without tripping it.")
    assert out.startswith("NOT SAVED — VERIFY=MISSING: write did not land")


def test_remember_reports_mismatched_read_back(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from core.memory.format import MemoryEntry
    from core.memory.store import MemoryStore
    from core.tools.builtin.memory_tools import remember

    monkeypatch.setattr(
        MemoryStore,
        "get_entry",
        lambda self, f, e: MemoryEntry(file_name=f, content="something else entirely", epoch=e),
    )
    out = remember("A finding long enough to clear the dedup floor without tripping it.")
    assert out.startswith("SAVED file=")
    assert "VERIFY=MISMATCH — stored content differs from what you sent" in out
    assert 'stored: "something else entirely"' in out


def test_remember_sanitized_content_is_not_a_mismatch(tmp_path, monkeypatch):
    """add_entry rewrites bare `---` rules; that is storage, not divergence."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from core.tools.builtin.memory_tools import remember

    out = remember(
        "Deployment steps for the box:\n---\nBuild, then compose up on the host.",
        file="pernix.test_sanitize",
    )
    assert out.endswith("VERIFY=OK")


def test_update_memory_verdicts(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from core.memory.store import get_memory_store
    from core.tools.builtin.memory_tools import remember, update_memory

    remember("The queue drains oldest-first and never reorders on retry.", file="pernix.test_upd")
    epoch = get_memory_store().search("queue drains", limit=1)[0].entry.epoch

    out = update_memory("pernix.test_upd", epoch, "The queue drains newest-first under backpressure.")
    assert out == f"UPDATED file=pernix.test_upd epoch={epoch} VERIFY=OK"

    missing = update_memory("pernix.test_upd", epoch + 99, "anything")
    assert missing.startswith("NOT UPDATED — no entry with epoch=")

    no_file = update_memory("pernix.test_absent", epoch, "anything")
    assert no_file.startswith("NOT UPDATED — memory file 'pernix.test_absent' not found")

    assert update_memory("", epoch, "x") == "NOT UPDATED — file and content are required"


def test_forget_verdicts(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from core.memory.store import get_memory_store
    from core.tools.builtin.memory_tools import forget, remember

    remember("The canary suite caps at twenty-four entries before auto-admission stops.", file="pernix.test_del")
    epoch = get_memory_store().search("canary suite caps", limit=1)[0].entry.epoch

    out = forget("pernix.test_del", epoch)
    assert out == f"DELETED file=pernix.test_del epoch={epoch} VERIFY=OK"
    assert get_memory_store().get_entry("pernix.test_del", epoch) is None

    again = forget("pernix.test_del", epoch)
    assert again.startswith("NOT DELETED — no entry with epoch=")


def test_forget_reports_entry_still_present(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from core.memory.format import MemoryEntry
    from core.memory.store import MemoryStore, get_memory_store
    from core.tools.builtin.memory_tools import forget, remember

    remember("The reflect pass runs after every third assistant turn in long sessions.", file="pernix.test_ghost")
    epoch = get_memory_store().search("reflect pass runs", limit=1)[0].entry.epoch

    monkeypatch.setattr(
        MemoryStore,
        "get_entry",
        lambda self, f, e: MemoryEntry(file_name=f, content="still here", epoch=e),
    )
    out = forget("pernix.test_ghost", epoch)
    assert out.startswith("NOT DELETED — VERIFY=STILL-PRESENT")


def test_verdicts_never_hedge(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from core.tools.builtin.memory_tools import forget, remember, update_memory

    outs = [
        remember("A durable finding about the scheduler that is long enough to be deduped."),
        remember("A durable finding about the scheduler that is long enough to be deduped."),
        remember(""),
        update_memory("pernix.nope", 1, "x"),
        forget("pernix.nope", 1),
    ]
    for out in outs:
        assert "PARTIALLY" not in out.upper()
        assert "MOSTLY" not in out.upper()
        assert out.split()[0] in ("SAVED", "UPDATED", "DELETED", "NOT")
