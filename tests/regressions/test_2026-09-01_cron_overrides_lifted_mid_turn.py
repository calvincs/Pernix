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
"""

import asyncio
import inspect

import pytest

from core.extensions import scheduling as sched


def test_the_clear_is_bound_to_the_turn_not_the_timer():
    src = inspect.getsource(sched._dispatch_prompt)
    assert "add_done_callback(_clear)" in src, "the overrides must outlive a timed-out wait"
    assert "_created_fresh" in src, "a throwaway session needs no clearing at all"


async def test_a_reused_session_keeps_its_pin_until_the_turn_ends(monkeypatch):
    from sessions.manager import SessionManager

    mgr = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", mgr)
    sid = mgr.create_session(title="attended")
    session = mgr.get(sid)

    running = asyncio.Event()
    finish = asyncio.Event()

    async def turn():
        running.set()
        await finish.wait()

    async def fake_prompt(_sid, _prompt, **kw):
        session.task = asyncio.create_task(turn())

    monkeypatch.setattr(mgr, "prompt", fake_prompt)
    monkeypatch.setattr("config.settings.cron_dispatch_timeout", 0.05)

    await sched._dispatch_prompt(sid, "do the thing", model="pinned/model", allowed_tools=["file_read"])

    # The wait has timed out by now, but the turn is still running.
    await running.wait()
    assert session.model_override == "pinned/model", "the pin must survive a timed-out wait"
    assert session.tool_allowlist is not None

    finish.set()
    await session.task
    await asyncio.sleep(0)
    assert session.model_override is None, "and must be cleared once the turn really ends"
    assert session.tool_allowlist is None


def test_a_deleted_pinned_session_falls_back_instead_of_erroring(monkeypatch):
    from sessions.manager import SessionManager

    mgr = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", mgr)
    out = sched._ensure_dispatch_session("does-not-exist", title="Cron: nightly")
    assert out != "does-not-exist"
    assert mgr.get(out) is not None


def test_an_existing_pinned_session_is_still_reused(monkeypatch):
    from sessions.manager import SessionManager

    mgr = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", mgr)
    sid = mgr.create_session(title="attended")
    assert sched._ensure_dispatch_session(sid, title="Cron: nightly") == sid
