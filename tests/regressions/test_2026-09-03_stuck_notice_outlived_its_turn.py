"""A stuck notice from one turn kept steering later turns.

Field case, session 3dc5a307d751 (2026-09-03). Stuck detection wrote
"You are repeating tool calls (bash). Do NOT retry the same operation." as a
plain role=system row. System rows are permanent history, so every later turn
recompiled that sentence into its prompt and the agent read it as standing
policy — declining to re-run a command in a turn that had nothing to do with
the loop that provoked the notice.

The notices are now stamped `metadata.ephemeral_turn = <turn's user msg id>`
and the compiler drops them from any turn but their own. They stay in the DB,
so the transcript UI still shows what the agent was told and when.
"""

from __future__ import annotations

import inspect
import json

from core.agent import _handle_stuck_signals
from core.context.compiler import compile_context
from db import models as db

NOTICE = "You are repeating tool calls"


def _texts(payload) -> str:
    return "\n".join(str(m.get("content", "")) for m in payload.messages)


async def test_the_notice_is_scoped_to_the_turn_that_provoked_it():
    sid = db.create_session(title="stuck scope")
    turn_one = db.add_message(sid, "user", "solve this")

    action, _ = await _handle_stuck_signals(
        session_id=sid,
        score=0.5,
        repeats=1,
        tool_calls=[{"name": "bash"}],
        active_tools=[],
        nudges_used=0,
        nudge_limit=3,
        turn_user_msg_id=turn_one,
    )
    assert action == "proceed"

    db.add_message(sid, "assistant", "done with that")
    turn_two = db.add_message(sid, "user", "now do the other thing")

    # Its own turn still sees it...
    assert NOTICE in _texts(compile_context(sid, turn_user_msg_id=turn_one))
    # ...the next turn does not.
    assert NOTICE not in _texts(compile_context(sid, turn_user_msg_id=turn_two))


async def test_the_row_survives_in_the_transcript():
    sid = db.create_session(title="stuck transcript")
    turn = db.add_message(sid, "user", "go")
    await _handle_stuck_signals(
        session_id=sid,
        score=0.5,
        repeats=1,
        tool_calls=[{"name": "bash"}],
        active_tools=[],
        nudges_used=0,
        nudge_limit=3,
        turn_user_msg_id=turn,
    )

    rows = [m for m in db.get_messages(sid) if m["role"] == "system"]
    assert len(rows) == 1
    assert NOTICE in rows[0]["content"]
    assert json.loads(rows[0]["metadata"])["ephemeral_turn"] == turn


async def test_the_loop_break_notices_are_stamped_too():
    """All three of the harder stuck rows — the ask_user nudge, the
    nudge-cap message and the plain 'stuck in a loop' stop — are advice about
    one round, not history."""
    sid = db.create_session(title="stuck stop")
    turn = db.add_message(sid, "user", "go")

    action, used = await _handle_stuck_signals(
        session_id=sid,
        score=0.9,
        repeats=3,
        tool_calls=[{"name": "bash"}],
        active_tools=["ask_user"],
        nudges_used=0,
        nudge_limit=1,
        turn_user_msg_id=turn,
    )
    assert action == "nudge-and-retry" and used == 1

    await _handle_stuck_signals(
        session_id=sid,
        score=0.9,
        repeats=3,
        tool_calls=[{"name": "bash"}],
        active_tools=["ask_user"],
        nudges_used=1,
        nudge_limit=1,
        turn_user_msg_id=turn,
    )
    await _handle_stuck_signals(
        session_id=sid,
        score=0.9,
        repeats=3,
        tool_calls=[{"name": "bash"}],
        active_tools=[],
        nudges_used=0,
        nudge_limit=1,
        turn_user_msg_id=turn,
    )

    rows = [m for m in db.get_messages(sid) if m["role"] == "system"]
    assert len(rows) == 3
    assert all(json.loads(r["metadata"])["ephemeral_turn"] == turn for r in rows)

    later = db.add_message(sid, "user", "next")
    assert "stuck in a loop" not in _texts(compile_context(sid, turn_user_msg_id=later))


def test_unstamped_system_rows_are_untouched():
    """Only rows carrying ephemeral_turn are scoped — every other system note
    (RLM orphan pointers, round-cap continuations, capability hints) keeps its
    permanent place in history."""
    sid = db.create_session(title="plain system row")
    db.add_message(sid, "user", "go")
    db.add_message(sid, "system", "[round cap reached — one continuation granted]")
    later = db.add_message(sid, "user", "next")

    assert "round cap reached" in _texts(compile_context(sid, turn_user_msg_id=later))


def test_the_agent_loop_passes_the_turn_id_through():
    """Guard the plumbing: the parameter exists and the caller supplies it."""
    assert "turn_user_msg_id" in inspect.signature(_handle_stuck_signals).parameters
    import core.agent as agent_mod

    src = inspect.getsource(agent_mod.run_agent)
    assert "turn_user_msg_id=_turn_user_msg_id," in src
