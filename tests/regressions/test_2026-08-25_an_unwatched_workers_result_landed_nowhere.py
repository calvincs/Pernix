"""A worker spawned without auto_resume_parent finished after its parent's
turn hit the round cap — and the result landed nowhere (field case: cd82
parent 41e10cf3c7bd, worker ef7758503a20, 2026-08-25).

The parent spawned the worker mid-turn without the flag, planned to wait,
then ran out of rounds and returned to IDLE_READY with an empty watch-set.
_on_watched_worker_done early-returned for unwatched workers, so worker
completion triggered no resume and no notification: the finished transcript
just sat there. An unwatched worker completing while its parent watches
nothing and sits idle now routes through the documented Gap-1 idle-resume.
Parents that are mid-turn, awaiting the user, or deliberately watching
other workers keep the old semantics.
"""

from __future__ import annotations

import pytest

from sessions import state_v2 as sv2
from sessions.manager import SessionManager


@pytest.fixture
def mgr(monkeypatch):
    fresh = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", fresh)
    return fresh


def _parent_and_unwatched_worker(mgr, parent_state):
    parent_id = mgr.create_session(title="P")
    worker_id = mgr.create_session(title="W", session_type="worker")
    worker = mgr.get(worker_id)
    worker.parent_session_id = parent_id
    parent = mgr.get(parent_id)
    parent.worker_ids = [worker_id]
    parent._watched_worker_ids = set()  # spawned without auto_resume_parent
    parent._state_v2 = parent_state
    return parent_id, worker


async def test_idle_parent_resumes_on_unwatched_worker_done(mgr, monkeypatch):
    parent_id, worker = _parent_and_unwatched_worker(mgr, sv2.SessionStateV2.IDLE_READY)
    resumes = []

    async def fake_resume(p):
        resumes.append(p.session_id)

    monkeypatch.setattr(mgr, "_resume_from_workers", fake_resume)
    await mgr._on_watched_worker_done(worker)
    assert resumes == [parent_id]


async def test_processing_parent_is_left_alone(mgr, monkeypatch):
    _, worker = _parent_and_unwatched_worker(mgr, sv2.SessionStateV2.PROCESSING)
    resumes = []

    async def fake_resume(p):
        resumes.append(p.session_id)

    monkeypatch.setattr(mgr, "_resume_from_workers", fake_resume)
    await mgr._on_watched_worker_done(worker)
    assert resumes == []


async def test_awaiting_user_parent_is_left_alone(mgr, monkeypatch):
    _, worker = _parent_and_unwatched_worker(mgr, sv2.SessionStateV2.AWAITING_USER)
    resumes = []

    async def fake_resume(p):
        resumes.append(p.session_id)

    monkeypatch.setattr(mgr, "_resume_from_workers", fake_resume)
    await mgr._on_watched_worker_done(worker)
    assert resumes == []


async def test_parent_watching_other_workers_keeps_old_semantics(mgr, monkeypatch):
    parent_id, worker = _parent_and_unwatched_worker(mgr, sv2.SessionStateV2.IDLE_READY)
    parent = mgr.get(parent_id)
    parent._watched_worker_ids = {"some-other-worker"}
    resumes = []

    async def fake_resume(p):
        resumes.append(p.session_id)

    monkeypatch.setattr(mgr, "_resume_from_workers", fake_resume)
    await mgr._on_watched_worker_done(worker)
    assert resumes == []
