"""Agent Mesh build, 2026-09-07: `_handle_stuck_signals` gave ask_user-capable
sessions a nudge budget and everyone else an immediate stop. All four research
workers were force-broken on the first trip of the detector, three of them
before writing the report file they existed to produce, and each cost a full
reflect retry that redid the research from scratch.

Two fixes, both covered here:
  * every session gets the same nudge budget; only the instruction differs,
    and the no-ask_user instruction names the file deliverable;
  * the stop is reported as `stuck_loop`, not `round_ceiling` — one worker
    died at round 4 of a 500-round budget and reflect told it to raise
    max_tool_rounds.
"""

from __future__ import annotations

import pytest

from core.agent import _handle_stuck_signals


@pytest.fixture
def captured(monkeypatch):
    """Collect system messages instead of writing them to the DB."""
    written: list[str] = []

    def _add_message(session_id, role, content, **kw):
        written.append(content)

    monkeypatch.setattr("db.models.add_message", _add_message)
    return written


async def _fire(active_tools, nudges_used, captured, repeats=3):
    return await _handle_stuck_signals(
        session_id="s1",
        score=0.9,
        repeats=repeats,
        tool_calls=[{"name": "http_get", "arguments": "{}"}],
        active_tools=active_tools,
        nudges_used=nudges_used,
        nudge_limit=3,
        turn_user_msg_id=42,
    )


async def test_a_worker_without_ask_user_gets_nudged_not_killed(captured):
    """The Agent Mesh shape: a research worker's allowlist has no ask_user."""
    action, used = await _fire(["http_get", "file_write"], 0, captured)
    assert action == "nudge-and-retry"
    assert used == 1
    assert "WRITE YOUR DELIVERABLE" in captured[-1]
    assert "ask_user" not in captured[-1]


async def test_the_nudge_names_what_was_being_repeated(captured):
    await _fire(["http_get", "file_write"], 0, captured)
    assert "http_get" in captured[-1]


async def test_a_session_with_ask_user_is_still_told_to_ask(captured):
    action, used = await _fire(["http_get", "ask_user"], 0, captured)
    assert action == "nudge-and-retry"
    assert "ask_user" in captured[-1]


async def test_the_budget_is_finite_for_a_worker_too(captured):
    """Past the cap it still stops — but the last word is 'write the file'."""
    action, used = await _fire(["http_get", "file_write"], 3, captured)
    assert action == "stop"
    assert "file deliverable" in captured[-1]


async def test_the_budget_is_finite_with_ask_user_too(captured):
    action, _ = await _fire(["http_get", "ask_user"], 3, captured)
    assert action == "stop"


async def test_a_mild_repeat_still_only_warns(captured):
    action, used = await _fire(["http_get"], 0, captured, repeats=1)
    assert action == "proceed"
    assert used == 0
    assert "repeating tool calls" in captured[-1]


def test_stuck_loop_is_its_own_termination_reason():
    """Not round_ceiling: the detector fires at any round number, and reflect's
    round_ceiling advice ('raise max_tool_rounds') is wrong for it."""
    from sessions.manager import _map_termination_to_v2_reason
    from sessions.state_v2 import TerminationReason

    reason, enum_value = _map_termination_to_v2_reason("stuck_loop")
    assert reason == "stuck-loop"
    assert enum_value is TerminationReason.STUCK_LOOP
    assert enum_value is not TerminationReason.ROUND_CEILING


def test_reflect_advises_a_different_approach_for_a_stuck_loop():
    """The two loop walls must not share advice."""
    import core.reflect as reflect

    assert "stuck_loop" in reflect.REFLECT_PROMPT
    assert "never advise raising max_tool_rounds for it" in reflect.REFLECT_PROMPT


def test_the_stuck_loop_transition_is_legal():
    """A reason string missing from the graph would make the turn's own exit
    an illegal transition."""
    from sessions.state_v2 import TRANSITIONS
    from sessions.state_v2 import SessionStateV2 as S

    assert TRANSITIONS[(S.PROCESSING, "stuck-loop")] is S.FINALIZING


def test_goal_continuation_does_not_auto_retry_a_stuck_loop():
    """Continuing a force-broken loop on the same approach repeats the loop."""
    import inspect

    from sessions.manager import SessionManager

    src = inspect.getsource(SessionManager._maybe_enqueue_goal_continuation)
    assert '"complete", "round_ceiling", "budget_exhausted"' in src
    assert "stuck_loop" not in src.split('"""')[2]
