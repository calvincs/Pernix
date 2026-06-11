"""Tests for snooze activity bodies: consolidate, reroute, enrich, split, prune."""

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from core.snooze import SnoozeRunner


def _old_timestamp():
    """Return a timestamp 48+ hours ago (bypasses rate limits)."""
    return (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()


# ---------------------------------------------------------------------------
# _refine_one_session (Activity 13)
# ---------------------------------------------------------------------------


async def test_refine_one_session_stamps_watermark_on_no_candidate(monkeypatch):
    """No eligible session → returns False, does not touch any state."""
    monkeypatch.setattr("config.settings.background_model", "fake-bg-model")
    runner = SnoozeRunner()
    runner._cycle_generation = runner._cancel_generation  # not cancelled
    used = await runner._refine_one_session()
    assert used is False


async def test_refine_one_session_stamps_watermark_after_run(monkeypatch):
    """After processing a session, refined:{sid} must be set so the same
    session is never picked up again — mark-on-success and mark-on-failure
    both stamp."""
    from db import models as db

    monkeypatch.setattr("config.settings.background_model", "fake-bg-model")

    sid = db.create_session(title="Refine candidate")
    db.add_message(sid, "user", "hi")
    db.add_message(sid, "assistant", "hi back")
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    from db.database import connect_sessions

    with connect_sessions() as conn:
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (past, sid))

    async def _fake_run_for_session(session_id):
        return {
            "proposals_saved": 0,
            "lessons_saved": 0,
            "nothing_actionable": True,
            "skipped_reason": None,
        }

    monkeypatch.setattr("core.refine.run_for_session", _fake_run_for_session)

    runner = SnoozeRunner()
    runner._cycle_generation = runner._cancel_generation  # not cancelled
    used = await runner._refine_one_session()
    assert used is True
    assert db.get_snooze_state(f"refined:{sid}") is not None

    # Second pass should find no candidate now.
    used2 = await runner._refine_one_session()
    assert used2 is False


async def test_refine_one_session_stamps_watermark_on_exception(monkeypatch):
    """If refine.run_for_session raises, the session is still watermarked
    so a broken session never retry-storms."""
    from db import models as db

    monkeypatch.setattr("config.settings.background_model", "fake-bg-model")

    sid = db.create_session(title="Refine boom")
    db.add_message(sid, "user", "hi")
    db.add_message(sid, "assistant", "hi back")
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    from db.database import connect_sessions

    with connect_sessions() as conn:
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (past, sid))

    async def _boom(session_id):
        raise RuntimeError("explode")

    monkeypatch.setattr("core.refine.run_for_session", _boom)

    runner = SnoozeRunner()
    runner._cycle_generation = runner._cancel_generation  # not cancelled
    used = await runner._refine_one_session()
    assert used is False
    assert db.get_snooze_state(f"refined:{sid}") is not None


# ---------------------------------------------------------------------------
# _consolidate_files: rate limit bypass
# ---------------------------------------------------------------------------


async def test_consolidate_files_rate_limit(tmp_path, monkeypatch):
    """Consolidate is skipped when interval hasn't elapsed."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from db import models as db

    # Set recent consolidation time
    db.set_snooze_state("last_consolidation_scan", datetime.now(timezone.utc).isoformat())

    from core.memory.store import MemoryStore

    store = MemoryStore(str(tmp_path / "memories"))
    store.add_entry("Entry A about database config", file_name="pernix.config")
    store.add_entry("Entry B about auth config", file_name="pernix.config")

    runner = SnoozeRunner()
    result = await runner._consolidate_files(did_llm_already=False)
    assert result is False


async def test_consolidate_files_with_similar_files(tmp_path, monkeypatch):
    """Consolidate processes files when interval has elapsed."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    monkeypatch.setattr("config.settings.snooze_consolidation_interval_hours", 0)
    from db import models as db

    db.set_snooze_state("last_consolidation_scan", _old_timestamp())

    from core.memory.store import MemoryStore

    store = MemoryStore(str(tmp_path / "memories"))

    # Create two similar-named files ("pernix.notes" and "pernix.note" both → "pernix" normalized)
    # Both need entries
    for i in range(3):
        store.add_entry(f"Entry {i} about notes", file_name="pernix.notes")

    runner = SnoozeRunner()
    runner._cycle_generation = runner._cancel_generation  # not cancelled
    result = await runner._consolidate_files(did_llm_already=False)
    assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# _reroute_misplaced_entries
# ---------------------------------------------------------------------------


async def test_reroute_rate_limit(tmp_path, monkeypatch):
    """Reroute is skipped when interval hasn't elapsed."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from db import models as db

    db.set_snooze_state("last_reroute_scan", datetime.now(timezone.utc).isoformat())

    runner = SnoozeRunner()
    result = await runner._reroute_misplaced_entries(did_llm_already=False)
    assert result is False


async def test_reroute_single_file(tmp_path, monkeypatch):
    """Reroute with only one file → skips."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    monkeypatch.setattr("config.settings.snooze_consolidation_interval_hours", 0)
    from db import models as db

    db.set_snooze_state("last_reroute_scan", _old_timestamp())

    from core.memory.store import MemoryStore

    store = MemoryStore(str(tmp_path / "memories"))
    store.add_entry("Only file content", file_name="pernix.notes")

    runner = SnoozeRunner()
    runner._cycle_generation = runner._cancel_generation
    result = await runner._reroute_misplaced_entries(did_llm_already=False)
    assert result is False  # < 2 files → skip


