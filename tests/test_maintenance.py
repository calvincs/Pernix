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
