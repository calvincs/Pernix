"""A queued turn started with the previous turn's error still set.

prompt() clears session.error and session.termination_reason before an
immediately dispatched turn; _process_pending did not. So when turn A died
on a provider stream error while turn B waited in the queue, B ran, finished
clean — and still carried A's error. Reflect and evaluate both return at
their `if session_obj.error` guard, so B, the only turn of the pair whose
hooks ran at all (A's were skipped because the queue was non-empty), got no
verification whatsoever. The stale error also survived into further queued
turns.

_process_pending now clears both fields at the same point prompt() does.
"""

import asyncio

import pytest

from sessions import hooks as hooks_mod
from sessions import state_v2 as sv2
from sessions.manager import SessionManager

STREAM_ERROR = "provider stream failed after retries: 503"


class _GuardPassed(Exception):
    """Raised by the trip-wire turn the first time reflect/evaluate reads
    session_obj.turn — which happens only after the error guard."""


class _TripTurn:
    def __getattr__(self, name):
        raise _GuardPassed(name)


@pytest.fixture
def mgr(monkeypatch):
    fresh = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", fresh)
    # Every follow-up must open its own turn rather than fold into the
    # running turn's row — the queue is what this test is about.
    monkeypatch.setattr("sessions.manager.RAPID_FIRE_WINDOW_SECONDS", 0.0)

    async def passthrough_scout(*a, **kw):
        from core.scout.report import ScoutReport

        return ScoutReport(approach_guidance="x")

    monkeypatch.setattr("core.scout.runner.run_scout", passthrough_scout)
    monkeypatch.setattr("core.scout.runner.build_session_brief", lambda *a, **kw: "")
    return fresh


async def _settle(session, runs, expected):
    for _ in range(500):
        await asyncio.sleep(0.02)
        if (
            runs == expected
            and session.task is not None
            and session.task.done()
            and not session.pending_messages
            and sv2._current_state(session) is sv2.SessionStateV2.IDLE_READY
        ):
            return
    raise AssertionError(f"turns did not settle: runs={runs} state={sv2._current_state(session).value}")


async def test_a_queued_turn_after_a_failed_one_starts_clean(mgr, monkeypatch):
    from db import models as db

    hold_a = asyncio.Event()
    runs: list[str] = []

    async def runner(session_id, message, session, is_retry=False, pre_saved=False):
        runs.append(message)
        # The real loop persists an assistant row, so the post-turn orphan
        # sweep doesn't re-queue the user message.
        db.add_message(session_id, "assistant", f"reply to {message}")
        if message == "A":
            await hold_a.wait()
            # What core.agent._end_turn_on_stream_error leaves behind.
            session.error = STREAM_ERROR
            session.termination_reason = "error"
        else:
            session.termination_reason = "complete"

    mgr.set_agent_runner(runner)
    sid = mgr.create_session(title="failed turn, queued turn")
    session = mgr.get(sid)

    observed: list[dict] = []

    async def spy_hooks(session_id, emit=None, session_obj=None):
        observed.append({"after": runs[-1], "error": session_obj.error})

    monkeypatch.setattr("sessions.hooks.run_post_task_hooks", spy_hooks)

    await mgr.prompt(sid, "A")
    for _ in range(500):
        await asyncio.sleep(0.02)
        if runs == ["A"]:
            break
    assert runs == ["A"], runs

    await mgr.prompt(sid, "B")
    assert len(session.pending_messages) == 1, "B should be queued behind A"

    hold_a.set()
    await _settle(session, runs, ["A", "B"])

    # B's own outcome, and none of A's.
    assert session.termination_reason == "complete"
    assert session.error is None

    # A's hooks never ran (queue non-empty), so B's pass is the only
    # verification either turn gets — and it saw a clean session.
    assert [o["after"] for o in observed] == ["B"], observed
    assert observed[0]["error"] is None

    # The real guards: both now reach past `if session_obj.error`.
    session_row = db.get_session(sid)
    session.turn = _TripTurn()
    passed = []
    for fn in (hooks_mod._maybe_reflect, hooks_mod._maybe_evaluate):
        try:
            await fn(sid, session_row, emit=None, session_obj=session)
        except _GuardPassed:
            passed.append(fn.__name__)
    assert passed == ["_maybe_reflect", "_maybe_evaluate"]


async def test_the_error_does_not_propagate_down_three_queued_turns(mgr):
    """A → error, then B, C and D each queued behind the turn before it.
    Every one of them enters through _process_pending, so a stale error would
    ride the whole chain."""
    from db import models as db

    holds = {name: asyncio.Event() for name in "ABCD"}
    runs: list[str] = []
    seen_on_entry: list[tuple[str, str | None]] = []

    async def runner(session_id, message, session, is_retry=False, pre_saved=False):
        runs.append(message)
        seen_on_entry.append((message, session.error))
        db.add_message(session_id, "assistant", f"reply to {message}")
        await holds[message].wait()
        if message == "A":
            session.error = STREAM_ERROR
            session.termination_reason = "error"
        else:
            session.termination_reason = "complete"

    mgr.set_agent_runner(runner)
    sid = mgr.create_session(title="three behind a failure")
    session = mgr.get(sid)

    async def _wait_until_running(expected):
        for _ in range(500):
            await asyncio.sleep(0.02)
            if runs == expected:
                return
        raise AssertionError(f"expected {expected}, got {runs}")

    await mgr.prompt(sid, "A")
    await _wait_until_running(["A"])

    for prev, nxt in (("A", "B"), ("B", "C"), ("C", "D")):
        await mgr.prompt(sid, nxt)
        assert len(session.pending_messages) == 1, f"{nxt} should be queued behind {prev}"
        holds[prev].set()
        await _wait_until_running(runs[: runs.index(prev) + 1] + [nxt])

    holds["D"].set()
    await _settle(session, runs, ["A", "B", "C", "D"])

    # A's error never reached B, and B's clean start never had to be undone
    # again for C or D.
    assert seen_on_entry == [("A", None), ("B", None), ("C", None), ("D", None)]
    assert session.error is None
    assert session.termination_reason == "complete"
