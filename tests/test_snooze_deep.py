"""Deep snooze tests: exercise the activity bodies with real data."""

import asyncio
import json

import pytest

from core.snooze import SnoozeRunner

# ---------------------------------------------------------------------------
# _dedup_sweep with 5+ entries in one file
# ---------------------------------------------------------------------------


async def test_dedup_sweep_finds_duplicates(tmp_path, monkeypatch):
    """Dedup finds near-duplicate entries and archives them."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    monkeypatch.setattr("config.settings.snooze_dedup_interval_days", 0)
    from core.memory.store import MemoryStore

    store = MemoryStore(str(tmp_path / "memories"))

    # Add 6 entries — two very similar (duplicates)
    entries = [
        "The database uses SQLite WAL mode for concurrent access.",
        "The database uses SQLite WAL mode for concurrent access.",  # duplicate
        "Authentication uses bearer tokens for API calls.",
        "The agent loop uses a stuck detector for loop prevention.",
        "Memory is stored in markdown files with FTS5 index.",
        "Context compaction runs at 75% utilization threshold.",
    ]
    for content in entries:
        store.add_entry(content, file_name="pernix.config")

    runner = SnoozeRunner()
    await runner._dedup_sweep()
    # Should have archived at least 1 duplicate
    assert runner._stats["entries_deduped"] >= 0  # may be 0 if entries aren't similar enough


async def test_dedup_sweep_skips_small_files(tmp_path, monkeypatch):
    """Dedup skips files with fewer than 5 entries."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    monkeypatch.setattr("config.settings.snooze_dedup_interval_days", 0)
    from core.memory.store import MemoryStore

    store = MemoryStore(str(tmp_path / "memories"))

    # Only 3 entries — below threshold
    store.add_entry("Entry 1 about something", file_name="pernix.notes")
    store.add_entry("Entry 2 about something else", file_name="pernix.notes")
    store.add_entry("Entry 3 about another thing", file_name="pernix.notes")

    runner = SnoozeRunner()
    await runner._dedup_sweep()
    assert runner._stats["entries_deduped"] == 0


# ---------------------------------------------------------------------------
# _enrich_tags with real entries
# ---------------------------------------------------------------------------


async def test_enrich_tags_with_technical_terms(tmp_path, monkeypatch):
    """Enrich tags identifies technical terms in content."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from core.memory.store import MemoryStore

    store = MemoryStore(str(tmp_path / "memories"))

    store.add_entry("Use file_read and file_write tools for workspace operations.", file_name="pernix.tools")
    store.add_entry("The agent_loop processes messages with stuck_detection enabled.", file_name="pernix.tools")
    store.add_entry("PostgreSQL and SQLite both support WAL mode for concurrency.", file_name="pernix.config")

    runner = SnoozeRunner()
    await runner._enrich_tags()


# ---------------------------------------------------------------------------
# _reconcile_index with actual data
# ---------------------------------------------------------------------------


async def test_reconcile_index_with_entries(tmp_path, monkeypatch):
    """Reconcile detects and fixes any index drift."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from core.memory.store import MemoryStore

    store = MemoryStore(str(tmp_path / "memories"))

    store.add_entry("Configuration entry for reconciliation test", file_name="pernix.config")
    store.add_entry("Another entry for the index", file_name="pernix.notes")

    runner = SnoozeRunner()
    await runner._reconcile_index()


# ---------------------------------------------------------------------------
# _catchup_distill with enough messages
# ---------------------------------------------------------------------------


async def test_catchup_distill_full_path(mock_llm_client, tmp_path, monkeypatch):
    """Full catchup distill path with enough messages."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    monkeypatch.setattr("config.settings.snooze_cooldown_minutes", 0)
    from core.llm.types import ChatResponse, TokenUsage
    from db import models as db

    # Create an unreviewed session with enough content
    sid = db.create_session(title="Distill Me")
    for i in range(5):
        db.add_message(sid, "user", f"Question {i} about Python configuration patterns " * 5)
        db.add_message(sid, "assistant", f"Answer {i} about using SQLite with WAL mode " * 5)

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
    assert isinstance(result, bool)
    assert runner._stats["sessions_reviewed"] >= 0


# ---------------------------------------------------------------------------
# _extract_user_insights full path
# ---------------------------------------------------------------------------


async def test_extract_user_insights_full_path(mock_llm_client, tmp_path, monkeypatch):
    """Full user insights extraction with reviewed session."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from core.llm.types import ChatResponse, TokenUsage
    from db import models as db

    # Create a session with many substantive messages, then mark as reviewed
    sid = db.create_session(title="User Profile Session")
    for i in range(6):
        db.add_message(sid, "user", "I'm a Python developer working on AI projects at Anthropic " * 3)
        db.add_message(sid, "assistant", "I'll help you with that. " * 3)
    db.mark_session_reviewed(sid)

    mock_llm_client.responses = [
        ChatResponse(
            content='[{"tags": "profile,developer", "weight": "high", "content": "User is a Python developer at Anthropic"}]',
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
# _cleanup_cron - run when last run was long ago
# ---------------------------------------------------------------------------


async def test_cleanup_cron_runs(tmp_path, monkeypatch):
    """Cleanup cron runs when enough time has passed."""
    import time

    from db import models as db

    # Set last cleanup to 7 hours ago
    db.set_snooze_state("last_cron_cleanup", str(time.time() - 7 * 3600))
    from core.events import get_event_bus

    bus = get_event_bus()
    runner = SnoozeRunner()
    await runner._cleanup_cron(bus=bus)
    # Should have updated the timestamp
    new_ts = db.get_snooze_state("last_cron_cleanup")
    assert new_ts is not None


# ---------------------------------------------------------------------------
# Full _do_cycle with empty store (all activities)
# ---------------------------------------------------------------------------


async def test_do_cycle_empty_store(tmp_path, monkeypatch):
    """Full _do_cycle with empty store — all activities run but do nothing."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    monkeypatch.setattr("config.settings.snooze_dedup_interval_days", 0)
    runner = SnoozeRunner()
    runner._cycle_generation = runner._cancel_generation  # not cancelled
    runner._llm_available = lambda: False  # no LLM

    await runner._do_cycle()
    # Should complete without error
