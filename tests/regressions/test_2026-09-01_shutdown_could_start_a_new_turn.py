"""Shutdown cancelled agent tasks and then guessed at a fixed 0.5s wait.

Cancelling a parent cascades to its workers, whose completion callbacks
resume the parent and dispatch its pending queue — starting a fresh turn
against an LLM client that was about to close and leaving a half-written
SCOUTING row for the next boot to report as an interrupted session. The
manager's shutting_down flag now goes up before the cancels, and the
shutdown awaits the cancellations (bounded) instead of sleeping.

2026-09-08: the flag order was right and the coverage was not. The flag
guarded the two recovery paths and nothing else — `prompt()` never read it —
so a cron fire or a heartbeat tick landing after the snapshot still created a
turn. Shutdown now closes ADMISSION, stops the scheduling producer before
collecting anything, and drains rather than snapshots. These tests drive the
real dispatch paths; the source-order checks that used to stand in for them
are kept only where ordering is the whole point.
"""

import asyncio
import inspect

import pytest

from sessions.manager import SessionManager


@pytest.fixture
def mgr(monkeypatch):
    fresh = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", fresh)
    return fresh


def test_admission_closes_before_any_task_is_collected():
    import api.app as app_mod

    src = inspect.getsource(app_mod.lifespan)
    close = src.index("_mgr.close_admission()")
    drain = src.index("_mgr.drain_turns(")
    assert close < drain, "a dispatch admitted after the collection would never be cancelled"


def test_shutdown_drains_the_turns_instead_of_sleeping_blind():
    import api.app as app_mod

    src = inspect.getsource(app_mod.lifespan)
    assert "_mgr.drain_turns(timeout=5.0)" in src, "and it must stay bounded"


async def test_a_shutting_down_manager_refuses_to_resume_a_parent(mgr, monkeypatch):
    parent_id = mgr.create_session(title="P")
    parent = mgr.get(parent_id)
    parent.pending_messages.append(object())
    started = []
    monkeypatch.setattr(mgr, "_spawn_detached", lambda *a, **k: started.append(a))

    mgr.close_admission()
    await mgr._process_pending(parent)
    await mgr._resume_from_workers(parent)
    assert started == []


async def test_a_shutting_down_manager_refuses_a_new_prompt(mgr):
    """The gate the flag never had: every start path goes through prompt()."""

    async def runner(session_id, message, session, **kw):
        pass

    mgr.set_agent_runner(runner)
    sid = mgr.create_session(title="P")
    mgr.close_admission()

    admission = await mgr.prompt(sid, "one more turn")
    assert admission.rejected
    assert mgr.get(sid).task is None


async def test_the_estimator_warmup_task_is_referenced():
    import api.app as app_mod

    src = inspect.getsource(app_mod.lifespan)
    assert (
        "app.state.warm_estimator_task = asyncio.create_task" in src
    ), "a bare create_task can be garbage-collected mid-await"
