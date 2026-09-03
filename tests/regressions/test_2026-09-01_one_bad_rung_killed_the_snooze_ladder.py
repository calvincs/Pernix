"""One failing activity skipped the whole rest of the snooze ladder.

_do_cycle was a long sequence of awaits with no guard between them, so an
exception in an early rung — a permissions error on one RLM run dir, a
corrupt FTS row, a hand-created memory file with a space in its name —
ended the coroutine. Everything after it (refine, skill auto-apply, dream,
telos, adaptive) was skipped on EVERY cycle for as long as the fault
lasted, and the only sign was one "SnoozeRunner cycle error" log line.

Also: a cycle that YIELDED to a user prompt used to clear the
activity flag the prompt had just set, so the interrupted rung waited out
the full cadence gate instead of resuming at the next slot.
"""

import asyncio

import pytest

from core.snooze import SnoozeRunner


@pytest.fixture
def snooze():
    return SnoozeRunner()


async def test_a_failing_rung_does_not_stop_the_ladder(snooze, caplog):
    ran = []

    async def boom():
        raise RuntimeError("one bad run dir")

    async def later():
        ran.append("later")

    await snooze._rung("cleanup_rlm_runs", boom())
    await snooze._rung("dream_step", later())

    assert ran == ["later"], "an early failure must not skip the rest of the cycle"


async def test_a_failing_rung_reports_its_default(snooze):
    async def boom():
        raise RuntimeError("nope")

    assert await snooze._rung("consolidate_files", boom(), default=False) is False


async def test_a_successful_rung_passes_its_value_through(snooze):
    async def ok():
        return True

    assert await snooze._rung("catchup_distill", ok(), default=False) is True


async def test_cancellation_is_never_swallowed(snooze):
    async def cancelled():
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await snooze._rung("dedup_sweep", cancelled())


def test_every_ladder_activity_is_wrapped():
    """The guard has to cover the ladder, not just the rungs someone
    remembered — the unguarded ones were the whole defect."""
    import inspect

    src = inspect.getsource(SnoozeRunner._do_cycle)
    bare = [
        line.strip()
        for line in src.splitlines()
        if "await self._" in line and "self._rung(" not in line and "_is_cancelled" not in line
    ]
    assert bare == [], f"unguarded ladder activities: {bare}"
