"""Tests for core/memory/distill.py: _parse_entries and distill_session."""

import json

import pytest

from core.llm.types import ChatResponse, TokenUsage
from core.memory.distill import _parse_entries, distill_session

# ---------------------------------------------------------------------------
# _parse_entries
# ---------------------------------------------------------------------------


def test_parse_entries_valid_array():
    text = '[{"type": "note", "content": "found something", "file": "pernix.notes"}]'
    entries = _parse_entries(text)
    assert len(entries) == 1
    assert entries[0]["content"] == "found something"


def test_parse_entries_valid_dict():
    text = '{"type": "note", "content": "single entry", "file": "pernix.notes"}'
    entries = _parse_entries(text)
    assert len(entries) == 1


def test_parse_entries_with_fences():
    text = '```json\n[{"type": "note", "content": "fenced", "file": "pernix.notes"}]\n```'
    entries = _parse_entries(text)
    assert len(entries) == 1


def test_parse_entries_invalid_json():
    entries = _parse_entries("not json at all")
    assert entries == []


def test_parse_entries_empty():
    entries = _parse_entries("")
    assert entries == []


def test_parse_entries_multiple():
    data = [
        {"type": "note", "content": "entry1", "file": "pernix.notes"},
        {"type": "decision", "content": "entry2", "file": "pernix.decisions"},
    ]
    text = json.dumps(data)
    entries = _parse_entries(text)
    assert len(entries) == 2


# ---------------------------------------------------------------------------
# distill_session (async integration)
# ---------------------------------------------------------------------------


async def test_distill_session_skip(mock_llm_client, tmp_path, monkeypatch):
    """LLM returning SKIP means no entries saved."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    monkeypatch.setattr("config.settings.memory_recall", True)
    from db import models as db

    sid = db.create_session(title="Distill Test")
    for i in range(5):
        db.add_message(sid, "user", f"Message {i} " * 50)
        db.add_message(sid, "assistant", f"Response {i} " * 50)
    messages = db.get_messages(sid)

    mock_llm_client.responses = [
        ChatResponse(
            content="SKIP",
            tool_calls=None,
            usage=TokenUsage(10, 5, 15),
            model="test",
            provider="fake",
            finish_reason="stop",
        )
    ]

    # Should complete without error
    await distill_session(sid, title="Test", messages=messages)


async def test_distill_session_saves_entries(mock_llm_client, tmp_path, monkeypatch):
    """LLM returning valid JSON entries → entries saved."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from db import models as db

    sid = db.create_session(title="Save Entries")
    for i in range(5):
        db.add_message(sid, "user", f"Question {i} about database configuration " * 10)
        db.add_message(sid, "assistant", f"Answer {i} with SQLite WAL mode details " * 10)
    messages = db.get_messages(sid)

    mock_llm_client.responses = [
        ChatResponse(
            content=json.dumps(
                [
                    {
                        "type": "note",
                        "content": "Database uses SQLite WAL mode",
                        "file": "pernix.config",
                        "tags": "database",
                        "weight": "normal",
                    },
                ]
            ),
            tool_calls=None,
            usage=TokenUsage(10, 20, 30),
            model="test",
            provider="fake",
            finish_reason="stop",
        )
    ]

    await distill_session(sid, title="Test", messages=messages)


async def test_distill_session_short_transcript(mock_llm_client, tmp_path, monkeypatch):
    """Transcripts < 200 chars are skipped without LLM call."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from db import models as db

    sid = db.create_session(title="Short")
    messages = [
        {"role": "user", "content": "hi", "id": 1},
        {"role": "assistant", "content": "ok", "id": 2},
    ]
    # Should return without calling LLM
    await distill_session(sid, title="Short", messages=messages)
    assert mock_llm_client.call_count == 0


async def test_distill_session_llm_error(mock_llm_client, tmp_path, monkeypatch):
    """LLM failure is handled gracefully."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from db import models as db

    sid = db.create_session(title="Error Test")
    for i in range(5):
        db.add_message(sid, "user", f"Message {i} " * 50)
        db.add_message(sid, "assistant", f"Response {i} " * 50)
    messages = db.get_messages(sid)

    async def failing_chat(*args, **kwargs):
        raise ConnectionError("LLM down")

    mock_llm_client.chat = failing_chat

    # Should not raise
    await distill_session(sid, title="Error", messages=messages)


# ---------------------------------------------------------------------------
# _is_saved — the store still returns "Saved to ...", the memory tools return
# the model-facing "SAVED file=... VERIFY=OK". Both count as a landed write.
# ---------------------------------------------------------------------------


def test_is_saved_accepts_both_shapes():
    from core.memory.distill import _is_saved

    assert _is_saved("Saved to pernix.notes (epoch=1777154774)")
    assert _is_saved("SAVED file=pernix.notes epoch=1777154774 VERIFY=OK")


def test_is_saved_rejects_refusals_and_supersedes():
    from core.memory.distill import _is_saved

    assert not _is_saved('Memory already contains similar content — entry skipped (duplicate of a@1: "x").')
    assert not _is_saved('NOT SAVED — duplicate of pernix.notes@1777154774: "x"')
    assert not _is_saved("NOT SAVED — VERIFY=MISSING: write did not land (no entry epoch=1 in a on read-back)")
    assert not _is_saved("Superseded pernix.notes@1777154774")
    assert not _is_saved("Error: Empty content")
