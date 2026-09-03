"""The overflow path ignored the compactor's answer.

A provider overflow ran compaction with no state restore and `continue`d
unconditionally: a rejected summary left the session in COMPACTING while
the same oversized request was re-sent, and a turn that then succeeded on
the fallback model was finalized as COMPACTION_FAILED. The overflow now
restores PROCESSING on a declined compaction, and once the controller is
spent or provably a no-op the overflow stays in the ladder as a fatal
stream error where the fallback's larger window is the last rescue.
"""

from types import SimpleNamespace

import pytest

from core.agent import _CompactionController
from db import models as db
from sessions import state_v2 as sv2
from sessions.state import AgentSession


@pytest.fixture
def processing_session():
    sid = db.create_session(title="overflow")
    session = AgentSession(session_id=sid)
    session.emit_event = lambda e: None
    sv2._set_state(session, sv2.SessionStateV2.PROCESSING)
    return session, sid


def _payload(tokens: int):
    return SimpleNamespace(token_count=tokens, messages=[], history_budget=50_000)


async def test_declined_overflow_compaction_returns_to_processing(processing_session, monkeypatch):
    async def _declines(session_id, messages, **kwargs):
        return False

    monkeypatch.setattr("core.agent.compact_with_llm", _declines)
    session, sid = processing_session
    c = _CompactionController(session, sid)
    compacted = await c.run(
        _payload(120_000),
        transition_reason="compact-overflow",
        event_reason="api_overflow",
        restore_state_on_failure=True,
    )
    assert compacted is False
    assert sv2._current_state(session) == sv2.SessionStateV2.PROCESSING


async def test_overflow_stops_surfacing_once_the_compactor_cannot_help(processing_session, monkeypatch):
    async def _noop(session_id, messages, **kwargs):
        return True

    monkeypatch.setattr("core.agent.compact_with_llm", _noop)
    session, sid = processing_session
    c = _CompactionController(session, sid)
    assert c.can_help_with_overflow
    await c.run(_payload(120_000), transition_reason="compact-overflow", restore_state_on_failure=True)
    c.observe(119_000)  # the re-compile: nothing moved
    assert c.stalled
    assert not c.can_help_with_overflow, "a no-op compactor must hand the overflow to the ladder"
