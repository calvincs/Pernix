"""An unwatched worker's result was dropped when its idle parent had been
reaped from memory; the row still existed."""

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

async def test_unwatched_worker_result_revives_a_reaped_parent(mgr, monkeypatch):
    parent_id = mgr.create_session(title="P")
    worker_id = mgr.create_session(title="W", session_type="worker", parent_session_id=parent_id)
    worker = mgr.get(worker_id)
    worker.parent_session_id = parent_id
    worker.termination_reason = "complete"
    mgr._sessions.pop(parent_id)
    resumed = []

    async def fake_resume(p):
        resumed.append(p.session_id)

    monkeypatch.setattr(mgr, "_resume_from_workers", fake_resume)
    await mgr._on_watched_worker_done(worker)
    assert resumed == [parent_id]
    assert mgr.get(parent_id) is not None
