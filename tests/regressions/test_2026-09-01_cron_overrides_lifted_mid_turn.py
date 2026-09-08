"""A long cron run lost its tool allow-list and its model pin mid-turn.

_dispatch_prompt cleared session.tool_allowlist and session.model_override
in a `finally` that ran when the SHIELDED WAIT gave up at
cron_dispatch_timeout — not when the dispatched turn ended. An
orchestrating job running past the hour therefore continued with the full
tool surface, and its next LLM call fell back to the default model, which
breaks the rule that the harness never switches models on its own.

Also: a job pinned to a session the user later deleted returned that id
anyway, so manager.prompt raised on every tick — an error row and a
high-urgency notification each time, 96 a day for a */15 job, forever.

2026-09-08: the timing was fixed then, the ownership was not. The options
were still written onto the SESSION, before prompt() had decided whether the
message would start a turn or queue behind one — see
test_2026-09-08_a_cron_job_pinned_the_users_own_turn.py for what that cost.
They now ride with the admitted message and are applied at the boundary of
the turn that owns them. These tests drive the real manager: the old fake
prompt() always created a fresh task, so the queued case — the one that
actually broke — could not occur in them.
"""

import asyncio

import pytest

from core.extensions import scheduling as sched
from sessions.manager import SessionManager


@pytest.fixture
def mgr(monkeypatch):
    fresh = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", fresh)
    return fresh


async def test_a_reused_session_keeps_its_pin_until_the_turn_ends(mgr, monkeypatch):
    sid = mgr.create_session(title="attended")
    session = mgr.get(sid)
    running = asyncio.Event()
    finish = asyncio.Event()

    async def runner(session_id, message, session, **kw):
        running.set()
        await finish.wait()

    mgr.set_agent_runner(runner)
    monkeypatch.setattr("config.settings.cron_dispatch_timeout", 0.2)

    result = await sched._dispatch_prompt(sid, "do the thing", model="pinned/model", allowed_tools=["file_read"])

    # The wait has timed out by now, but the turn is still running.
    await running.wait()
    assert result.status == sched.DISPATCH_UNRESOLVED, "a wait that expired is not an outcome"
    assert session.model_override == "pinned/model", "the pin must survive a timed-out wait"
    assert session.tool_allowlist is not None

    finish.set()
    await asyncio.wait({session.task})
    await asyncio.sleep(0)
    assert session.model_override is None, "and must be cleared once the turn really ends"
    assert session.tool_allowlist is None


async def test_a_fresh_dispatch_session_is_still_constrained_for_its_turn(mgr):
    """A throwaway session used to skip the clear entirely as an optimization.

    It was also unreachable: the only production caller resolves the session
    before dispatching, so `_created_fresh` was never true. The charter now
    applies the same way whoever owns the session.
    """
    seen = {}

    async def runner(session_id, message, session, **kw):
        seen["allow"] = session.tool_allowlist
        seen["model"] = session.model_override

    mgr.set_agent_runner(runner)

    result = await sched._dispatch_prompt(None, "do the thing", title="Cron: nightly", allowed_tools=["recall"])
    assert result.completed
    assert seen["allow"] == frozenset({"recall"})
    assert seen["model"] is None
    assert mgr.get(result.session_id).tool_allowlist is None


def test_a_deleted_pinned_session_falls_back_instead_of_erroring(mgr):
    out = sched._ensure_dispatch_session("does-not-exist", title="Cron: nightly")
    assert out != "does-not-exist"
    assert mgr.get(out) is not None


def test_an_existing_pinned_session_is_still_reused(mgr):
    sid = mgr.create_session(title="attended")
    assert sched._ensure_dispatch_session(sid, title="Cron: nightly") == sid
