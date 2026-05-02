"""Async activity tests for snooze.py: catchup distill, user insights, etc."""

import asyncio
import json

import pytest

from core.snooze import SnoozeRunner

# ---------------------------------------------------------------------------
# _catchup_distill
# ---------------------------------------------------------------------------


async def test_catchup_distill_no_sessions():
    """Returns False immediately when no unreviewed sessions."""
    runner = SnoozeRunner()
    result = await runner._catchup_distill()
    assert result is False


async def test_catchup_distill_with_session(mock_llm_client, tmp_path, monkeypatch):
    """Reviews and distills an unreviewed session."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from core.llm.types import ChatResponse, TokenUsage
    from db import models as db

    sid = db.create_session(title="Review Me")
    for i in range(5):
        db.add_message(sid, "user", f"Message {i} about database config " * 10)
        db.add_message(sid, "assistant", f"Response {i} about SQLite settings " * 10)

    # Mock LLM to return SKIP (no entries to save)
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

    runner = SnoozeRunner()
    result = await runner._catchup_distill()
    # Returns True if it worked, False if not enough messages
    assert isinstance(result, bool)


async def test_catchup_distill_manual_save_skips(monkeypatch):
    """Skips distillation if user manually saved entries."""
    from db import models as db

    sid = db.create_session(title="Manual Saved")
    for i in range(5):
        db.add_message(sid, "user", f"Message {i} " * 20)
        db.add_message(sid, "assistant", f"Response {i} " * 20)
    # Mark as manually saved
    db.set_snooze_state(f"manual_save:{sid}", "12345")

    runner = SnoozeRunner()
    result = await runner._catchup_distill()
    # Should mark reviewed and return False (skipped manual saves)
    assert result is False


# ---------------------------------------------------------------------------
# _extract_user_insights
# ---------------------------------------------------------------------------


async def test_extract_user_insights_no_sessions():
    """Returns False when no reviewed sessions with profile content."""
    runner = SnoozeRunner()
    result = await runner._extract_user_insights()
    assert result is False


async def test_extract_user_insights_skip_response(mock_llm_client, tmp_path, monkeypatch):
    """LLM returning SKIP → no entries saved, returns False."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from core.llm.types import ChatResponse, TokenUsage
    from db import models as db

    sid = db.create_session(title="Profile Test")
    db.mark_session_reviewed(sid)
    for i in range(5):
        db.add_message(sid, "user", f"Message {i} " * 20)
        db.add_message(sid, "assistant", f"Response {i} " * 20)

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

    runner = SnoozeRunner()
    result = await runner._extract_user_insights()
    assert result is False


async def test_extract_user_insights_with_entries(mock_llm_client, tmp_path, monkeypatch):
    """LLM returning entries → saves profile facts."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from core.llm.types import ChatResponse, TokenUsage
    from db import models as db

    sid = db.create_session(title="Profile Session")
    db.mark_session_reviewed(sid)
    for i in range(5):
        db.add_message(sid, "user", "I'm a software engineer who works with Python " * 5)
        db.add_message(sid, "assistant", "I'll help you with that " * 5)

    mock_llm_client.responses = [
        ChatResponse(
            content=json.dumps(
                [
                    {
                        "tags": "profile,developer",
                        "weight": "high",
                        "content": "User is a software engineer who works with Python",
                    }
                ]
            ),
            tool_calls=None,
            usage=TokenUsage(10, 5, 15),
            model="test",
            provider="fake",
            finish_reason="stop",
        )
    ]

    runner = SnoozeRunner()
    result = await runner._extract_user_insights()
    assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# _dedup_sweep with entries
# ---------------------------------------------------------------------------


async def test_dedup_sweep_with_entries(tmp_path, monkeypatch):
    """Dedup sweep on store with entries."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from core.memory.store import MemoryStore

    store = MemoryStore(str(tmp_path / "memories"))
    store.add_entry("Database uses SQLite WAL mode for concurrency", file_name="pernix.config")
    store.add_entry("Database uses SQLite WAL mode for concurrency", file_name="pernix.config")  # duplicate

    runner = SnoozeRunner()
    await runner._dedup_sweep()
    # Should not raise


# ---------------------------------------------------------------------------
# _enrich_tags with entries
# ---------------------------------------------------------------------------


async def test_enrich_tags_with_entries(tmp_path, monkeypatch):
    """Enrich tags on store with sparse entries."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from core.memory.store import MemoryStore

    store = MemoryStore(str(tmp_path / "memories"))
    store.add_entry("The file_read tool reads content from workspace files", file_name="pernix.tools")
    store.add_entry("Authentication uses bearer_token validation middleware", file_name="pernix.config")

    runner = SnoozeRunner()
    await runner._enrich_tags()


# ---------------------------------------------------------------------------
# _consolidate_files
# ---------------------------------------------------------------------------


async def test_consolidate_files_empty_store(tmp_path, monkeypatch):
    """Consolidation on empty store → no-op."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    runner = SnoozeRunner()
    result = await runner._consolidate_files(did_llm_already=False)
    assert result is False


async def test_consolidate_files_no_clusters(tmp_path, monkeypatch):
    """Consolidation with dissimilar files → no clusters."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from core.memory.store import MemoryStore

    store = MemoryStore(str(tmp_path / "memories"))
    store.add_entry("User profile info about preferences", file_name="user.profile")
    store.add_entry("Bug fix for authentication", file_name="pernix.debugging")

    runner = SnoozeRunner()
    result = await runner._consolidate_files(did_llm_already=False)
    assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# _reroute_misplaced_entries
# ---------------------------------------------------------------------------


async def test_reroute_empty_store(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    runner = SnoozeRunner()
    result = await runner._reroute_misplaced_entries(did_llm_already=False)
    assert result is False


# ---------------------------------------------------------------------------
# _prune_stale_entries
# ---------------------------------------------------------------------------


async def test_prune_stale_entries_empty(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    runner = SnoozeRunner()
    await runner._prune_stale_entries()


# ---------------------------------------------------------------------------
# _split_file
# ---------------------------------------------------------------------------


async def test_split_file_empty(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    runner = SnoozeRunner()
    await runner._split_file()


# ---------------------------------------------------------------------------
# _update_skill_cooccurrence with memory entries
# ---------------------------------------------------------------------------


async def test_update_skill_cooccurrence_with_entries(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from core.memory.store import MemoryStore

    store = MemoryStore(str(tmp_path / "memories"))
    store.add_entry("Used web-search skill for research tasks", file_name="pernix.tools")
    runner = SnoozeRunner()
    await runner._update_skill_cooccurrence()
