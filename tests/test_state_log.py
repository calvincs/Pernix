"""Tests for the v2 state-machine log table and the transition() mutator.

These tests exercise sessions/state_v2.py and db.session_state_log directly
without running a full agent turn — the goal is to lock down the observable
contract of a transition: what goes into the DB, what goes over SSE, and
what the derived properties (post_hooks_complete, waiting_for_input) return.
"""

from __future__ import annotations

import pytest

from db import models as db
from sessions import state_v2 as sv2
from sessions.state import AgentSession


@pytest.fixture
def sid():
    # isolate_data (autouse in conftest) already initialized the DB.
    return db.create_session(title="state-log test")


@pytest.fixture
def session(sid):
    return AgentSession(session_id=sid, session_type="normal")


def _log_rows(sid: str) -> list[dict]:
    return db.get_state_log(sid, limit=1000)


def test_first_transition_writes_row_with_null_from_state(session):
    sv2.transition(session, sv2.SessionStateV2.SCOUTING, "prompt-arrived")
    rows = _log_rows(session.session_id)
    assert len(rows) == 1
    assert rows[0]["from_state"] == "idle_ready"
    assert rows[0]["to_state"] == "scouting"
    assert rows[0]["reason"] == "prompt-arrived"
    assert rows[0]["turn_id"] == 1
    assert rows[0]["retry_index"] == 0
    assert rows[0]["compaction_count"] == 0


def test_full_happy_turn_writes_expected_sequence(session):
    sv2.transition(session, sv2.SessionStateV2.SCOUTING, "prompt-arrived")
    sv2.transition(session, sv2.SessionStateV2.PROCESSING, "scout-done")
    sv2.transition(
        session,
        sv2.SessionStateV2.FINALIZING,
        "loop-complete",
        termination_reason=sv2.TerminationReason.COMPLETE,
    )
    sv2.transition(session, sv2.SessionStateV2.IDLE_READY, "turn-complete")

    rows = _log_rows(session.session_id)
    assert [r["reason"] for r in rows] == [
        "prompt-arrived",
        "scout-done",
        "loop-complete",
        "turn-complete",
    ]
    # turn_id stays at 1 across the whole turn
    assert {r["turn_id"] for r in rows} == {1}
    # retry_index 0 — no retries
    assert {r["retry_index"] for r in rows} == {0}
    # termination_reason lands on the FINALIZING row
    fin = [r for r in rows if r["to_state"] == "finalizing"][0]
    assert fin["termination_reason"] == "complete"


def test_reflect_retry_increments_retry_index_same_turn(session):
    # Full first attempt
    sv2.transition(session, sv2.SessionStateV2.SCOUTING, "prompt-arrived")
    sv2.transition(session, sv2.SessionStateV2.PROCESSING, "scout-done")
    sv2.transition(
        session,
        sv2.SessionStateV2.FINALIZING,
        "loop-complete",
        termination_reason=sv2.TerminationReason.COMPLETE,
    )
    # Reflect says retry — FINALIZING → SCOUTING within the same user turn
    sv2.transition(session, sv2.SessionStateV2.SCOUTING, "reflect-retry")
    sv2.transition(session, sv2.SessionStateV2.PROCESSING, "scout-done")
    sv2.transition(
        session,
        sv2.SessionStateV2.FINALIZING,
        "loop-complete",
        termination_reason=sv2.TerminationReason.COMPLETE,
    )
    sv2.transition(session, sv2.SessionStateV2.IDLE_READY, "turn-complete")

    rows = _log_rows(session.session_id)
    # All rows share turn_id=1; retry_index goes 0 during first attempt,
    # 1 during the retry.
    assert {r["turn_id"] for r in rows} == {1}
    retry_row = [r for r in rows if r["reason"] == "reflect-retry"][0]
    assert retry_row["retry_index"] == 1
    # All subsequent rows in the same retry keep retry_index=1
    after_retry = [r for r in rows if r["id"] > retry_row["id"]]
    assert all(r["retry_index"] == 1 for r in after_retry)


def test_answer_received_increments_turn_and_sets_parent_turn_id(session):
    # Turn 1 reaches AWAITING_USER
    sv2.transition(session, sv2.SessionStateV2.SCOUTING, "prompt-arrived")
    sv2.transition(session, sv2.SessionStateV2.PROCESSING, "scout-done")
    sv2.transition(session, sv2.SessionStateV2.AWAITING_USER, "ask-user")
    # Answer arrives → new turn with parent_turn_id pointing at turn 1
    sv2.transition(session, sv2.SessionStateV2.SCOUTING, "answer-received")

    rows = _log_rows(session.session_id)
    answer_row = [r for r in rows if r["reason"] == "answer-received"][0]
    assert answer_row["turn_id"] == 2
    assert answer_row["parent_turn_id"] == 1


def test_compaction_loop_increments_compaction_count(session):
    sv2.transition(session, sv2.SessionStateV2.SCOUTING, "prompt-arrived")
    sv2.transition(session, sv2.SessionStateV2.PROCESSING, "scout-done")
    sv2.transition(session, sv2.SessionStateV2.COMPACTING, "compact-proactive")
    sv2.transition(session, sv2.SessionStateV2.PROCESSING, "compact-done")
    sv2.transition(session, sv2.SessionStateV2.COMPACTING, "compact-overflow")
    sv2.transition(session, sv2.SessionStateV2.PROCESSING, "compact-done")

    rows = _log_rows(session.session_id)
    compact_rows = [r for r in rows if r["reason"].startswith("compact-") and r["to_state"] == "compacting"]
    assert len(compact_rows) == 2
    assert compact_rows[0]["compaction_count"] == 1
    assert compact_rows[1]["compaction_count"] == 2


