"""Regression: the daily tier said it had run before it had done anything.

Shipped defect (3.2.2 audit S20). `_daily_tier_due()` wrote the durable stamp
`maintenance_last_daily_at` BEFORE any of the day's work started, and the
whole tier then ran inside `_tick()`, which `_heartbeat` wraps in
`wait_for(TICK_TIMEOUT)` — 30 seconds.

There was exactly one await in that block: the memory health check. So the
timeout could only ever fire there, and firing raised `CancelledError`, which
is a `BaseException` and therefore passed straight through the three
surrounding `except Exception` handlers. The incremental vacuum and the five
prunes behind it were skipped. The stamp had already been written, so a fresh
runner after a restart read the same row and returned False: the work was
suppressed for a full 24 hours, and the only thing in the log was
"Maintenance tick exceeded 30s timeout, skipping".

Calibration, from the audit's measurements: the health check cannot reach 30 s
on CPU — 0.09 s per 10k entries, 0.43 s at 50k, 3.04 s with 5,000 epoch
collisions and fix=True. The trigger is lock contention, because
`repair_epoch_collisions` runs under `MemoryStore._lock`, an untimed
`threading.Lock` shared with every live `remember()` and the whole snooze
cycle. Rare, then — but silent and self-perpetuating, which is what makes it
worth fixing rather than watching.

Two more defects the filing missed, fixed in the same change:

* The entire block after the health check ran SYNCHRONOUSLY ON THE EVENT
  LOOP. `incremental_vacuum()`, `prune_cron()` and the four hygiene prunes
  were plain blocking calls — unlike the WAL checkpoint and the backup, which
  had been explicitly moved off the loop with the note that running them there
  "froze every session's SSE". The daily prune sweep froze it too, once a day.
* With no awaits after the health check, `wait_for` could not bound the block
  at all: the tick could overrun arbitrarily and then raise TimeoutError over
  work that had in fact completed. `TICK_TIMEOUT` was neither a bound nor a
  reliable signal.
* And `get_stats()` exposed no daily-tier state, so a stranded day was
  invisible to /api/health.

Fix: the tier is a tracked single-flight task outside the tick bound, each
duty runs on the background pool under its own budget, completion is stamped
per duty AFTER the duty finishes, and a duty whose thread outlives its budget
keeps its in-flight marker until the THREAD ends — so it stays eligible
without ever being started a second time on top of itself.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

import maintenance as maintenance_mod
from db import models as db
from maintenance import DAILY_DUTIES, MaintenanceRunner


@pytest.fixture
def runner(monkeypatch):
    """A runner whose backup step is a no-op — backups are S08's subject."""
    r = MaintenanceRunner()

    async def _no_backup():
        return None

    monkeypatch.setattr(r, "_ensure_recent_backup", _no_backup)
    return r


@pytest.fixture
def no_budget(monkeypatch):
    """A zero-second duty budget: `wait_for` gives up without ever waiting.

    Deterministic by construction — a timeout of 0 raises the moment the
    future is not already done, so nothing here depends on how fast a thread
    starts.
    """
    monkeypatch.setattr(maintenance_mod, "DAILY_DUTY_TIMEOUT", 0)


class _ParkedDuty:
    """A duty body that blocks until released, and counts its entries."""

    def __init__(self, result: str = "done"):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.calls = 0
        self._result = result
        self._lock = threading.Lock()

    def __call__(self) -> str:
        with self._lock:
            self.calls += 1
        self.entered.set()
        assert self.release.wait(10), "the test never released the duty"
        self.finished.set()
        return self._result


# ---------------------------------------------------------------------------
# A duty that overruns does not take the day with it
# ---------------------------------------------------------------------------


async def test_a_duty_that_overruns_its_budget_is_not_marked_done(runner, no_budget):
    parked = _ParkedDuty()
    runner._duty_memory_health = parked

    await runner._run_daily_duty("memory_health")

    assert runner._duty_last_success("memory_health") == 0, "unfinished work must not be stamped"
    assert "memory_health" in runner._duties_due(), "and must stay eligible"
    assert "memory_health" in runner._daily_inflight, "its thread is still running"

    parked.release.set()
    assert await asyncio.to_thread(parked.finished.wait, 10)