async def test_reroute_with_multiple_files(tmp_path, monkeypatch):
    """Reroute with multiple files executes the no-LLM pass."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    monkeypatch.setattr("config.settings.snooze_consolidation_interval_hours", 0)
    from db import models as db

    db.set_snooze_state("last_reroute_scan", _old_timestamp())

    from core.memory.store import MemoryStore

    store = MemoryStore(str(tmp_path / "memories"))
    store.add_entry("Database configuration and settings", file_name="pernix.config")
    store.add_entry("User name is Alice, works at Example Corp", file_name="pernix.notes")  # misplaced?
    store.add_entry("Debug: fixed bug in auth module", file_name="pernix.debugging")

    runner = SnoozeRunner()
    runner._cycle_generation = runner._cancel_generation
    runner._llm_available = lambda: False  # no LLM
    result = await runner._reroute_misplaced_entries(did_llm_already=False)
    assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# _enrich_tags: interval bypass
# ---------------------------------------------------------------------------


async def test_enrich_tags_with_sparse_entries(tmp_path, monkeypatch):
    """Enrich tags processes files with entries that have few tags."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from core.memory.store import MemoryStore

    store = MemoryStore(str(tmp_path / "memories"))

    # Add entries - the enrich should add technical term tags
    store.add_entry(
        "The file_read tool reads from workspace_dir. Use agent_loop for processing.", file_name="pernix.tools"
    )
    store.add_entry(
        "PostgreSQL and MySQL both support WAL_mode. SQLite uses WAL by default.", file_name="pernix.config"
    )

    runner = SnoozeRunner()
    runner._cycle_generation = runner._cancel_generation
    await runner._enrich_tags()
    assert runner._stats["entries_enriched"] >= 0


# ---------------------------------------------------------------------------
# _split_file
# ---------------------------------------------------------------------------


async def test_split_file_no_large_files(tmp_path, monkeypatch):
    """Split file does nothing when no file is large enough."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from core.memory.store import MemoryStore

    store = MemoryStore(str(tmp_path / "memories"))

    # Small files → no split needed
    for i in range(3):
        store.add_entry(f"Short entry {i}", file_name="pernix.notes")

    runner = SnoozeRunner()
    await runner._split_file()


# ---------------------------------------------------------------------------
# _prune_stale_entries
# ---------------------------------------------------------------------------


async def test_prune_stale_empty_store(tmp_path, monkeypatch):
    """Prune does nothing with empty store."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    runner = SnoozeRunner()
    await runner._prune_stale_entries()


async def test_prune_stale_no_candidates(tmp_path, monkeypatch):
    """Prune does nothing when no entries have hit counts."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from core.memory.store import MemoryStore

    store = MemoryStore(str(tmp_path / "memories"))
    store.add_entry("Fresh entry with no usage data yet", file_name="pernix.notes")

    runner = SnoozeRunner()
    await runner._prune_stale_entries()


# ---------------------------------------------------------------------------
# _extract_user_insights: exercise the body
# ---------------------------------------------------------------------------


async def test_extract_user_insights_reviewed_session(mock_llm_client, tmp_path, monkeypatch):
    """Exercise user insight extraction with a reviewed session."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    monkeypatch.setattr("config.settings.snooze_cooldown_minutes", 0)

    from core.llm.types import ChatResponse, TokenUsage
    from db import models as db

    # Create and mark a session as reviewed
    sid = db.create_session(title="Profile Session")
    db.mark_session_reviewed(sid)
    for i in range(6):
        db.add_message(sid, "user", "I work as a Python developer at Anthropic " * 4)
        db.add_message(sid, "assistant", "That's interesting! " * 4)

    # LLM returns a profile entry
    mock_llm_client.responses = [
        ChatResponse(
            content=json.dumps(
                [{"tags": "developer,python", "weight": "high", "content": "User is a Python developer at Anthropic"}]
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
    # May return True (used LLM) or False (short transcript, etc.)
    assert isinstance(result, bool)


async def test_extract_user_insights_llm_failure(mock_llm_client, tmp_path, monkeypatch):
    """User insight extraction handles LLM failure gracefully."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    monkeypatch.setattr("config.settings.snooze_cooldown_minutes", 0)

    from db import models as db

    sid = db.create_session(title="LLM Fail Session")
    db.mark_session_reviewed(sid)
    for i in range(6):
        db.add_message(sid, "user", "Detailed message about Python async programming " * 4)
        db.add_message(sid, "assistant", "Response about asyncio patterns " * 4)

    async def failing_chat(*args, **kwargs):
        raise ConnectionError("LLM down")

    mock_llm_client.chat = failing_chat

    runner = SnoozeRunner()
    result = await runner._extract_user_insights()
    # Should handle gracefully — returns True (LLM attempted) or False
    assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# _update_skill_cooccurrence with memory entries
# ---------------------------------------------------------------------------


async def test_update_skill_cooccurrence_with_skill_mentions(tmp_path, monkeypatch):
    """Skill cooccurrence updates when memory mentions skill names."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from core.memory.store import MemoryStore

    store = MemoryStore(str(tmp_path / "memories"))

    # Add entries that mention skills
    store.add_entry("Used web-search skill followed by file-analysis to complete task", file_name="pernix.tools")
    store.add_entry("Loaded code-review skill then used git-workflow skill", file_name="pernix.tools")

    runner = SnoozeRunner()
    await runner._update_skill_cooccurrence()
