"""One "Provider returned an empty response" ended a 61-round turn with no
final message, no grade, and no notification (field case ae952f40e3d1).

The ladder classified the empty stream as non-retryable (it matches no
gateway/transport marker), and the fallback rung was already spent from an
earlier failover — so the turn died. An empty stream is an upstream flake:
the request was well-formed and the provider sent nothing. It is now
retryable. And because errored sessions skip reflect, a stream death now
leaves a high-urgency notification instead of silence.
"""

import asyncio

from core.llm.stream_ladder import is_stream_retryable
from db import models as db


def test_empty_response_is_retryable():
    assert is_stream_retryable("Provider returned an empty response")


def test_config_errors_stay_non_retryable():
    assert not is_stream_retryable("401 Unauthorized")
    assert not is_stream_retryable("model not found: gpt-nope")


def test_stream_death_leaves_a_notification():
    from core.agent import _end_turn_on_stream_error
    from sessions.state import AgentSession

    sid = db.create_session(title="ARC run")
    session = AgentSession(session_id=sid)
    events = []
    session.emit_event = lambda e: events.append(e)

    async def _save(role, content, partial=0):
        return 1

    asyncio.get_event_loop_policy()
    asyncio.run(
        _end_turn_on_stream_error(
            session=session,
            session_id=sid,
            error="Provider returned an empty response",
            partial_content="",
            save_turn_msg=_save,
        )
    )
    notes = [n for n in db.get_notifications() if n.get("session_id") == sid]
    assert len(notes) == 1
    assert "stream error" in notes[0]["title"]
    assert "not graded" in notes[0]["body"]
    assert session.termination_reason == "error"


def test_budget_soft_land_does_not_notify():
    """Budget exhaustion has its own transcript notice and reflect still runs
    — the stream-death notification is only for genuine errors."""
    from core.agent import _end_turn_on_stream_error
    from sessions.state import AgentSession

    sid = db.create_session(title="ARC run")
    session = AgentSession(session_id=sid)
    session.emit_event = lambda e: None

    async def _save(role, content, partial=0):
        return 1

    asyncio.run(
        _end_turn_on_stream_error(
            session=session,
            session_id=sid,
            error="session exceeded the 1800s LLM time limit",
            partial_content="",
            save_turn_msg=_save,
        )
    )
    assert [n for n in db.get_notifications() if n.get("session_id") == sid] == []
    assert session.termination_reason == "budget_exhausted"
