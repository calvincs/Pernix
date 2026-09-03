"""Shutdown cancelled agent tasks and then guessed at a fixed 0.5s wait.

Cancelling a parent cascades to its workers, whose completion callbacks
resume the parent and dispatch its pending queue — starting a fresh turn
against an LLM client that was about to close and leaving a half-written
SCOUTING row for the next boot to report as an interrupted session. The
manager's shutting_down flag now goes up before the cancels, and the
shutdown awaits the cancellations (bounded) instead of sleeping.
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


def test_the_flag_is_raised_before_any_task_is_cancelled():
    import api.app as app_mod

    src = inspect.getsource(app_mod.lifespan)
    flag = src.index("shutting_down = True")
    cancel = src.index("s.task.cancel()")
    assert flag < cancel, "a cascade fired before the flag would still start a turn"


def test_shutdown_awaits_the_cancellations_instead_of_sleeping_blind():
    import api.app as app_mod

    src = inspect.getsource(app_mod.lifespan)
    assert "asyncio.gather(*cancelled, return_exceptions=True)" in src
    assert "timeout=5.0" in src, "and it must stay bounded"


async def test_a_shutting_down_manager_refuses_to_resume_a_parent(mgr, monkeypatch):
    parent_id = mgr.create_session(title="P")
    parent = mgr.get(parent_id)
    parent.pending_messages.append(object())
    started = []
    monkeypatch.setattr(mgr, "_spawn_detached", lambda *a, **k: started.append(a))

    mgr.shutting_down = True
    await mgr._process_pending(parent)
    await mgr._resume_from_workers(parent)
    assert started == []


async def test_the_estimator_warmup_task_is_referenced():
    import api.app as app_mod

    src = inspect.getsource(app_mod.lifespan)
    assert (
        "app.state.warm_estimator_task = asyncio.create_task" in src
    ), "a bare create_task can be garbage-collected mid-await"
