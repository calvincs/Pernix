"""A cron job's prompt was appended to the sentence the user had just typed.

The rapid-fire combiner folds a message that lands within
RAPID_FIRE_WINDOW_SECONDS of the previous one into that message's DB row, so
three quick corrections become one turn instead of three. It keyed on
recency alone. A scheduled job firing inside that three-second window was
therefore not queued and not run: `db.update_message_content` rewrote the
user's own row to

    [Combined rapid-fire messages]

    1. what is the weather

    2. cron work

and `prompt()` returned. The machine's instructions were injected into a
human's turn — and, before the ownership fix, that same turn also ran under
the job's model pin and tool charter. The cron row said `completed`. Nothing
had run.

Folding is now a thing only a person's own follow-up may do. Anything that
arrives with per-turn execution options, or from a producer other than the
user, gets its own turn.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from core.extensions import scheduling as sched
from db import models as db
from sessions import state_v2 as sv2
from sessions.manager import _COMBINED_PREFIX, SessionManager


@pytest.fixture
def busy(monkeypatch):
    """A session whose user message landed a moment ago and is mid-turn."""
    mgr = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", mgr)
    sid = mgr.create_session(title="a session in use")
    session = mgr.get(sid)
    return mgr, sid, session


async def test_a_cron_prompt_is_never_appended_to_the_users_message(busy):
    mgr, sid, session = busy
    running = asyncio.Event()
    release = asyncio.Event()
    ran: list[str] = []

    async def runner(session_id, message, session, **kw):
        ran.append(message)
        if message == "what is the weather":
            running.set()
            await release.wait()

    mgr.set_agent_runner(runner)
    await mgr.prompt(sid, "what is the weather")
    await running.wait()

    user_row = session.current_turn_user_msg_id
    assert user_row is not None
    # Hold the window open across the scout phase, so the job arrives under
    # exactly the condition that used to absorb it rather than passing this
    # test by having taken too long.
    session.last_user_msg_at = time.monotonic()
    job = asyncio.create_task(sched._dispatch_prompt(sid, "cron work"))
    while not session.pending_messages:
        await asyncio.sleep(0)

    assert db.get_message(user_row)["content"] == "what is the weather"
    assert _COMBINED_PREFIX not in db.get_message(user_row)["content"]

    release.set()
    result = await asyncio.wait_for(job, timeout=10)
    assert result.completed
    assert ran == ["what is the weather", "cron work"], "the job needs a turn, not a paragraph"


async def test_a_users_own_follow_up_is_still_combined(busy):
    """The combiner is not disabled — it is only narrowed to the human."""
    mgr, sid, session = busy
    running = asyncio.Event()
    release = asyncio.Event()
    ran: list[str] = []

    async def runner(session_id, message, session, **kw):
        ran.append(message)
        if message == "summarise report.txt":
            running.set()
            await release.wait()

    mgr.set_agent_runner(runner)
    await mgr.prompt(sid, "summarise report.txt")
    await running.wait()
    user_row = session.current_turn_user_msg_id
    session.last_user_msg_at = time.monotonic()  # same window, human follow-up

    admission = await mgr.prompt(sid, "actually three lines")
    assert admission.outcome == "absorbed"
    assert not session.pending_messages
    assert "actually three lines" in db.get_message(user_row)["content"]

    release.set()
    await asyncio.wait({session.task})
    assert ran == ["summarise report.txt"]


async def test_an_absorbed_message_is_not_reported_as_a_completed_run(busy):
    """Absorption is a terminal outcome, and it is not success."""
    mgr, sid, session = busy
    session._state_v2 = sv2.SessionStateV2.PROCESSING
    row = db.add_message(sid, "user", "what is the weather")
    session.last_user_msg_id = row
    session.current_turn_user_msg_id = row
    session.last_user_msg_at = time.monotonic()

    admission = await mgr.prompt(sid, "and also this")
    assert admission.outcome == "absorbed"
    assert admission.execution.result == "absorbed"
    assert not admission.execution.succeeded


async def test_a_job_with_no_options_is_still_not_absorbed(busy):
    """Origin decides, not the presence of a charter.

    A heartbeat tick carries no model and no allow-list, and it was folded
    into the user's row just the same.
    """
    mgr, sid, session = busy
    session._state_v2 = sv2.SessionStateV2.PROCESSING
    row = db.add_message(sid, "user", "what is the weather")
    session.last_user_msg_id = row
    session.current_turn_user_msg_id = row
    session.last_user_msg_at = time.monotonic()

    admission = await mgr.prompt(sid, "[heartbeat:nudge] check in", origin="scheduled")
    assert admission.outcome == "queued"
    assert db.get_message(row)["content"] == "what is the weather"
    assert len(session.pending_messages) == 1
