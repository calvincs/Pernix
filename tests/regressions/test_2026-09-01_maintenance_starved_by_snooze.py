"""Two ways the maintenance heartbeat stopped doing its job.

1. The tick loop awaited the snooze cycle inline. A cycle can run to its
   backstop (900s, ×4 for a local background model), and for that whole
   time nothing was reaped, no stuck session was unstuck, no WAL
   checkpoint ran — and _tick_count froze, so the hourly and daily tiers
   drifted by however long the cycle took.

2. The daily tier fired on `tick % 1440`, a per-process counter. A box
   that restarts daily never reached 1440, so the memory self-repair, the
   incremental vacuum and the aux-table prunes never ran at all.
"""

import asyncio
import time

import pytest

from db import models as db
from maintenance import MaintenanceRunner


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
    assert heartbeat._daily_tier_due() is False, "not again in the same day"


def test_the_daily_tier_survives_a_restart(heartbeat):
    db.set_snooze_state(heartbeat._DAILY_TIER_KEY, "")
    assert heartbeat._daily_tier_due() is True
    # A brand-new process — the tick counter is 0 again, the stamp is not.
    assert MaintenanceRunner()._daily_tier_due() is False


def test_the_daily_tier_comes_due_again_after_24h(heartbeat):
    db.set_snooze_state(heartbeat._DAILY_TIER_KEY, str(time.time() - 25 * 3600))
    assert heartbeat._daily_tier_due() is True