async def test_the_duties_behind_a_failing_one_still_run(runner):
    """Per-step failure: one duty's problem is not the other four's."""

    def _boom():
        raise RuntimeError("index is wedged")

    runner._duty_memory_health = _boom
    await runner._run_daily_tier()

    assert runner._duties_due() == ["memory_health"], "only the failure stays outstanding"
    for name in DAILY_DUTIES:
        if name != "memory_health":
            assert runner._duty_last_success(name) > 0, f"{name} was skipped by an unrelated failure"
    assert "wedged" in runner._daily_errors["memory_health"]


async def test_a_stranded_duty_is_not_started_a_second_time(runner, no_budget):
    """The thread is still doing the work; a retry would be two of them."""
    parked = _ParkedDuty()
    runner._duty_memory_health = parked

    await runner._run_daily_duty("memory_health")
    assert await asyncio.to_thread(parked.entered.wait, 10), "the thread did start"
    assert parked.calls == 1

    await runner._run_daily_tier()  # a later attempt, same process
    assert parked.calls == 1, "a duty whose thread is still alive must not be re-entered"

    parked.release.set()
    assert await asyncio.to_thread(parked.finished.wait, 10)
    assert await asyncio.to_thread(_wait_until_clear, runner, "memory_health")
    assert "memory_health" in runner._duties_due(), "and once the thread ends it is eligible again"


def _wait_until_clear(runner: MaintenanceRunner, name: str, timeout: float = 10.0) -> bool:
    """The in-flight marker is cleared by the future's callback, on the loop's
    schedule rather than the thread's — poll the barrier, not the clock."""
    deadline = time.monotonic() + timeout
    while name in runner._daily_inflight:
        if time.monotonic() > deadline:
            return False
    return True


async def test_a_whole_stranded_tier_is_still_due_after_a_restart(runner, no_budget):
    """The durable half of the defect: a restart used to read 'already ran'."""
    parked = {name: _ParkedDuty() for name in DAILY_DUTIES}
    for name, duty in parked.items():
        setattr(runner, f"_duty_{name}", duty)

    assert runner._daily_tier_due() is True
    await runner._run_daily_tier()

    # A brand-new process: no in-memory state at all, only the durable rows.
    reborn = MaintenanceRunner()
    assert reborn._duties_due() == list(DAILY_DUTIES), "nothing completed, so nothing is done"

    # Within the retry window it holds off; past it, it tries again.
    assert reborn._daily_tier_due() is False, "not every 60 seconds"
    db.set_snooze_state(reborn._DAILY_TIER_KEY, str(time.time() - 2 * maintenance_mod.DAILY_RETRY_INTERVAL_S))
    assert reborn._daily_tier_due() is True, "an hour later, not a day later"

    for duty in parked.values():
        duty.release.set()
    for duty in parked.values():
        assert await asyncio.to_thread(duty.finished.wait, 10)


async def test_a_finished_tier_is_left_alone_for_a_day(runner):
    """The fix must not turn a healthy box into an hourly sweep."""
    await runner._run_daily_tier()

    assert runner._duties_due() == []
    assert runner._daily_tier_due() is False
    assert MaintenanceRunner()._daily_tier_due() is False, "durable, as it always was"


# ---------------------------------------------------------------------------
# Where the work runs, and where it does not
# ---------------------------------------------------------------------------


async def test_the_daily_tier_is_no_longer_inside_the_tick_bound(runner, monkeypatch):
    """_tick had one await and five blocking calls; the timeout could only
    ever fire at the await, and firing abandoned everything after it."""
    ran: list[str] = []
    for name in DAILY_DUTIES:
        setattr(runner, f"_duty_{name}", lambda n=name: ran.append(n) or "")

    class _StubManager:
        def reap_dead_subscribers(self):
            return 0

        def reap_idle_sessions(self, **_kw):
            return 0

    monkeypatch.setattr("sessions.manager.get_manager", lambda: _StubManager())
    runner._tick_count = 1
    await runner._tick()

    assert ran == [], "the tick must not carry the daily tier any more"
    assert runner._duties_due() == list(DAILY_DUTIES)


