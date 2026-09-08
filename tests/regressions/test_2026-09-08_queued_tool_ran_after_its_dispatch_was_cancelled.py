"""Stop did not stop a tool that had not started yet.

Both tool pools are bounded, so a dispatched call can sit in the executor
queue with no thread on it. Cancelling the task that awaits it cancelled
neither the queued work item nor the callable: the `finally` cancelled only
the `started` waiter, and the CancelledError handler killed the subprocesses
of a tool that, by construction, had not spawned any. When a thread finally
came free, `_runner` called `registry.execute_sync` with no idea that its
dispatch had been abandoned — so a file write could land minutes after the
user pressed stop, and the caller had already seen a clean CancelledError.

A per-dispatch gate now settles it. The pool thread claims the dispatch
immediately before the tool's first line; the cancel path claims it on the
way out. Whichever takes the lock first decides, so the queue-to-running
boundary has a winner instead of a coin toss.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from core.tools import executor as tool_executor
from core.tools.executor import _DispatchGate, _execute_single, execute_tool_round
from core.tools.registry import ToolRegistry
from sessions.manager import SessionManager


@pytest.fixture
def one_thread_pool(monkeypatch):
    """A single tool thread — the saturation this file is about."""
    from concurrent.futures import ThreadPoolExecutor

    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pernix-tool")
    monkeypatch.setattr(tool_executor, "_get_tool_executor", lambda: pool)
    yield pool
    pool.shutdown(wait=False)


@pytest.fixture
def mgr(monkeypatch):
    fresh = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", fresh)
    return fresh


def _registry(tools: dict) -> ToolRegistry:
    reg = ToolRegistry()
    for name, func in tools.items():
        reg.register(
            name=name,
            func=func,
            description=name,
            parameters={"type": "object", "properties": {}},
            parallel_safe=False,
            timeout=5,
        )
    return reg


class _Fanout:
    """A hog holding the only thread, plus a side-effecting call behind it."""

    def __init__(self):
        self.ran: list[str] = []
        self.hog_started = threading.Event()
        self.release = threading.Event()

    def hog(self):
        self.ran.append("hog")
        self.hog_started.set()
        self.release.wait(5)
        return "hog done"

    def victim(self):
        self.ran.append("victim")  # the side effect that must never happen
        return "victim ran"

    def probe(self):
        self.ran.append("probe")
        return "probe ran"

    def registry(self) -> ToolRegistry:
        return _registry({"hog": self.hog, "victim": self.victim, "probe": self.probe})


async def _drain(reg, ctx, f: _Fanout, hog_task):
    """Release the pool and prove it worked past the victim's queue slot."""
    f.release.set()
    await hog_task
    await _execute_single("probe", {}, ctx, reg)


async def test_a_queued_tool_does_not_run_after_its_dispatch_is_cancelled(one_thread_pool, mgr):
    f = _Fanout()
    reg = f.registry()
    ctx = {"session_id": mgr.create_session(title="cancel-queued")}

    hog_task = asyncio.create_task(_execute_single("hog", {}, ctx, reg))
    await asyncio.to_thread(f.hog_started.wait, 5)
    victim_task = asyncio.create_task(_execute_single("victim", {}, ctx, reg))
    await asyncio.sleep(0.1)  # submitted, queued, parked in asyncio.wait

    victim_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await victim_task

    await _drain(reg, ctx, f, hog_task)
    assert f.ran == ["hog", "probe"], (
        "the queued tool ran after its dispatch was cancelled — the shipped bug "
        "cancelled only the started-waiter, so the pool ran the callable anyway"
    )


async def test_the_same_holds_through_execute_tool_round(one_thread_pool, mgr):
    """The agent's actual entry point, not just the private helper."""
    f = _Fanout()
    reg = f.registry()
    ctx = {"session_id": mgr.create_session(title="cancel-round")}

    hog_task = asyncio.create_task(execute_tool_round([{"name": "hog", "arguments": {}}], ctx, reg))
    await asyncio.to_thread(f.hog_started.wait, 5)
    victim_task = asyncio.create_task(execute_tool_round([{"name": "victim", "arguments": {}}], ctx, reg))
    await asyncio.sleep(0.1)

    victim_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await victim_task

    await _drain(reg, ctx, f, hog_task)
    assert f.ran == ["hog", "probe"]


