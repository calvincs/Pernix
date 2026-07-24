"""Tests for maintenance.py: MaintenanceRunner lifecycle, stats, and tick duties."""

import asyncio

import pytest

from maintenance import MaintenanceRunner, get_maintenance


def _make_runner() -> MaintenanceRunner:
    return MaintenanceRunner()


# ---------------------------------------------------------------------------
# start / stop
# ---------------------------------------------------------------------------


async def test_start_creates_task():
    runner = _make_runner()
    runner.start()
    assert runner._task is not None
    assert not runner._task.done()
    await runner.stop()


async def test_stop_cancels_task():
    runner = _make_runner()
    runner.start()
    await runner.stop()
    assert runner._task.done()


async def test_start_idempotent():
    runner = _make_runner()
    runner.start()
    t1 = runner._task
    runner.start()  # second call — should not replace the task
    assert runner._task is t1
    await runner.stop()


# ---------------------------------------------------------------------------
# track_task
# ---------------------------------------------------------------------------


async def test_track_task():
    runner = _make_runner()

    async def dummy():
        await asyncio.sleep(0.01)

    task = asyncio.create_task(dummy())
    runner.track_task(task)
    assert task in runner._tracked_tasks
    await task  # wait for completion
    # After completion the task is discarded from set
    assert task not in runner._tracked_tasks


# ---------------------------------------------------------------------------
# get_stats
# ---------------------------------------------------------------------------


async def test_get_stats():
    runner = _make_runner()
    stats = runner.get_stats()
    assert "tick_count" in stats
    assert "sessions_reaped" in stats
    assert "subscribers_reaped" in stats
    assert "active_background_tasks" in stats
    assert stats["tick_count"] == 0


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


def test_get_maintenance_singleton(monkeypatch):
    import maintenance

    monkeypatch.setattr(maintenance, "_runner", None)
    m1 = get_maintenance()
    m2 = get_maintenance()
    assert m1 is m2


# ---------------------------------------------------------------------------
# _tick duties
# ---------------------------------------------------------------------------


async def test_tick_runs_without_error():
    """Maintenance tick executes without raising exceptions."""
    runner = _make_runner()
    runner._tick_count = 1
    # Should not raise
    await runner._tick()
    # Stats should still be accessible
    stats = runner.get_stats()
    assert isinstance(stats, dict)


async def test_tick_prunes_completed_tasks():
    """Maintenance tick cleans up completed tracked tasks from the runner."""
    runner = _make_runner()

    async def quick_task():
        return "done"

    task = asyncio.create_task(quick_task())
    await task  # Let it complete first

    # Manually add to tracked tasks (as done task)
    runner._tracked_tasks.add(task)

    # Run tick which should prune it
    runner._tick_count = 1
    await runner._tick()

    assert runner._stats["tasks_completed"] >= 1


async def test_tick_5_runs_without_error():
    """Tick divisible by 5 runs session reaping code without error."""
    runner = _make_runner()
    runner._tick_count = 5  # divisible by 5
    # Should not raise even if sessions are empty
    await runner._tick()
    # _tick doesn't increment tick_count (that's done in _heartbeat)
    # Just verify stats are accessible
    assert isinstance(runner.get_stats(), dict)


# ---------------------------------------------------------------------------
# Snooze scheduling — must not run inside the tick's 30s bound
# ---------------------------------------------------------------------------


async def test_tick_does_not_run_snooze(monkeypatch):
    """Snooze must be scheduled by _heartbeat, never from inside _tick.

    _tick is wrapped in wait_for(TICK_TIMEOUT); a cycle budgeted by the
    larger snooze_max_cycle_seconds would be force-cancelled partway.
    """
    import core.snooze as snooze_mod

    called = False

    class _Spy:
        async def run_cycle(self):
            nonlocal called
            called = True

        def request_cancel(self):
            pass

        def get_stats(self):
            return {}

    monkeypatch.setattr(snooze_mod, "_runner", _Spy())
    runner = _make_runner()
    runner._tick_count = 10  # a snooze-interval tick under the old scheduling
    await runner._tick()
    assert not called, "_tick must not invoke the snooze cycle"


async def test_run_snooze_budget_exceeds_the_cycle_budget(monkeypatch):
    """The outer bound must be strictly larger than snooze's own budget.

    Otherwise the outer wait fires first and cancels a cycle that was still
    inside its configured allowance — the original defect, where a 30s tick
    bound truncated a 60s cycle budget.
    """
    import maintenance as maintenance_mod
    from config import settings

    observed: dict = {}

    async def _fake_wait_for(coro, timeout=None):
        observed["timeout"] = timeout
        coro.close()

    monkeypatch.setattr(maintenance_mod.asyncio, "wait_for", _fake_wait_for)
    runner = _make_runner()
    await runner._run_snooze()
    assert observed["timeout"] > settings.snooze_max_cycle_seconds
    assert observed["timeout"] > maintenance_mod.TICK_TIMEOUT


async def test_run_snooze_propagates_cancellation(monkeypatch):
    """A shutdown cancel must not be absorbed by the snooze wrapper."""
    import core.snooze as snooze_mod

    class _Cancelling:
        async def run_cycle(self):
            raise asyncio.CancelledError()

        def request_cancel(self):
            pass

        def get_stats(self):
            return {}

    monkeypatch.setattr(snooze_mod, "_runner", _Cancelling())
    runner = _make_runner()
    with pytest.raises(asyncio.CancelledError):
        await runner._run_snooze()


async def test_run_snooze_swallows_ordinary_errors(monkeypatch):
    """A failing cycle must not take the heartbeat down with it."""
    import core.snooze as snooze_mod

    class _Boom:
        async def run_cycle(self):
            raise RuntimeError("kaboom")

        def request_cancel(self):
            pass

        def get_stats(self):
            return {}

    monkeypatch.setattr(snooze_mod, "_runner", _Boom())
    runner = _make_runner()
    await runner._run_snooze()  # must not raise
