"""Alarm discharge pass (E3): evidence closes what evidence opened.

A divergence alarm is raised by a weekly reconciliation and had no clear
path — the live box carried one on AUTO-2026-W33 long after later weeks
came in clean. These tests pin the replacement: N spaced clean re-checks
discharge it, one dirty week resets the streak, the same week never counts
twice, and alarms owned by their own monitors are never touched.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from config import settings
from core.telos import discharge
from core.telos.discharge import run_alarm_discharge
from core.telos.store import TelosObject, TelosStore


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setattr(settings, "telos_enabled", True)
    return TelosStore.open()


@pytest.fixture
def clock(monkeypatch):
    """Controllable _now_iso so N daily passes fit in one test run."""
    state = {"now": datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)}
    monkeypatch.setattr(discharge, "_now_iso", lambda: state["now"].isoformat())

    def advance(hours):
        state["now"] += timedelta(hours=hours)

    return advance


def _divergence_alarm(store, week="2026-W33", state="open"):
    alarm = TelosObject(
        id=store.mint_id("alarm"),
        kind="alarm",
        meta={
            "type": "divergence",
            "target": f"AUTO-{week}",
            "level": 1,
            "state": state,
            "check_mode": "snapshot",
            "evidence": {"divergence": 0.4, "unsupported": 3},
        },
    )
    store.write(alarm)
    return alarm


def _week(store, week, divergence):
    series = list(store.get_state().get("coherence_series") or [])
    series.append({"week": week, "divergence": divergence, "claims": 5})
    store.set_state(coherence_series=series)


def test_three_spaced_clean_weeks_discharge_the_alarm(store, clock):
    alarm = _divergence_alarm(store)
    _week(store, "2026-W33", 0.4)  # the week that raised it — never a clean check

    assert run_alarm_discharge(store) == {"checked": 0, "discharged": 0}

    for week in ("2026-W34", "2026-W35", "2026-W36"):
        _week(store, week, 0.05)
        result = run_alarm_discharge(store)
        clock(24 * 7)
    assert result == {"checked": 1, "discharged": 1}

    reread = store.read("alarm", alarm.id)
    assert reread.get("state") == "cleared"
    assert "closed-by-discharge" in reread.get("cleared_reason")
    events = store.trace_events(days=1, types={"alarm_discharge"})
    assert events and events[-1]["clean_checks"] == 3 and events[-1]["was_acknowledged"] is False


def test_dirty_week_resets_the_streak_and_same_week_never_recounts(store, clock):
    alarm = _divergence_alarm(store)
    _week(store, "2026-W34", 0.05)
    run_alarm_discharge(store)
    assert len(store.read("alarm", alarm.id).get("clean_checks")) == 1

    # Re-running against the same week is not a second check.
    clock(24 * 7)
    assert run_alarm_discharge(store) == {"checked": 0, "discharged": 0}

    # A week where the condition holds again restarts the count from zero.
    _week(store, "2026-W35", 0.4)
    run_alarm_discharge(store)
    assert store.read("alarm", alarm.id).get("clean_checks") == []


def test_forced_same_day_rerun_is_one_check_not_two(store, clock):
    _alarm = _divergence_alarm(store)
    _week(store, "2026-W34", 0.05)
    run_alarm_discharge(store)
    _week(store, "2026-W35", 0.05)
    clock(1)  # a re-run an hour later sees new evidence but no new day
    run_alarm_discharge(store)
    assert len(store.read("alarm", _alarm.id).get("clean_checks")) == 1


def test_acknowledged_alarm_discharges_and_says_so(store, clock):
    """Discharge is by evidence, not by ack: an acknowledged alarm closes the
    same way, and the record keeps the fact it had been acknowledged."""
    alarm = _divergence_alarm(store, state="acknowledged")
    for week in ("2026-W34", "2026-W35", "2026-W36"):
        _week(store, week, 0.05)
        run_alarm_discharge(store)
        clock(24 * 7)
    assert store.read("alarm", alarm.id).get("state") == "cleared"
    events = store.trace_events(days=1, types={"alarm_discharge"})
    assert events[-1]["was_acknowledged"] is True


def test_alarms_with_their_own_monitor_are_never_touched(store, clock):
    binding = TelosObject(
        id=store.mint_id("alarm"),
        kind="alarm",
        meta={"type": "binding", "target": "g_x", "level": 1, "state": "open", "check_mode": "live"},
    )
    store.write(binding)
    _week(store, "2026-W34", 0.05)
    assert run_alarm_discharge(store) == {"checked": 0, "discharged": 0}
    assert store.read("alarm", binding.id).get("state") == "open"


def test_knob_off_means_no_writes(store, clock, monkeypatch):
    monkeypatch.setattr(settings, "telos_alarm_autoclose", False)
    alarm = _divergence_alarm(store)
    _week(store, "2026-W34", 0.05)
    assert run_alarm_discharge(store) == {"checked": 0, "discharged": 0}
    assert store.read("alarm", alarm.id).get("clean_checks") is None
