"""Cancelling a session dropped its queued messages, then ran them anyway.

Queued prompts are persisted the moment they are queued, so the UI can show
them and so a restart can recover them. Cancel cleared the in-memory queue
and wrote a "[N queued message(s) dropped]" notice, but left the rows
untouched, and get_orphaned_user_messages knew nothing about notices. The
next unrelated prompt's Window B sweep therefore read the cancelled work as
work a restart had lost and dispatched it first.

The running turn's own user row was the worse half: cancel during scout or
the first stream leaves it with no assistant row behind it — exactly the
orphan shape — so the prompt the user had just stopped was the one that
re-ran.

Both cancel paths now stamp metadata.cancelled=true on every row they drop,
through one shared manager helper, and orphan recovery skips stamped rows.
The text stays in the transcript.
"""

import asyncio
import json

import pytest

import api.routers.sessions as sessions_router
from db import models as db
from sessions import state_v2 as sv2
from sessions.manager import SessionManager

RUNNING = "the running turn"


@pytest.fixture
def mgr(monkeypatch):
    fresh = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", fresh)
    monkeypatch.setattr("api.routers.sessions.get_manager", lambda: fresh)
    # Each follow-up must open its own queue entry rather than fold into the
    # running turn's row.
    monkeypatch.setattr("sessions.manager.RAPID_FIRE_WINDOW_SECONDS", 0.0)

    async def passthrough_scout(*a, **kw):
        from core.scout.report import ScoutReport

        return ScoutReport(approach_guidance="x")

    monkeypatch.setattr("core.scout.runner.run_scout", passthrough_scout)
    monkeypatch.setattr("core.scout.runner.build_session_brief", lambda *a, **kw: "")
    return fresh


def _install_runner(mgr, dispatched, hold, *, answer_the_running_turn):
    """An agent stub that parks on the first turn and answers the rest.

    answer_the_running_turn writes the parked turn's assistant row before it
    parks — a cancel landing mid tool-round. False is a cancel during scout or
    the first stream, where nothing has been written yet.
    """

    async def runner(session_id, message, session, is_retry=False, pre_saved=False):
        dispatched.append(message)
        if message == RUNNING:
            if answer_the_running_turn:
                db.add_message(session_id, "assistant", "working on it...")
            await hold.wait()
        else:
            db.add_message(session_id, "assistant", f"reply to {message}")

    mgr.set_agent_runner(runner)


async def _wait_for(predicate, what):
    for _ in range(500):
        await asyncio.sleep(0.02)
        if predicate():
            return
    raise AssertionError(f"timed out waiting for {what}")


async def _start_and_queue(mgr, sid, dispatched):
    """Run one turn, park it, and queue two follow-ups behind it."""
    session = mgr.get(sid)
    await mgr.prompt(sid, RUNNING)
    await _wait_for(lambda: dispatched == [RUNNING], "the first turn to reach the agent")
    await mgr.prompt(sid, "queued-A")
    await mgr.prompt(sid, "queued-B")
    assert [e.message for e in session.pending_messages] == ["queued-A", "queued-B"]
    return session


async def _await_cancelled(session):
    if session.task:
        try:
            await session.task
        except asyncio.CancelledError:
            pass
    await _wait_for(
        lambda: sv2._current_state(session) is sv2.SessionStateV2.IDLE_READY,
        "the cancelled turn to settle",
    )


async def _next_prompt_runs_alone(mgr, sid, dispatched):
    session = mgr.get(sid)
    dispatched.clear()
    await mgr.prompt(sid, "an unrelated new request")
    await _wait_for(
        lambda: session.task is not None and session.task.done() and not session.pending_messages,
        "the new request to finish",
    )
    assert dispatched == ["an unrelated new request"], dispatched


async def test_the_manager_cancel_path_retires_the_queued_rows(mgr):
    """cancel_session + a cancel that lands mid tool-round."""
    dispatched: list[str] = []
    hold = asyncio.Event()
    _install_runner(mgr, dispatched, hold, answer_the_running_turn=True)
    sid = mgr.create_session(title="manager cancel")
    session = await _start_and_queue(mgr, sid, dispatched)

    assert mgr.cancel_session(session) is True
    assert not session.pending_messages
    await _await_cancelled(session)

    # The rows are still in the transcript, and are no longer recoverable work.
    contents = [m["content"] for m in db.get_messages(sid) if m["role"] == "user"]
    assert "queued-A" in contents and "queued-B" in contents
    assert db.get_orphaned_user_messages(sid) == []

    await _next_prompt_runs_alone(mgr, sid, dispatched)
    hold.set()


async def test_the_http_cancel_route_retires_the_queued_rows(mgr):
    """The /cancel route, and a cancel with no assistant row written yet — so
    the cancelled prompt that was actually running is at stake too."""
    dispatched: list[str] = []
    hold = asyncio.Event()
    _install_runner(mgr, dispatched, hold, answer_the_running_turn=False)
    sid = mgr.create_session(title="route cancel")
    session = await _start_and_queue(mgr, sid, dispatched)
    running_msg_id = session.current_turn_user_msg_id
    assert running_msg_id is not None

    assert await sessions_router.cancel_session(sid) == {"status": "cancelled"}
    assert not session.pending_messages
    await _await_cancelled(session)

    assert db.get_orphaned_user_messages(sid) == []
    running_meta = db.get_message(running_msg_id)["metadata"]
    assert json.loads(running_meta)["cancelled"] is True

    await _next_prompt_runs_alone(mgr, sid, dispatched)
    hold.set()


async def test_the_stamp_survives_a_reload_of_the_session(mgr, monkeypatch):
    """A restart drops the in-memory queue but keeps every row, so the
    guarantee has to live in the DB, not in the session object."""
    dispatched: list[str] = []
    hold = asyncio.Event()
    _install_runner(mgr, dispatched, hold, answer_the_running_turn=False)
    sid = mgr.create_session(title="cancel then restart")
    session = await _start_and_queue(mgr, sid, dispatched)

    assert await sessions_router.cancel_session(sid) == {"status": "cancelled"}
    await _await_cancelled(session)
    hold.set()

    # A fresh manager rebuilding the session from the DB, as a restart does.
    restored = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", restored)
    monkeypatch.setattr("api.routers.sessions.get_manager", lambda: restored)
    reloaded = restored.get_or_create(sid)
    assert not reloaded.pending_messages
    assert reloaded.current_turn_user_msg_id is None

    after: list[str] = []

    async def plain_runner(session_id, message, session, is_retry=False, pre_saved=False):
        after.append(message)
        db.add_message(session_id, "assistant", f"reply to {message}")

    restored.set_agent_runner(plain_runner)
    await _next_prompt_runs_alone(restored, sid, after)
