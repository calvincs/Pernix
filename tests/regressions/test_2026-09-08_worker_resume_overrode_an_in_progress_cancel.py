"""Cancelling a parent that was waiting on workers did not stick.

`_resume_from_workers` checked `cancel_requested` once, before it acquired
the parent lock, and then spent a transcript read per worker inside
`asyncio.to_thread`. Neither cancel route takes that lock, so a cancel landing
during the read was invisible to every check the function had already made.
Two different endings, both wrong:

  * the detached resume (reaper stale-purge, boot reconcile) finished the read,
    cleared `cancel_requested` and launched the synthesis turn — restarting the
    work the user had just stopped, and erasing the flag that said so;
  * the ordinary resume runs inside the finishing worker's own task, so the
    cascade's `worker.task.cancel()` aborted it. `CancelledError` is not an
    `Exception`, so it escaped `_finalize_turn`'s handler and left the parent
    parked in AWAITING_WORKERS with `cancel_requested=True`, an empty watch-set
    and no cancel notice. Nothing but the reaper's empty-watch-set safety net
    released it, thirty minutes later, and a prompt sent meanwhile queued
    behind a session that was never going to run.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

import api.routers.sessions as sessions_router
from sessions import state_v2 as sv2
from sessions.manager import SessionManager

RESUME_MSG = "[Watched workers have completed — 1 total]"


@pytest.fixture
def mgr(monkeypatch):
    fresh = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", fresh)
    monkeypatch.setattr(sessions_router, "get_manager", lambda: fresh)
    return fresh


@pytest.fixture
def launched(mgr, monkeypatch):
    """Record every turn the manager dispatches instead of running one."""
    seen: list[str] = []

    async def fake_run_agent_safe(session, message, system_prompt, **kw):
        seen.append(message)
        session._state_v2 = sv2.SessionStateV2.PROCESSING

    monkeypatch.setattr(mgr, "_run_agent_safe", fake_run_agent_safe)
    mgr.set_agent_runner(fake_run_agent_safe)  # prompt() refuses to dispatch without one
    return seen


def _parked_parent(mgr):
    """A parent suspended on await_workers, its one worker already finished."""
    worker_id = mgr.create_session(title="W", session_type="worker")
    parent_id = mgr.create_session(title="P")
    worker = mgr.get(worker_id)
    worker.parent_session_id = parent_id
    worker.termination_reason = "complete"
    worker._state_v2 = sv2.SessionStateV2.IDLE_READY
    parent = mgr.get(parent_id)
    parent.worker_ids = [worker_id]
    parent._watched_worker_ids = {worker_id}
    parent._state_v2 = sv2.SessionStateV2.AWAITING_WORKERS
    return parent, worker


class _Barrier:
    """Hold `_build_resume_message` off-loop, exactly where production does."""

    def __init__(self, mgr, monkeypatch):
        self.entered = asyncio.Event()
        self._release = threading.Event()
        loop = asyncio.get_event_loop()

        def blocking_build(parent):
            loop.call_soon_threadsafe(self.entered.set)
            assert self._release.wait(10), "barrier never released"
            return RESUME_MSG

        monkeypatch.setattr(mgr, "_build_resume_message", blocking_build)

    async def wait(self):
        await asyncio.wait_for(self.entered.wait(), 10)

    def release(self):
        self._release.set()


def _notices(session_id) -> list[str]:
    from db import models as db

    return [m["content"] for m in db.get_messages(session_id) if m["role"] == "notice"]


async def _released(parent, timeout: float = 5.0) -> None:
    """Wait for the release, which runs in a task of its own and writes its
    notice off-loop."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if sv2._current_state(parent) is sv2.SessionStateV2.IDLE_READY and _notices(parent.session_id):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"parent never released: state={sv2._current_state(parent)}")


async def test_a_cancel_during_a_detached_resume_is_not_overridden(mgr, monkeypatch, launched):
    """The reaper/boot path: the resume task survives the cascade, so only the
    re-check inside the lock can stop it."""
    parent, _worker = _parked_parent(mgr)
    barrier = _Barrier(mgr, monkeypatch)

    resume = mgr._spawn_detached(mgr._resume_from_workers(parent), "resume-from-workers")
    await barrier.wait()
    await sessions_router.cancel_session(parent.session_id)
    barrier.release()
    await asyncio.wait_for(resume, 10)
    await _released(parent)

    assert launched == [], "no synthesis turn may start after the cancel"
    assert parent.cancel_requested is True, "the cancel must stay authoritative"
    assert not parent.pending_messages, "and no deferred resume may be queued"
    assert sv2._current_state(parent) is sv2.SessionStateV2.IDLE_READY
    assert not parent._watched_worker_ids


async def test_a_cancel_that_cascades_into_the_resume_still_releases_the_parent(mgr, monkeypatch, launched):
    """The ordinary path: the resume runs inside the worker's task, and the
    cascade cancels it mid-read."""
    parent, worker = _parked_parent(mgr)
    barrier = _Barrier(mgr, monkeypatch)

    worker.task = asyncio.create_task(mgr._on_watched_worker_done(worker))
    await barrier.wait()
    await sessions_router.cancel_session(parent.session_id)
    barrier.release()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(worker.task, 10)
    await _released(parent)

    assert launched == [], "no synthesis turn may start after the cancel"
    assert sv2._current_state(parent) is not sv2.SessionStateV2.AWAITING_WORKERS, "the parent must not stay parked"
    assert sv2._current_state(parent) is sv2.SessionStateV2.IDLE_READY
    assert not parent._watched_worker_ids
    assert any(
        "turn cancelled" in n for n in _notices(parent.session_id)
    ), "the cancel must be visible in the transcript"

    # And the session is usable again — this is what the 30-minute park cost.
    await mgr.prompt(parent.session_id, "never mind, do this instead")
    await asyncio.sleep(0)  # the dispatched turn is a task
    assert launched == ["never mind, do this instead"]


async def test_cancelling_a_parked_parent_with_no_resume_in_flight_releases_it(mgr, launched):
    """No worker is going to call back — the endpoint owns the release."""
    parent, _worker = _parked_parent(mgr)

    await sessions_router.cancel_session(parent.session_id)
    await _released(parent)

    assert not parent._watched_worker_ids
    assert any("turn cancelled" in n for n in _notices(parent.session_id))


async def test_the_manager_cancel_releases_a_parked_parent_too(mgr, launched):
    """cancel_session is the tool/cascade route into the same defect."""
    parent, _worker = _parked_parent(mgr)

    assert mgr.cancel_session(parent) is False  # a parked parent owns no task
    await _released(parent)

    assert not parent._watched_worker_ids


async def test_an_uncancelled_resume_still_starts_its_synthesis_turn(mgr, monkeypatch, launched):
    """Control: the re-check must not block the ordinary resume."""
    parent, _worker = _parked_parent(mgr)
    monkeypatch.setattr(mgr, "_build_resume_message", lambda p: RESUME_MSG)

    await mgr._resume_from_workers(parent)
    await asyncio.sleep(0)  # the synthesis turn is a task

    assert launched == [RESUME_MSG]