async def test_every_daily_duty_runs_off_the_event_loop(runner):
    """The missed defect: the prunes ran synchronously on the loop, which is
    exactly what the checkpoint and the backup were moved off it to avoid."""
    threads: dict[str, str] = {}

    for name in DAILY_DUTIES:

        def _record(n=name):
            threads[n] = threading.current_thread().name
            return ""

        setattr(runner, f"_duty_{name}", _record)

    loop_thread = threading.current_thread().name
    await runner._run_daily_tier()

    assert sorted(threads) == sorted(DAILY_DUTIES)
    for name, where in threads.items():
        assert where != loop_thread, f"{name} blocked the event loop"


async def test_the_mutating_duties_still_stand_on_a_recent_backup():
    """Unchanged intent: health_check(fix=True) rewrites the memory index and
    the prunes delete rows, so a snapshot comes first."""
    runner = MaintenanceRunner()
    order: list[str] = []

    async def _backup():
        order.append("backup")

    runner._ensure_recent_backup = _backup
    for name in DAILY_DUTIES:
        setattr(runner, f"_duty_{name}", lambda n=name: order.append(n) or "")

    await runner._run_daily_tier()
    assert order[0] == "backup"
    assert order[1:] == list(DAILY_DUTIES)


async def test_only_one_daily_tier_runs_at_a_time(monkeypatch):
    """Single-flight from the heartbeat, like the snooze cycle beside it."""
    runner = MaintenanceRunner()
    running = 0
    peak = 0
    release = asyncio.Event()

    async def _tier():
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await release.wait()
        running -= 1

    monkeypatch.setattr(runner, "_run_daily_tier", _tier)
    monkeypatch.setattr(runner, "_tick", lambda: asyncio.sleep(0))
    monkeypatch.setattr(runner, "_daily_tier_due", lambda: True)
    monkeypatch.setattr(maintenance_mod, "TICK_INTERVAL", 0.001)
    monkeypatch.setattr("config.settings.snooze_interval_ticks", 10_000)

    task = asyncio.create_task(runner._heartbeat())
    for _ in range(50):
        await asyncio.sleep(0)
    release.set()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert peak <= 1, "two tiers would be two memory repairs on one index"


# ---------------------------------------------------------------------------
# Shutdown, and being able to see any of this
# ---------------------------------------------------------------------------


async def test_shutdown_leaves_an_unfinished_duty_due_and_does_not_wait_for_it(runner, no_budget):
    parked = _ParkedDuty()
    runner._duty_memory_health = parked

    await runner._run_daily_duty("memory_health")
    assert parked.entered.wait(10)

    await runner.stop()  # must return rather than block on the thread

    assert runner._duty_last_success("memory_health") == 0
    assert "memory_health" in MaintenanceRunner()._duties_due(), "eligible on the next start"

    parked.release.set()
    assert await asyncio.to_thread(parked.finished.wait, 10)


async def test_a_stranded_day_is_visible_in_the_stats(runner, no_budget):
    """It used to be invisible: the stamp said done, the log said 'skipping'."""
    parked = _ParkedDuty()
    runner._duty_memory_health = parked
    await runner._run_daily_duty("memory_health")

    daily = runner.get_stats()["daily"]
    assert daily["in_flight"] == ["memory_health"]
    assert "memory_health" in daily["due"]
    assert daily["last_success"]["memory_health"] is None
    assert "budget" in daily["last_error"]["memory_health"]

    parked.release.set()
    assert await asyncio.to_thread(parked.finished.wait, 10)


# ---------------------------------------------------------------------------
# The S07 crossover: two vacuums on one file
# ---------------------------------------------------------------------------


async def test_the_vacuum_duty_defers_rather_than_colliding_with_optimize(runner):
    """The sharpest case in the filing: the daily tier can be running
    `incremental_vacuum` on the same file at the moment an operator clicks
    Optimize, and neither used to know about the other."""
    from db.exclusive import get_gate

    gate = get_gate()
    gate.claim("storage.optimize")
    try:
        await runner._run_daily_duty("incremental_vacuum")
    finally:
        gate.release()

    assert runner._duty_last_success("incremental_vacuum") == 0, "deferred, so not done"
    assert "incremental_vacuum" in runner._duties_due(), "and tried again in an hour"
    assert "storage.optimize" in runner._daily_errors["incremental_vacuum"]

    await runner._run_daily_duty("incremental_vacuum")
    assert runner._duty_last_success("incremental_vacuum") > 0, "and it runs once the gate is free"
