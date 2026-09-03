"""Cancelling an orchestrating session restarted a synthesis turn.

The cancel cascade only called task.cancel() on each worker, so the
worker's executor swallowed it and its bash child ran on. The parent
itself finalized to IDLE_READY (resetting cancel_requested) before the
workers unwound, and when they finally reported in, the Gap-1 idle
resume queued "[Watched workers have completed ... CANCELLED]" and
started a fresh turn re-planning the work the user had just stopped.
"""

import asyncio

import pytest

from sessions import state_v2 as sv2
from sessions.manager import SessionManager


@pytest.fixture
def mgr(monkeypatch):
    fresh = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", fresh)
    return fresh


def _parent_and_worker(mgr):
    worker_id = mgr.create_session(title="W", session_type="worker", parent_session_id=None)
    parent_id = mgr.create_session(title="P")
    worker = mgr.get(worker_id)
    worker.parent_session_id = parent_id
    parent = mgr.get(parent_id)
    parent.worker_ids = [worker_id]
    return parent, worker


async def test_cancelled_worker_does_not_resume_an_idle_parent(mgr, monkeypatch):
    parent, worker = _parent_and_worker(mgr)
    parent._watched_worker_ids = set()  # the parent's cancel branch already cleared it
    parent._state_v2 = sv2.SessionStateV2.IDLE_READY
    worker.termination_reason = "cancelled"
    resumed = []

    async def fake_resume(p):
        resumed.append(p.session_id)

    monkeypatch.setattr(mgr, "_resume_from_workers", fake_resume)
    await mgr._on_watched_worker_done(worker)
    assert resumed == []


async def test_a_completed_unwatched_worker_still_gets_the_gap1_resume(mgr, monkeypatch):
    parent, worker = _parent_and_worker(mgr)
    parent._watched_worker_ids = set()
    parent._state_v2 = sv2.SessionStateV2.IDLE_READY
    worker.termination_reason = "complete"
    resumed = []

    async def fake_resume(p):
        resumed.append(p.session_id)

    monkeypatch.setattr(mgr, "_resume_from_workers", fake_resume)
    await mgr._on_watched_worker_done(worker)
    assert resumed == [parent.session_id]


async def test_cancelled_worker_still_resumes_a_parent_parked_in_awaiting_workers(mgr, monkeypatch):
    parent, worker = _parent_and_worker(mgr)
    parent._watched_worker_ids = {worker.session_id}
    parent._state_v2 = sv2.SessionStateV2.AWAITING_WORKERS
    worker.termination_reason = "cancelled"
    resumed = []

    async def fake_resume(p):
        resumed.append(p.session_id)

    monkeypatch.setattr(mgr, "_resume_from_workers", fake_resume)
    await mgr._on_watched_worker_done(worker)
    assert resumed == [parent.session_id], "a parked parent must still be released"


async def test_cancel_session_sets_the_flag_clears_the_queue_and_cancels_the_task(mgr):
    parent, worker = _parent_and_worker(mgr)
    worker.task = asyncio.create_task(asyncio.sleep(30))
    worker.pending_messages.append(object())
    await asyncio.sleep(0)
    assert mgr.cancel_session(parent) is False  # parent had no task of its own
    assert worker.cancel_requested, "the cascade must set the cooperative flag"
    assert not worker.pending_messages
    with pytest.raises(asyncio.CancelledError):
        await worker.task


async def test_cancel_worker_tool_uses_the_shared_cancel(mgr):
    from core.extensions.orchestration import cancel_worker

    parent, worker = _parent_and_worker(mgr)
    worker.task = asyncio.create_task(asyncio.sleep(30))
    await asyncio.sleep(0)
    out = cancel_worker(worker.session_id, {"_loop": asyncio.get_running_loop()})
    assert "cancelled" in out
    assert worker.cancel_requested
    with pytest.raises(asyncio.CancelledError):
        await worker.task