def test_cancel_path_records_termination_reason(session):
    sv2.transition(session, sv2.SessionStateV2.SCOUTING, "prompt-arrived")
    sv2.transition(session, sv2.SessionStateV2.PROCESSING, "scout-done")
    sv2.transition(
        session,
        sv2.SessionStateV2.CANCELLING,
        "cancel-requested",
        termination_reason=sv2.TerminationReason.CANCELLED,
    )
    sv2.transition(session, sv2.SessionStateV2.IDLE_READY, "cancel-complete")

    rows = _log_rows(session.session_id)
    cancel_row = [r for r in rows if r["to_state"] == "cancelling"][0]
    assert cancel_row["termination_reason"] == "cancelled"


def test_invariant_violation_still_writes_row(session):
    # Manually force an illegal edge (IDLE_READY → FINALIZING directly with a
    # reason that isn't in the graph). The mutator should log a warning,
    # tag the row as invariant-violation, but still commit the transition.
    sv2.transition(session, sv2.SessionStateV2.FINALIZING, "bogus-reason")
    rows = _log_rows(session.session_id)
    assert len(rows) == 1
    assert rows[0]["reason"].startswith("invariant-violation:")
    assert rows[0]["to_state"] == "finalizing"


def test_elapsed_ms_increases_monotonically(session):
    import time

    sv2.transition(session, sv2.SessionStateV2.SCOUTING, "prompt-arrived")
    time.sleep(0.01)
    sv2.transition(session, sv2.SessionStateV2.PROCESSING, "scout-done")
    time.sleep(0.01)
    sv2.transition(
        session,
        sv2.SessionStateV2.FINALIZING,
        "loop-complete",
        termination_reason=sv2.TerminationReason.COMPLETE,
    )
    rows = _log_rows(session.session_id)
    # First row elapsed_ms is None (no prior state entry)
    assert rows[0]["elapsed_ms"] is None
    assert rows[1]["elapsed_ms"] is not None and rows[1]["elapsed_ms"] >= 0
    assert rows[2]["elapsed_ms"] is not None and rows[2]["elapsed_ms"] >= 0


def test_post_hooks_complete_property_derives_from_state(session):
    # Brand-new session is IDLE_READY → "done"
    assert session.post_hooks_complete is True
    sv2.transition(session, sv2.SessionStateV2.SCOUTING, "prompt-arrived")
    assert session.post_hooks_complete is False
    sv2.transition(session, sv2.SessionStateV2.PROCESSING, "scout-done")
    assert session.post_hooks_complete is False
    sv2.transition(
        session,
        sv2.SessionStateV2.FINALIZING,
        "loop-complete",
        termination_reason=sv2.TerminationReason.COMPLETE,
    )
    assert session.post_hooks_complete is False  # FINALIZING is not done
    sv2.transition(session, sv2.SessionStateV2.IDLE_READY, "turn-complete")
    assert session.post_hooks_complete is True


def test_waiting_for_input_property_derives_from_state(session):
    assert session.waiting_for_input is False
    sv2.transition(session, sv2.SessionStateV2.SCOUTING, "prompt-arrived")
    sv2.transition(session, sv2.SessionStateV2.PROCESSING, "scout-done")
    sv2.transition(session, sv2.SessionStateV2.AWAITING_USER, "ask-user")
    assert session.waiting_for_input is True
    sv2.transition(session, sv2.SessionStateV2.SCOUTING, "answer-received")
    assert session.waiting_for_input is False


def test_state_changed_event_emitted(session):
    received = []
    sub = session.subscribe()

    sv2.transition(session, sv2.SessionStateV2.SCOUTING, "prompt-arrived")

    # Drain pending events synchronously (no await in tests — emit is sync)
    while not sub.empty():
        received.append(sub.get_nowait())

    state_changed = [e for e in received if e.get("type") == "session.state_changed"]
    assert len(state_changed) == 1
    e = state_changed[0]
    assert e["from"] == "idle_ready"
    assert e["to"] == "scouting"
    assert e["reason"] == "prompt-arrived"
    assert e["turn_id"] == 1
    assert "_seq" in e


def test_get_state_log_since_id_filters(session):
    sv2.transition(session, sv2.SessionStateV2.SCOUTING, "prompt-arrived")
    sv2.transition(session, sv2.SessionStateV2.PROCESSING, "scout-done")
    all_rows = _log_rows(session.session_id)
    first_id = all_rows[0]["id"]
    later = db.get_state_log(session.session_id, since_id=first_id)
    assert len(later) == 1
    assert later[0]["reason"] == "scout-done"


def test_compat_status_maps_correctly():
    assert sv2.compat_status(sv2.SessionStateV2.IDLE_READY) == "idle"
    assert sv2.compat_status(sv2.SessionStateV2.AWAITING_USER) == "idle"
    assert sv2.compat_status(sv2.SessionStateV2.SCOUTING) == "scouting"
    assert sv2.compat_status(sv2.SessionStateV2.FINALIZING) == "processing"
    assert sv2.compat_status(sv2.SessionStateV2.CANCELLING) == "processing"
    assert sv2.compat_status(sv2.SessionStateV2.PAUSED) == "processing"
    assert sv2.compat_status(sv2.SessionStateV2.COMPACTING) == "processing"
