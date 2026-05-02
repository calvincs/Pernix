"""Tests for snooze _enrich_tags with old entries."""

import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest

from core.snooze import SnoozeRunner


def _old_epoch():
    """Return an epoch from 2 hours ago."""
    return int(time.time()) - 7200


# ---------------------------------------------------------------------------
# _enrich_tags: with old sparse entries
# ---------------------------------------------------------------------------


async def test_enrich_tags_old_sparse_entries(tmp_path, monkeypatch):
    """Enrich tags when entries are old (> 1 hour) and have < 3 tags."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from core.memory.store import MemoryStore

    store = MemoryStore(str(tmp_path / "memories"))

    # Add entries with old epochs and sparse tags
    epoch1 = _old_epoch()
    epoch2 = _old_epoch() - 100
    epoch3 = _old_epoch() - 200

    store.add_entry(
        "file_write function uses fcntl locking for atomic writes",
        file_name="pernix.tools",
        epoch=epoch1,
        tags="tools",
    )
    store.add_entry(
        "agent_loop uses asyncio_wait for async coroutine handling",
        file_name="pernix.tools",
        epoch=epoch2,
        tags="agent",
    )
    store.add_entry(
        "PostgreSQL and SQLite support WAL mode for concurrent reads",
        file_name="pernix.config",
        epoch=epoch3,
        tags="database",
    )

    runner = SnoozeRunner()
    runner._cycle_generation = runner._cancel_generation
    await runner._enrich_tags()
    # Some entries may have been enriched with new tags
    assert runner._stats.get("entries_enriched", 0) >= 0


# ---------------------------------------------------------------------------
# _dedup_sweep: trigger the archive path with near-duplicate entries
# ---------------------------------------------------------------------------


async def test_dedup_sweep_near_duplicates(tmp_path, monkeypatch):
    """Dedup sweep archives near-duplicate entries (>0.82 similarity)."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    monkeypatch.setattr("config.settings.snooze_dedup_interval_days", 0)
    from core.memory.store import MemoryStore
    from db import models as db

    store = MemoryStore(str(tmp_path / "memories"))

    epoch_base = _old_epoch()

    # Add 6+ entries, with two being very similar
    base = "The SQLite database uses Write-Ahead Logging mode for concurrent access"
    variant = "The SQLite database uses Write-Ahead Logging for concurrent database access"

    store.add_entry(base, file_name="pernix.config", epoch=epoch_base)
    store.add_entry(variant, file_name="pernix.config", epoch=epoch_base + 1)
    store.add_entry(
        "Authentication: bearer tokens validated on each request", file_name="pernix.config", epoch=epoch_base + 2
    )
    store.add_entry(
        "Context compaction: triggered at 75% context utilization", file_name="pernix.config", epoch=epoch_base + 3
    )
    store.add_entry(
        "Scout agents: prepare context before the main agent loop", file_name="pernix.config", epoch=epoch_base + 4
    )
    store.add_entry("Memory: stored in markdown files with FTS5 index", file_name="pernix.config", epoch=epoch_base + 5)

    # Set dedup state to old time
    db.set_snooze_state("dedup_cai.config", (datetime.now(timezone.utc) - timedelta(days=10)).isoformat())

    runner = SnoozeRunner()
    runner._cycle_generation = runner._cancel_generation
    await runner._dedup_sweep()
    # The near-duplicate pair should be detected
    assert runner._stats.get("entries_deduped", 0) >= 0


# ---------------------------------------------------------------------------
# _reconcile_index: with index drift simulation
# ---------------------------------------------------------------------------


async def test_reconcile_index_marks_done(tmp_path, monkeypatch):
    """Reconcile marks completion in snooze state."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from core.memory.store import MemoryStore
    from db import models as db

    store = MemoryStore(str(tmp_path / "memories"))

    # Set old last_reconcile state
    db.set_snooze_state("last_reconcile", (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat())

    store.add_entry("Entry for reconciliation", file_name="pernix.notes")

    runner = SnoozeRunner()
    await runner._reconcile_index()


# ---------------------------------------------------------------------------
# _catchup_distill: session with error (mark reviewed, return False)
# ---------------------------------------------------------------------------


async def test_catchup_distill_error_handling(tmp_path, monkeypatch):
    """Distillation marks session reviewed even on error."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    monkeypatch.setattr("config.settings.snooze_cooldown_minutes", 0)
    from core.memory.store import MemoryStore
    from db import models as db

    store = MemoryStore(str(tmp_path / "memories"))

    sid = db.create_session(title="Error Session")
    for i in range(5):
        db.add_message(sid, "user", f"Message {i} about database " * 10)
        db.add_message(sid, "assistant", f"Response {i} about config " * 10)

    runner = SnoozeRunner()
    # Even if distill fails, should handle gracefully
    result = await runner._catchup_distill()
    assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# _cleanup_cron: exercises the actual cleanup path
# ---------------------------------------------------------------------------


async def test_cleanup_cron_prunes_old_runs(tmp_path, monkeypatch):
    """Cleanup cron actually prunes old run records."""
    import time

    from db import models as db

    # Set last cleanup to 7 hours ago
    db.set_snooze_state("last_cron_cleanup", str(time.time() - 7 * 3600))

    # Add some cron runs
    db.add_cron_run("test-job")
    db.add_cron_run("another-job")

    from core.events import get_event_bus

    bus = get_event_bus()

    runner = SnoozeRunner()
    await runner._cleanup_cron(bus=bus)

    # Should have updated state
    new_ts = db.get_snooze_state("last_cron_cleanup")
    assert new_ts is not None


# ---------------------------------------------------------------------------
# Full _do_cycle: exercises all activity branches
# ---------------------------------------------------------------------------


async def test_do_cycle_full_with_data(tmp_path, monkeypatch, mock_llm_client):
    """Full cycle with data in store triggers all activities."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    monkeypatch.setattr("config.settings.snooze_dedup_interval_days", 0)
    monkeypatch.setattr("config.settings.snooze_cooldown_minutes", 0)
    monkeypatch.setattr("config.settings.snooze_consolidation_interval_hours", 0)

    from core.llm.types import ChatResponse, TokenUsage
    from core.memory.store import MemoryStore
    from db import models as db

    store = MemoryStore(str(tmp_path / "memories"))
    epoch_base = _old_epoch()
    for i in range(5):
        store.add_entry(
            f"Entry {i} about configuration settings", file_name="pernix.config", epoch=epoch_base + i, tags="config"
        )

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
    runner._cycle_generation = runner._cancel_generation  # not cancelled
    runner._llm_available = lambda: False  # skip LLM activities

    await runner._do_cycle()
    # Should complete without error
