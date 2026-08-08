"""Item #8: snooze activity-gating + 15-minute cadence."""

import time

import pytest

from core.snooze import SnoozeRunner
from sessions import state_v2 as sv2
from sessions.manager import SessionManager


@pytest.mark.asyncio
async def test_active_session_blocks_snooze(monkeypatch):
    monkeypatch.setattr("config.settings.snooze_enabled", True)
    mgr = SessionManager()
    sid = mgr.create_session(title="Active")
    mgr.get(sid)._state_v2 = sv2.SessionStateV2.PROCESSING
    monkeypatch.setattr("sessions.manager._manager", mgr)

    runner = SnoozeRunner()
    # Bypass _is_idle since has_active_work should trigger the skip first.
    runner._is_idle = lambda: True
    await runner.run_cycle()

    assert runner._stats["cycles"] == 0
    assert runner._stats["cycles_skipped"] == 1
    # Important: active-work skip must preserve the pending flag so the
    # next quiescent tick picks up whatever triggered it.
    assert runner._activity_since_last_cycle is True


@pytest.mark.asyncio
async def test_idle_session_allows_snooze(monkeypatch):
    monkeypatch.setattr("config.settings.snooze_enabled", True)
    monkeypatch.setattr("config.settings.snooze_max_cycle_seconds", 5)
    mgr = SessionManager()
    sid = mgr.create_session(title="Idle")
    mgr.get(sid)._state_v2 = sv2.SessionStateV2.IDLE_READY
    monkeypatch.setattr("sessions.manager._manager", mgr)

    runner = SnoozeRunner()
    runner._is_idle = lambda: True
    runner._llm_available = lambda: False
    await runner.run_cycle()

    assert runner._stats["cycles"] == 1


def test_has_active_work_detects_scouting_and_processing():
    mgr = SessionManager()
    assert mgr.has_active_work() is False

    sid = mgr.create_session(title="Busy")
    sess = mgr.get(sid)

    sess._state_v2 = sv2.SessionStateV2.IDLE_READY
    assert mgr.has_active_work() is False

    sess._state_v2 = sv2.SessionStateV2.SCOUTING
    assert mgr.has_active_work() is True

    sess._state_v2 = sv2.SessionStateV2.PROCESSING
    assert mgr.has_active_work() is True

    sess._state_v2 = sv2.SessionStateV2.IDLE_READY
    assert mgr.has_active_work() is False


@pytest.mark.asyncio
async def test_cadence_15min_blocks_early_second_cycle(monkeypatch):
    monkeypatch.setattr("config.settings.snooze_enabled", True)
    runner = SnoozeRunner()
    runner._is_idle = lambda: True
    runner._activity_since_last_cycle = False
    # Pretend a cycle just finished 5 min ago.
    runner._last_cycle_time = time.time() - 300

    await runner.run_cycle()
    assert runner._stats["cycles"] == 0
    assert runner._stats["cycles_skipped"] == 1


@pytest.mark.asyncio
async def test_cadence_allows_after_15min(monkeypatch):
    monkeypatch.setattr("config.settings.snooze_enabled", True)
    monkeypatch.setattr("config.settings.snooze_max_cycle_seconds", 5)
    runner = SnoozeRunner()
    runner._is_idle = lambda: True
    runner._llm_available = lambda: False
    runner._activity_since_last_cycle = False
    # Pretend the last cycle finished 20 min ago — past the 15-min cadence.
    runner._last_cycle_time = time.time() - 1200

    await runner.run_cycle()
    assert runner._stats["cycles"] == 1


def test_min_cycle_interval_constant():
    """Item #8: cadence tightened from 3600 (1 hour) to 900 (15 min)."""
    assert SnoozeRunner._MIN_CYCLE_INTERVAL_SEC == 900
