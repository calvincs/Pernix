"""Two ways the maintenance heartbeat stopped doing its job.

1. The tick loop awaited the snooze cycle inline. A cycle can run to its
   backstop (900s, ×4 for a local background model), and for that whole
   time nothing was reaped, no stuck session was unstuck, no WAL
   checkpoint ran — and _tick_count froze, so the hourly and daily tiers
   drifted by however long the cycle took.

2. The daily tier fired on `tick % 1440`, a per-process counter. A box
   that restarts daily never reached 1440, so the memory self-repair, the
   incremental vacuum and the aux-table prunes never ran at all.

Extended 2026-09-08 (3.2.2 audit S20). Moving the schedule onto the clock
fixed *when* the tier is attempted and said nothing about whether it
finished: the durable stamp was written before any work started, so an
interrupted tier and a completed one left the same row. These tests now cover
both halves — the schedule, and what "ran" means. The full suite for the
completion semantics is in
tests/regressions/test_2026-09-08_a_slow_health_check_skipped_a_days_maintenance.py.
"""

import asyncio
import time

import pytest

from db import models as db
from maintenance import DAILY_DUTIES, MaintenanceRunner


@pytest.fixture
def heartbeat():
    return MaintenanceRunner()


async def test_a_long_snooze_cycle_does_not_block_the_tick_loop(heartbeat, monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_cycle():
        started.set()
        await release.wait()

    monkeypatch.setattr(heartbeat, "_run_snooze", slow_cycle)
    monkeypatch.setattr("config.settings.snooze_interval_ticks", 1)

    ticks = 0

    async def counting_tick():
        nonlocal ticks
        ticks += 1

    monkeypatch.setattr(heartbeat, "_tick", counting_tick)
    monkeypatch.setattr("maintenance.TICK_INTERVAL", 0.01)

    task = asyncio.create_task(heartbeat._heartbeat())
    await asyncio.wait_for(started.wait(), timeout=2)
    ticks_when_snooze_started = ticks
    await asyncio.sleep(0.1)  # snooze still parked
    release.set()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert ticks > ticks_when_snooze_started, "the heartbeat must keep ticking during a snooze cycle"


async def test_only_one_snooze_cycle_runs_at_a_time(heartbeat, monkeypatch):
    running = 0
    peak = 0
    release = asyncio.Event()

    async def cycle():
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await release.wait()
        running -= 1

    monkeypatch.setattr(heartbeat, "_run_snooze", cycle)
    monkeypatch.setattr(heartbeat, "_tick", lambda: asyncio.sleep(0))
    monkeypatch.setattr("config.settings.snooze_interval_ticks", 1)
    monkeypatch.setattr("maintenance.TICK_INTERVAL", 0.01)

    task = asyncio.create_task(heartbeat._heartbeat())
    await asyncio.sleep(0.15)
    release.set()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert peak == 1, "overlapping cycles would double-run memory surgery"


def test_the_daily_tier_is_keyed_on_the_clock(heartbeat):
    db.set_snooze_state(heartbeat._DAILY_TIER_KEY, "")
    assert heartbeat._daily_tier_due() is True, "first run on a fresh box"
    assert heartbeat._daily_tier_due() is False, "not again on the next tick"


def test_the_daily_tier_survives_a_restart(heartbeat):
    db.set_snooze_state(heartbeat._DAILY_TIER_KEY, "")
    assert heartbeat._daily_tier_due() is True
    # A brand-new process — the tick counter is 0 again, the stamp is not.
    assert MaintenanceRunner()._daily_tier_due() is False


def test_the_daily_tier_comes_due_again_after_24h(heartbeat):
    db.set_snooze_state(heartbeat._DAILY_TIER_KEY, str(time.time() - 25 * 3600))
    assert heartbeat._daily_tier_due() is True


# ---------------------------------------------------------------------------
# 3. "Ran" has to mean the work happened
# ---------------------------------------------------------------------------
#
# The clock fixed WHEN the tier is attempted. What it could not say is whether
# the attempt got anywhere: the stamp above was written before a single duty
# started, so a tier cut short by the tick timeout left exactly the row a
# completed one leaves — and suppressed the vacuum and five prunes for a day,
# restart included.


async def test_the_schedule_is_satisfied_by_work_not_by_an_attempt(heartbeat, monkeypatch):
    """A tier that attempted everything and completed nothing is still due."""

    async def _no_backup():
        return None

    monkeypatch.setattr(heartbeat, "_ensure_recent_backup", _no_backup)
    for name in DAILY_DUTIES:
        monkeypatch.setattr(heartbeat, f"_duty_{name}", _raises)

    assert heartbeat._daily_tier_due() is True
    await heartbeat._run_daily_tier()

    assert heartbeat._duties_due() == list(DAILY_DUTIES), "nothing succeeded, so nothing is done"
    # An hour, not a day: the attempt stamp throttles the retry, and the
    # per-duty rows decide what the retry actually does.
    db.set_snooze_state(heartbeat._DAILY_TIER_KEY, str(time.time() - 2 * 3600))
    assert heartbeat._daily_tier_due() is True


async def test_a_completed_tier_stamps_each_duty_for_itself(heartbeat, monkeypatch):
    async def _no_backup():
        return None

    monkeypatch.setattr(heartbeat, "_ensure_recent_backup", _no_backup)
    await heartbeat._run_daily_tier()

    for name in DAILY_DUTIES:
        assert heartbeat._duty_last_success(name) > 0, f"{name} completed but was not recorded"
    assert heartbeat._duties_due() == []
    assert heartbeat._daily_tier_due() is False
    assert MaintenanceRunner()._daily_tier_due() is False, "and it is durable, as it always was"


def _raises():
    raise RuntimeError("this duty did not happen")