async def test_a_cancelled_dispatch_is_not_charged_to_the_tool(one_thread_pool, mgr):
    """A call that never ran is no evidence about the tool, and no queue time
    to account for either — it must not land as a failure or a timeout."""
    f = _Fanout()
    reg = f.registry()
    ctx = {"session_id": mgr.create_session(title="cancel-metrics")}

    hog_task = asyncio.create_task(_execute_single("hog", {}, ctx, reg))
    await asyncio.to_thread(f.hog_started.wait, 5)
    victim_task = asyncio.create_task(_execute_single("victim", {}, ctx, reg))
    await asyncio.sleep(0.1)
    victim_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await victim_task
    await _drain(reg, ctx, f, hog_task)

    m = reg.metrics["victim"]
    assert (m.total_calls, m.failure_count, m.timeout_count) == (0, 0, 0)


async def test_a_stop_flag_stops_a_call_that_is_still_queued(one_thread_pool, mgr):
    """The other door onto the same defect: the session asked to stop, but no
    CancelledError reached this dispatch (a sibling call's round is unwinding).
    The pool thread must decline instead of running the tool."""
    f = _Fanout()
    reg = f.registry()
    sid = mgr.create_session(title="stop-flag")
    ctx = {"session_id": sid}
    mgr.get(sid).cancel_requested = True

    with pytest.raises(asyncio.CancelledError):
        await _execute_single("victim", {}, ctx, reg)
    assert f.ran == []


# --- the queue-to-running boundary ----------------------------------------


@pytest.fixture
def gate_barrier(monkeypatch):
    """Park every pool thread inside claim_for_run so the race is scriptable."""
    real_claim = _DispatchGate.claim_for_run
    at_gate = threading.Event()
    proceed = threading.Event()

    def _slow_claim(self):
        at_gate.set()
        proceed.wait(5)
        return real_claim(self)

    monkeypatch.setattr(_DispatchGate, "claim_for_run", _slow_claim)
    return at_gate, proceed


async def test_a_cancel_that_lands_as_the_thread_picks_the_call_up_wins(one_thread_pool, mgr, gate_barrier):
    """The thread has been handed the work item but has not entered the tool.
    fut.cancel() is already too late here — only the gate can stop it."""
    at_gate, proceed = gate_barrier
    f = _Fanout()
    reg = f.registry()
    ctx = {"session_id": mgr.create_session(title="boundary-cancel")}

    task = asyncio.create_task(_execute_single("victim", {}, ctx, reg))
    await asyncio.to_thread(at_gate.wait, 5)  # dequeued, one instruction short

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    proceed.set()
    await _execute_single("probe", {}, ctx, reg)
    assert f.ran == ["probe"]


async def test_a_cancel_that_lands_after_the_tool_started_does_not_pretend_otherwise(
    one_thread_pool, mgr, gate_barrier
):
    """The mirror case. A thread that won the race is inside the tool and
    cannot be recalled; the fix must not claim it stopped anything."""
    at_gate, proceed = gate_barrier
    f = _Fanout()
    reg = f.registry()
    ctx = {"session_id": mgr.create_session(title="boundary-run")}

    task = asyncio.create_task(_execute_single("hog", {}, ctx, reg))
    await asyncio.to_thread(at_gate.wait, 5)
    proceed.set()  # the claim succeeds — the tool is running
    await asyncio.to_thread(f.hog_started.wait, 5)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    f.release.set()
    await asyncio.sleep(0.1)

    assert f.ran == ["hog"]
    assert reg.metrics["hog"].failure_count == 1, "an in-flight cancel is still recorded against the call"


def test_the_gate_settles_the_race_exactly_once():
    claimed = _DispatchGate()
    assert claimed.claim_for_run() is True
    assert claimed.cancel() is True, "cancel must learn that the tool is already running"

    cancelled = _DispatchGate()
    assert cancelled.cancel() is False, "nothing had started, so there is no child to kill"
    assert cancelled.claim_for_run() is False, "the pool thread must not enter a cancelled dispatch"
