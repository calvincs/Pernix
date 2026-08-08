"""Regression: two agent turns could run concurrently on one session.

Shipped defect (architecture review 2026-08-07, "one turn per session"):
prompt(), _process_pending and _resume_from_workers each assigned
session.task from asyncio.create_task without checking whether the previous
task was still alive. The invariant was carried entirely by the state value,
and _finalize_turn writes IDLE_READY several awaits before it drains the
queue — so a second dispatcher could observe a ready state, launch a second
_run_agent_safe, and have both turns write to the same transcript and both
run _finalize_turn. session.task pointed at only one of them, leaving the
other orphaned and un-cancellable through the API.

Fix: _turn_in_flight() compares session.task against asyncio.current_task(),
so the turn that owns the task can still drain its own queue while every
other caller queues instead. A message queued behind a turn that will never
reach IDLE_READY on its own (parked in AWAITING_USER by ask_user) is
dispatched by _dispatch_after_turn once that task exits.
"""

import asyncio

from sessions.manager import SessionManager
from sessions.state import PendingMessage


def _make_manager() -> SessionManager:
    return SessionManager()


async def test_process_pending_refuses_to_start_a_second_turn(monkeypatch):
    mgr = _make_manager()
    sid = mgr.create_session(title="Concurrent dispatch")
    session = mgr.get(sid)

    dispatched: list[str] = []
    hold = asyncio.Event()

    async def _turn(*args, **kwargs):
        dispatched.append("dispatched")
        await hold.wait()

    monkeypatch.setattr(mgr, "_run_agent_safe", _turn)

    # The exact window _finalize_turn opens: IDLE_READY is already written,
    # the turn that wrote it is still running.
    live = asyncio.create_task(_turn())
    session.task = live
    await asyncio.sleep(0)
    session.pending_messages.append(PendingMessage("queued", "", True, 0.0, None))

    await mgr._process_pending(session)

    assert len(session.pending_messages) == 1, "a second turn was dispatched over a live one"
    assert session.task is live, "session.task was overwritten, orphaning the running turn"

    hold.set()
    await live


async def test_owning_task_can_still_drain_its_own_queue(monkeypatch):
    """The guard must not break the legitimate case: _finalize_turn calls
    _process_pending from inside the very task that owns session.task."""
    mgr = _make_manager()
    sid = mgr.create_session(title="Self drain")
    session = mgr.get(sid)

    dispatched: list[str] = []

    async def _turn(session_arg=None, message="", *args, **kwargs):
        dispatched.append(message)

    monkeypatch.setattr(mgr, "_run_agent_safe", _turn)
    session.pending_messages.append(PendingMessage("queued", "", True, 0.0, None))

    async def _owning_turn():
        await mgr._process_pending(session)

    owner = asyncio.create_task(_owning_turn())
    session.task = owner
    await owner

    follow_on = session.task
    assert follow_on is not owner, "the owning task's own drain was blocked"
    await follow_on
    assert dispatched == ["queued"]


async def test_prompt_queues_behind_a_settling_turn(monkeypatch):
    """IDLE_READY plus a live task means the previous turn is still settling
    post-hooks; the new message must queue, then dispatch when it exits."""
    mgr = _make_manager()
    sid = mgr.create_session(title="Settling turn")
    session = mgr.get(sid)

    started = asyncio.Event()
    hold = asyncio.Event()
    dispatched: list[str] = []

    async def _first_turn():
        await hold.wait()

    async def _turn(session_arg=None, message="", *args, **kwargs):
        dispatched.append(message)
        started.set()

    async def _unused_runner(**kwargs):
        return None

    mgr.set_agent_runner(_unused_runner)
    monkeypatch.setattr(mgr, "_run_agent_safe", _turn)
    live = asyncio.create_task(_first_turn())
    session.task = live
    await asyncio.sleep(0)

    await mgr.prompt(sid, "second message")

    assert session.task is live, "prompt started a second concurrent turn"
    assert len(session.pending_messages) == 1

    hold.set()
    await asyncio.wait_for(started.wait(), timeout=2.0)
    assert dispatched == ["second message"], "the queued message was never dispatched"


async def test_answer_dispatches_after_the_ask_user_turn_settles(monkeypatch):
    """A turn parked in AWAITING_USER never reaches IDLE_READY, so nothing
    else would drain the answer — _dispatch_after_turn has to."""
    from db import models as _db
    from sessions import state_v2 as sv2

    mgr = _make_manager()
    sid = mgr.create_session(title="Answer while settling")
    session = mgr.get(sid)

    _db.add_question(
        sid,
        question="What color?",
        session_title="Answer while settling",
        session_type="normal",
        context="",
        urgency="normal",
        question_type="question",
    )
    sv2.transition(session, sv2.SessionStateV2.SCOUTING, "prompt-arrived")
    sv2.transition(session, sv2.SessionStateV2.PROCESSING, "scout-done")
    sv2.transition(session, sv2.SessionStateV2.AWAITING_USER, "ask-user")

    started = asyncio.Event()
    hold = asyncio.Event()
    dispatched: list[str] = []

    async def _settling_turn():
        await hold.wait()

    async def _turn(session_arg=None, message="", *args, **kwargs):
        dispatched.append(message)
        started.set()

    async def _unused_runner(**kwargs):
        return None

    mgr.set_agent_runner(_unused_runner)
    monkeypatch.setattr(mgr, "_run_agent_safe", _turn)
    live = asyncio.create_task(_settling_turn())
    session.task = live
    await asyncio.sleep(0)

    await mgr.prompt(sid, "[User answered your question] A: blue")

    assert session.task is live
    assert len(session.pending_messages) == 1

    hold.set()
    await asyncio.wait_for(started.wait(), timeout=2.0)
    assert dispatched and dispatched[0].endswith("blue")


async def test_resume_from_workers_queues_behind_a_settling_parent(monkeypatch):
    """A fast worker can complete while the parent's suspended turn is still
    running post-hooks; the synthesis turn must not start on top of it."""
    from sessions import state_v2 as sv2

    mgr = _make_manager()
    sid = mgr.create_session(title="Parent settling")
    parent = mgr.get(sid)

    parent._watched_worker_ids = {"w0"}  # AWAITING_WORKERS invariant
    sv2.transition(parent, sv2.SessionStateV2.SCOUTING, "prompt-arrived")
    sv2.transition(parent, sv2.SessionStateV2.PROCESSING, "scout-done")
    sv2.transition(parent, sv2.SessionStateV2.AWAITING_WORKERS, "workers-dispatched")

    started = asyncio.Event()
    hold = asyncio.Event()
    dispatched: list[str] = []

    async def _settling_turn():
        await hold.wait()

    async def _turn(session_arg=None, message="", *args, **kwargs):
        dispatched.append(message)
        started.set()

    monkeypatch.setattr(mgr, "_run_agent_safe", _turn)
    live = asyncio.create_task(_settling_turn())
    parent.task = live
    await asyncio.sleep(0)

    await mgr._resume_from_workers(parent)

    assert parent.task is live, "resume started a turn over the settling one"
    assert len(parent.pending_messages) == 1

    hold.set()
    await asyncio.wait_for(started.wait(), timeout=2.0)
    assert dispatched and "Watched workers have completed" in dispatched[0]
