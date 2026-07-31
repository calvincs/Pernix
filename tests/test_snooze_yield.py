"""run_cycle supervisor semantics: run-to-done, yield-to-user, hang backstop.

The cycle has no wall-clock schedule: it runs until the ladder completes.
User activity (request_cancel) must abort in-flight awaits promptly, the
backstop must kill a wedged cycle, and shutdown cancellation must still
propagate (maintenance relies on it).
"""

import asyncio

import pytest

from core.snooze import SnoozeRunner


@pytest.fixture
def runner(monkeypatch):
    monkeypatch.setattr("config.settings.snooze_enabled", True)
    r = SnoozeRunner()
    r._is_idle = lambda: True
    r._activity_since_last_cycle = True
    return r


async def test_cycle_runs_to_completion(runner):
    async def quick():
        return None

    runner._do_cycle = quick
    assert await runner.run_cycle(force=True) == "ran"
    assert runner._stats["cycles"] == 1
    assert not runner._running


async def test_cycle_yields_promptly_to_user_activity(runner, monkeypatch):
    monkeypatch.setattr("config.settings.snooze_max_cycle_seconds", 60)
    started = asyncio.Event()

    async def slow():
        started.set()
        await asyncio.sleep(30)  # stands in for a minutes-long LLM call

    runner._do_cycle = slow
    loop = asyncio.get_event_loop()
    t0 = loop.time()
    task = asyncio.create_task(runner.run_cycle(force=True))
    await started.wait()
    runner.request_cancel()  # what SessionManager.prompt() does
    outcome = await task
    assert outcome == "yielded"
    assert loop.time() - t0 < 5, "yield must abort the in-flight await, not wait it out"
    assert not runner._running
    assert runner._stats["cycles"] == 1  # bookkeeping intact


async def test_backstop_kills_wedged_cycle(runner, monkeypatch):
    monkeypatch.setattr("config.settings.snooze_max_cycle_seconds", 1)

    async def wedged():
        await asyncio.sleep(30)

    runner._do_cycle = wedged
    assert await runner.run_cycle(force=True) == "backstop"
    assert not runner._running


async def test_shutdown_cancel_propagates(runner):
    started = asyncio.Event()

    async def slow():
        started.set()
        await asyncio.sleep(30)

    runner._do_cycle = slow
    task = asyncio.create_task(runner.run_cycle(force=True))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not runner._running


async def test_cycle_error_reported_and_bookkept(runner):
    async def boom():
        raise RuntimeError("x")

    runner._do_cycle = boom
    assert await runner.run_cycle(force=True) == "error"
    assert runner._stats["cycles"] == 1
    assert not runner._running
