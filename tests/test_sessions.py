"""Tests for session state machine and manager.

The v2 state machine (sessions/state_v2.py) is the authoritative lifecycle
model. Tests here exercise the happy path (IDLE_READY → SCOUTING → PROCESSING
→ FINALIZING → IDLE_READY), the no-op loop-back paths, and assert that the
legacy-enum mirror stays coherent with v2 state. For the full edge catalog,
see tests/test_state_log.py."""

from sessions import state_v2 as sv2
from sessions.state import AgentSession, SessionState


def test_v2_happy_turn_transitions():
    """A normal turn progresses IDLE_READY → SCOUTING → PROCESSING →
    FINALIZING → IDLE_READY. Verifies both the v2 state and the legacy
    mirror (which existing consumers still read) stay coherent."""
    s = AgentSession(session_id="test")
    assert sv2._current_state(s) is sv2.SessionStateV2.IDLE_READY
    assert s.state == SessionState.IDLE

    sv2.transition(s, sv2.SessionStateV2.SCOUTING, "prompt-arrived")
    assert sv2._current_state(s) is sv2.SessionStateV2.SCOUTING
    assert s.state == SessionState.SCOUTING

    sv2.transition(s, sv2.SessionStateV2.PROCESSING, "scout-done")
    assert sv2._current_state(s) is sv2.SessionStateV2.PROCESSING
    assert s.state == SessionState.PROCESSING

    sv2.transition(
        s,
        sv2.SessionStateV2.FINALIZING,
        "loop-complete",
        termination_reason=sv2.TerminationReason.COMPLETE,
    )
    # FINALIZING mirrors as legacy IDLE (post-hooks run while legacy state
    # is IDLE; Stage 2 of the migration retired that convention).
    assert sv2._current_state(s) is sv2.SessionStateV2.FINALIZING
    assert s.state == SessionState.IDLE

    sv2.transition(s, sv2.SessionStateV2.IDLE_READY, "turn-complete")
    assert sv2._current_state(s) is sv2.SessionStateV2.IDLE_READY
    assert s.state == SessionState.IDLE


def test_v2_invariant_violation_still_writes_and_mutates():
    """The mutator is forgiving by design: illegal edges log a warning
    (the row is tagged 'invariant-violation:<reason>') and the transition
    still completes. The point is forensics, not crash-on-bug — partial
    state after a crash in production is worse than a stern diagnostic."""
    s = AgentSession(session_id="test")
    # Edge NOT in the graph: IDLE_READY → PROCESSING directly.
    sv2.transition(s, sv2.SessionStateV2.PROCESSING, "scout-done")
    assert sv2._current_state(s) is sv2.SessionStateV2.PROCESSING


def test_v2_error_path_collapses_to_finalizing():
    """Stage 1 of the migration collapsed legacy ERROR into FINALIZING with
    termination_reason=error. Ensures the scout-error path writes the
    right terminal classifier for post-hooks and worker auto-stamping."""
    s = AgentSession(session_id="test")
    sv2.transition(s, sv2.SessionStateV2.SCOUTING, "prompt-arrived")
    sv2.transition(
        s,
        sv2.SessionStateV2.FINALIZING,
        "scout-error",
        termination_reason=sv2.TerminationReason.SCOUT_ERROR,
    )
    assert sv2._current_state(s) is sv2.SessionStateV2.FINALIZING
    assert s.termination_reason == "scout_error"


def test_event_system():
    s = AgentSession(session_id="test")
    q = s.subscribe()
    s.emit_event({"type": "test", "data": "hello"})
    assert not q.empty()
    event = q.get_nowait()
    assert event["type"] == "test"
    assert event["_seq"] == 1
    assert "timestamp" in event
    assert event["session_id"] == "test"


def test_event_unsubscribe():
    s = AgentSession(session_id="test")
    q = s.subscribe()
    assert len(s.subscribers) == 1
    s.unsubscribe(q)
    assert len(s.subscribers) == 0


def test_background_refs():
    s = AgentSession(session_id="test")
    assert not s.has_background_tasks
    s.add_background_ref()
    s.add_background_ref()
    assert s.has_background_tasks
    s.remove_background_ref()
    assert s.has_background_tasks
    s.remove_background_ref()
    assert not s.has_background_tasks
    s.remove_background_ref()  # should not go negative
    assert not s.has_background_tasks


def test_idle_seconds():
    import time

    s = AgentSession(session_id="test")
    time.sleep(0.1)
    assert s.idle_seconds >= 0.1
    s.touch()
    assert s.idle_seconds < 0.1
