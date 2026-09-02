"""delete_session cancelled the task but not its subprocesses, and the
space cascade ran the loop-affine function on a worker thread. It is now
two-phase: stop the turn on the loop, purge DB/files off it."""

import asyncio

import pytest

from db import models as db
from sessions import state_v2 as sv2
from sessions.manager import SessionManager


@pytest.fixture
def mgr(monkeypatch):
    fresh = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", fresh)
    return fresh


async def test_delete_session_async_stops_the_turn_then_purges_off_loop(mgr):
    sid = mgr.create_session(title="doomed")
    session = mgr.get(sid)
    session.task = asyncio.create_task(asyncio.sleep(30))
    await asyncio.sleep(0)
    await mgr.delete_session_async(sid)
    assert session.cancel_requested
    with pytest.raises(asyncio.CancelledError):
        await session.task
    assert mgr.get(sid) is None
    assert db.get_session(sid) is None


async def test_delete_cascades_in_memory_workers(mgr):
    parent_id = mgr.create_session(title="P")
    worker_id = mgr.create_session(title="W", session_type="worker", parent_session_id=parent_id)
    mgr.get(parent_id).worker_ids = [worker_id]
    await mgr.delete_session_async(parent_id)
    assert mgr.get(worker_id) is None
    assert db.get_session(worker_id) is None
