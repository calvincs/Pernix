"""Dismissing a question could 500, or strand the session in AWAITING_USER.

Two defects in the same endpoint, both from routing the dismissal notice
through `manager.prompt`:

* `manager.get_or_create(session_id)` raises `ValueError` when the session
  row is gone — a question left open by a deleted session. The old code
  returned `{"status": "dismissed"}` for that case; the new call turned it
  into an unhandled 500 over a row that just needs deleting.
* When admission is refused (`queue_full`, `shutting_down`, `cancelling`) the
  endpoint raised 409 with the question row still open and the session still
  `AWAITING_USER`. Nothing clears that: the reaper only unsticks an
  `AWAITING_USER` session whose question row is already *gone*, and this path
  is what would have removed it. The user dismissed a question and got a
  session that could not be prompted.

Dismiss has no failure the user can act on. Every path now ends with the row
deleted, the session off `AWAITING_USER` via the declared
`question-dismissed` edge, and the refusal reported as information.
"""

from __future__ import annotations

import pytest

from api.routers.questions import dismiss_question
from db import models as db
from db.database import connect_sessions
from sessions import state_v2 as sv2
from sessions.manager import SessionManager


@pytest.fixture
def mgr(monkeypatch):
    fresh = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", fresh)
    monkeypatch.setattr("sessions.manager.get_manager", lambda: fresh)
    return fresh


def _asking_session(mgr) -> tuple[str, str]:
    """A session parked in AWAITING_USER with one open question."""
    sid = mgr.create_session(title="dismiss")
    session = mgr.get(sid)
    qid = db.add_question(sid, "Which file?")
    sv2.transition(session, sv2.S.SCOUTING, "prompt-arrived")
    session.task = None
    session._state_v2 = sv2.S.PROCESSING
    sv2.transition(session, sv2.S.AWAITING_USER, "ask-user")
    assert sv2._current_state(session) is sv2.S.AWAITING_USER
    return sid, qid


def _open_questions(sid: str) -> list[dict]:
    return db.get_questions(sid)


# ---------------------------------------------------------------------------
# The 500
# ---------------------------------------------------------------------------


async def test_a_question_whose_session_is_gone_is_simply_dismissed(mgr):
    sid, qid = _asking_session(mgr)
    # The session row goes; the question row outlives it (the caller is a tab
    # that still has the dialog open).
    mgr._sessions.pop(sid, None)
    with connect_sessions() as conn:
        conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))

    assert await dismiss_question(qid) == {"status": "dismissed"}
    assert _open_questions(sid) == []


# ---------------------------------------------------------------------------
# The stranded session
# ---------------------------------------------------------------------------


async def test_a_refused_notice_still_dismisses_and_releases_the_session(mgr):
    """shutting_down: the notice cannot be admitted, the dismissal still is."""
    sid, qid = _asking_session(mgr)
    mgr.shutting_down = True

    result = await dismiss_question(qid)

    assert result["status"] == "dismissed"
    assert result["notice_delivered"] is False
    assert result["detail"] == "shutting_down"
    assert _open_questions(sid) == []
    session = mgr.get(sid)
    assert sv2._current_state(session) is sv2.S.IDLE_READY
    hops = [(r["from_state"], r["reason"], r["to_state"]) for r in db.get_state_log(sid)]
    assert ("awaiting_user", "question-dismissed", "idle_ready") in hops


async def test_a_full_queue_does_not_strand_the_session_either(mgr, monkeypatch):
    """queue_full is reached through the settling branch: a task still owns
    the session while it sits in AWAITING_USER."""
    import asyncio

    from config import settings

    sid, qid = _asking_session(mgr)
    session = mgr.get(sid)
    monkeypatch.setattr(settings, "max_pending_messages", 0)
    hold = asyncio.Event()
    session.task = asyncio.create_task(hold.wait())
    try:
        result = await dismiss_question(qid)
    finally:
        hold.set()
        await session.task
        session.task = None

    assert result["status"] == "dismissed"
    assert result["detail"] == "queue_full"
    assert _open_questions(sid) == []
    assert sv2._current_state(session) is sv2.S.IDLE_READY


# ---------------------------------------------------------------------------
# The paths that already worked keep working
# ---------------------------------------------------------------------------


async def test_an_accepted_dismissal_closes_the_question_and_notifies(mgr, monkeypatch):
    sid, qid = _asking_session(mgr)
    ran: list[str] = []

    async def runner(session_id, message, session, is_retry=False, pre_saved=False):
        ran.append(message)

    mgr.set_agent_runner(runner)
    events: list[dict] = []
    monkeypatch.setattr(mgr, "emit", lambda session_id, event: events.append(event))

    assert await dismiss_question(qid) == {"status": "dismissed"}

    task = mgr.get(sid).task
    if task is not None:
        await task

    assert _open_questions(sid) == []
    assert [e["type"] for e in events] == ["dialog.dismissed"]
    assert any("dismissed your question" in m for m in ran)


async def test_a_session_not_awaiting_user_just_drops_the_row(mgr):
    sid = mgr.create_session(title="already moved on")
    qid = db.add_question(sid, "Which file?")
    assert sv2._current_state(mgr.get(sid)) is sv2.S.IDLE_READY

    assert await dismiss_question(qid) == {"status": "dismissed"}
    assert _open_questions(sid) == []


async def test_an_unknown_question_is_still_a_no_op(mgr):
    assert await dismiss_question("no-such-question") == {"status": "dismissed"}
