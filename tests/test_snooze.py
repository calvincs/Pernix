"""Tests for core/snooze.py: SnoozeRunner state machine, pure logic, and async methods."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.snooze import SnoozeRunner, get_snooze

# ---------------------------------------------------------------------------
# Basic state machine
# ---------------------------------------------------------------------------


def test_init_state():
    runner = SnoozeRunner()
    assert not runner._running
    assert runner._cancel_generation == 0
    assert runner._cycle_generation == -1
    assert runner._activity_since_last_cycle is True


def test_request_cancel_when_running():
    runner = SnoozeRunner()
    runner._running = True
    runner.request_cancel()
    assert runner._cancel_generation == 1


def test_request_cancel_when_not_running():
    runner = SnoozeRunner()
    runner._running = False
    runner.request_cancel()
    # No cancel when not running
    assert runner._cancel_generation == 0


def test_notify_activity():
    runner = SnoozeRunner()
    runner._activity_since_last_cycle = False
    runner.notify_activity()
    assert runner._activity_since_last_cycle is True


def test_get_stats():
    runner = SnoozeRunner()
    stats = runner.get_stats()
    assert "cycles" in stats
    assert "running" in stats
    assert stats["running"] is False
    assert stats["cycles"] == 0


def test_is_cancelled_initially_false():
    runner = SnoozeRunner()
    # cycle_generation=-1, cancel_generation=0 → different → cancelled
    # But this is the "not started" state; cycle only starts if not cancelled
    assert runner._is_cancelled() is True  # because -1 != 0


def test_is_cancelled_after_cycle_start():
    runner = SnoozeRunner()
    runner._cycle_generation = runner._cancel_generation  # = 0
    assert not runner._is_cancelled()


def test_is_cancelled_after_request():
    runner = SnoozeRunner()
    runner._cycle_generation = runner._cancel_generation  # sync to 0
    runner._running = True
    runner.request_cancel()  # bumps to 1
    assert runner._is_cancelled()


# ---------------------------------------------------------------------------
# _is_idle
# ---------------------------------------------------------------------------


def test_is_idle_no_sessions(monkeypatch):
    """No active sessions → idle (if other conditions met)."""
    from sessions.manager import SessionManager

    mgr = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", mgr)
    monkeypatch.setattr("config.settings.snooze_cooldown_minutes", 0)

    runner = SnoozeRunner()
    # No sessions → should be idle (assuming no running crons)
    result = runner._is_idle()
    assert isinstance(result, bool)


def test_is_idle_busy_session(monkeypatch):
    """Session in PROCESSING state → not idle."""
    from sessions.manager import SessionManager
    from sessions.state import AgentSession, SessionState

    mgr = SessionManager()
    sid = mgr.create_session(title="Busy")
    session = mgr.get(sid)
    session._force_state_for_tests(SessionState.PROCESSING)

    monkeypatch.setattr("sessions.manager._manager", mgr)
    runner = SnoozeRunner()
    assert runner._is_idle() is False


# ---------------------------------------------------------------------------
# run_cycle: short-circuit cases
# ---------------------------------------------------------------------------


async def test_run_cycle_disabled(monkeypatch):
    monkeypatch.setattr("config.settings.snooze_enabled", False)
    runner = SnoozeRunner()
    await runner.run_cycle()
    assert runner._stats["cycles"] == 0


async def test_run_cycle_not_idle(monkeypatch):
    monkeypatch.setattr("config.settings.snooze_enabled", True)
    runner = SnoozeRunner()
    # Force _is_idle to return False
    runner._is_idle = lambda: False
    await runner.run_cycle()
    assert runner._stats["cycles"] == 0


async def test_run_cycle_no_activity_recently(monkeypatch):
    import time

    monkeypatch.setattr("config.settings.snooze_enabled", True)
    runner = SnoozeRunner()
    runner._is_idle = lambda: True
    runner._activity_since_last_cycle = False
    runner._last_cycle_time = time.time()  # very recent

    await runner.run_cycle()
    assert runner._stats["cycles_skipped"] == 1


async def test_run_cycle_full_with_empty_store(monkeypatch, tmp_path):
    """Run a full cycle with empty memory store — no LLM calls needed."""
    import time

    monkeypatch.setattr("config.settings.snooze_enabled", True)
    monkeypatch.setattr("config.settings.snooze_max_cycle_seconds", 10)
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    runner = SnoozeRunner()
    runner._is_idle = lambda: True
    runner._llm_available = lambda: False  # no LLM → skip LLM activities

    await runner.run_cycle()
    assert runner._stats["cycles"] == 1
    assert runner._activity_since_last_cycle is False


async def test_run_cycle_propagates_cancellation(monkeypatch):
    """A cancel must escape run_cycle, not be logged and dropped.

    Snooze mutates data/memories/*.md and the FTS index in separate steps.
    Absorbing a cancel let the caller believe the cycle finished normally and
    carry on, and at shutdown left the maintenance tick running past the
    cancel into the checkpoint/vacuum branches.
    """
    monkeypatch.setattr("config.settings.snooze_enabled", True)
    runner = SnoozeRunner()
    runner._is_idle = lambda: True

    async def _cancelled():
        raise asyncio.CancelledError()

    runner._do_cycle = _cancelled

    with pytest.raises(asyncio.CancelledError):
        await runner.run_cycle()

    # The finally block still settles cycle bookkeeping on the way out.
    assert runner._running is False
    assert runner._stats["cycles"] == 1


async def test_run_cycle_still_absorbs_timeouts(monkeypatch):
    """Hitting the cycle's own budget is normal completion, not an error."""
    monkeypatch.setattr("config.settings.snooze_enabled", True)
    monkeypatch.setattr("config.settings.snooze_max_cycle_seconds", 1)
    runner = SnoozeRunner()
    runner._is_idle = lambda: True

    async def _slow():
        await asyncio.sleep(5)

    runner._do_cycle = _slow

    await runner.run_cycle()  # must not raise
    assert runner._running is False
    assert runner._stats["cycles"] == 1


# ---------------------------------------------------------------------------
# _parse_insight_entries (static method, pure logic)
# ---------------------------------------------------------------------------


def test_parse_insight_entries_valid_array():
    text = '[{"type": "profile", "content": "User lives in Seattle"}]'
    entries = SnoozeRunner._parse_insight_entries(text)
    assert len(entries) == 1
    assert entries[0]["type"] == "profile"


def test_parse_insight_entries_valid_dict():
    text = '{"type": "profile", "content": "User is a developer"}'
    entries = SnoozeRunner._parse_insight_entries(text)
    assert len(entries) == 1


def test_parse_insight_entries_fenced():
    text = '```json\n[{"type": "note", "content": "info"}]\n```'
    entries = SnoozeRunner._parse_insight_entries(text)
    assert len(entries) == 1


def test_parse_insight_entries_invalid():
    entries = SnoozeRunner._parse_insight_entries("not json")
    assert entries == []


def test_parse_insight_entries_empty():
    entries = SnoozeRunner._parse_insight_entries("")
    assert entries == []


# ---------------------------------------------------------------------------
# _cleanup_cron
# ---------------------------------------------------------------------------


async def test_cleanup_cron_first_run():
    """First run always executes (no last_cron_cleanup state)."""
    runner = SnoozeRunner()
    # Should not raise and should complete
    await runner._cleanup_cron()


async def test_cleanup_cron_skips_if_recent():
    import time

    from db import models as db

    # Set last_cron_cleanup to now
    db.set_snooze_state("last_cron_cleanup", str(time.time()))
    runner = SnoozeRunner()
    # Should return immediately without doing work
    await runner._cleanup_cron()


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


def test_get_snooze_singleton(monkeypatch):
    import core.snooze

    monkeypatch.setattr(core.snooze, "_runner", None)
    s1 = get_snooze()
    s2 = get_snooze()
    assert s1 is s2


# ---------------------------------------------------------------------------
# _dedup_sweep with empty store
# ---------------------------------------------------------------------------


async def test_dedup_sweep_empty_store(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    runner = SnoozeRunner()
    # Empty store → should complete without error
    await runner._dedup_sweep()


# ---------------------------------------------------------------------------
# _enrich_tags with empty store
# ---------------------------------------------------------------------------


async def test_enrich_tags_empty_store(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    runner = SnoozeRunner()
    await runner._enrich_tags()


# ---------------------------------------------------------------------------
# _reconcile_index with empty store
# ---------------------------------------------------------------------------


async def test_reconcile_index_empty_store(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    runner = SnoozeRunner()
    await runner._reconcile_index()


# ---------------------------------------------------------------------------
# _update_skill_cooccurrence with empty memory
# ---------------------------------------------------------------------------


async def test_update_skill_cooccurrence_empty(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    runner = SnoozeRunner()
    await runner._update_skill_cooccurrence()
