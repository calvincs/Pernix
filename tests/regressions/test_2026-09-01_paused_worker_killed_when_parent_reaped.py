"""A PAUSED worker was force-cancelled when its parent was merely reaped
from memory (idle, tab closed), not deleted."""

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

async def test_paused_worker_survives_a_parent_that_was_only_reaped(mgr):
    parent_id = mgr.create_session(title="P")
    worker_id = mgr.create_session(title="W", session_type="worker", parent_session_id=parent_id)
    worker = mgr.get(worker_id)
    worker.parent_session_id = parent_id
    worker._state_v2 = sv2.SessionStateV2.PAUSED
    mgr._sessions.pop(parent_id)  # reaped from memory; the row still exists
    mgr.reap_idle_sessions(max_idle=1800)
    assert not worker.cancel_requested, "a reaped (not deleted) parent must not kill a paused worker"
