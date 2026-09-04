"""The worker resume ran a turn the parent had already run — and stole its grade.

Field case, session 3dc5a307d751 (2026-09-03). The parent spawned workers,
called get_worker_result for every one of them inside its own turn, wrote its
answer and went idle. The Gap-1 resume then injected "[Watched workers have
completed …]" anyway. The agent re-read the same results and re-answered, and
because that synthetic turn advanced the turn counter and scheduled its own
deferred grade, the real turn's pending grade was dropped as "superseded".

Three guards: the Gap-1 resume is skipped when every listed worker's result
was already collected; a worker-resume turn is tagged synthetic and never
schedules a deferred grade of its own; and an intervening synthetic turn is
not supersession for a grade already in flight.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from db import models as db
from sessions import state_v2 as sv2
from sessions.hooks import _deferred_grade_superseded, _DeferredGrade
from sessions.manager import _WORKER_RESUME_PREFIX, SessionManager


@pytest.fixture
def mgr(monkeypatch):
    fresh = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", fresh)
    return fresh


def _parent_with_worker(mgr, *, collected: bool):
    parent_id = mgr.create_session(title="Orchestrator")
    worker_id = mgr.create_session(title="W", session_type="worker", parent_session_id=parent_id)
    parent = mgr.get(parent_id)
    parent.worker_ids = [worker_id]
    parent._watched_worker_ids = {worker_id}

    db.add_message(parent_id, "user", "delegate the audit and summarize")
    if collected:
        db.add_message(
            parent_id,
            "assistant",
            "",
            tool_calls=json.dumps(
                [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "get_worker_result", "arguments": json.dumps({"worker_id": worker_id})},
                    }
                ]
            ),
        )
        db.add_message(parent_id, "tool", "worker summary text", tool_call_id="c1")
    db.add_message(parent_id, "assistant", "Here is the synthesis.")
    return parent, worker_id


def _capture_turns(mgr, monkeypatch) -> list:
    started: list = []

    async def _fake_run(session, message, system_prompt, **kwargs):
        started.append(message)

    monkeypatch.setattr(mgr, "_run_agent_safe", _fake_run)
    return started


async def test_resume_is_skipped_when_the_results_are_already_in_hand(mgr, monkeypatch):
    parent, worker_id = _parent_with_worker(mgr, collected=True)
    started = _capture_turns(mgr, monkeypatch)
    assert sv2._current_state(parent) is sv2.SessionStateV2.IDLE_READY

    await mgr._resume_from_workers(parent)
    await asyncio.sleep(0)

    assert started == [], "no redundant synthesis turn"
    assert not parent.pending_messages
    assert parent._watched_worker_ids == set()


async def test_resume_still_fires_when_a_result_was_never_read(mgr, monkeypatch):
    parent, worker_id = _parent_with_worker(mgr, collected=False)
    started = _capture_turns(mgr, monkeypatch)

    await mgr._resume_from_workers(parent)
    await asyncio.sleep(0)

    assert len(started) == 1
    assert started[0].startswith(_WORKER_RESUME_PREFIX)


async def test_a_parked_parent_is_never_skipped(mgr, monkeypatch):
    """AWAITING_WORKERS means the parent suspended precisely because it had
    not collected them — the skip must not reach that path."""
    parent, worker_id = _parent_with_worker(mgr, collected=True)
    sv2.transition(parent, sv2.SessionStateV2.SCOUTING, "prompt-arrived")
    sv2.transition(parent, sv2.SessionStateV2.PROCESSING, "scout-complete")
    sv2.transition(parent, sv2.SessionStateV2.AWAITING_WORKERS, "workers-suspend")

    started = _capture_turns(mgr, monkeypatch)
    await mgr._resume_from_workers(parent)
    await asyncio.sleep(0)

    assert started and started[0].startswith(_WORKER_RESUME_PREFIX)


def test_the_collection_check_is_scoped_to_the_current_turn(mgr):
    """A get_worker_result from an EARLIER turn must not count — otherwise a
    second batch of workers would never resume."""
    parent, worker_id = _parent_with_worker(mgr, collected=True)
    assert mgr._worker_results_already_collected(parent) is True

    db.add_message(parent.session_id, "user", "now spawn a second batch")
    assert mgr._worker_results_already_collected(parent) is False


# --- the grade the synthetic turn used to steal --------------------------------


def _snapshot(session_obj, turn_id: int) -> _DeferredGrade:
    return _DeferredGrade(
        session_id=session_obj.session_id,
        ticket=getattr(session_obj, "_deferred_reflect_seq", 0),
        turn_id=turn_id,
        turn_user_msg_id=None,
        attempt=1,
    )


def test_a_synthetic_resume_turn_does_not_supersede_the_real_grade(mgr):
    sid = mgr.create_session(title="Graded")
    session_obj = mgr.get(sid)
    session_obj._turn_id = 4
    snap = _snapshot(session_obj, 4)

    # The resume turn runs: the counter advances and the id is tagged.
    session_obj._turn_id = 5
    session_obj._synthetic_turn_ids.add(5)

    assert _deferred_grade_superseded(session_obj, snap) is None


def test_a_real_follow_up_turn_still_supersedes(mgr):
    sid = mgr.create_session(title="Graded")
    session_obj = mgr.get(sid)
    session_obj._turn_id = 4
    snap = _snapshot(session_obj, 4)

    session_obj._turn_id = 5  # a user message, not a resume

    assert _deferred_grade_superseded(session_obj, snap) == "turn counter advanced"


def test_a_real_turn_after_a_synthetic_one_supersedes(mgr):
    sid = mgr.create_session(title="Graded")
    session_obj = mgr.get(sid)
    session_obj._turn_id = 4
    snap = _snapshot(session_obj, 4)

    session_obj._turn_id = 5
    session_obj._synthetic_turn_ids.add(5)
    session_obj._turn_id = 6  # the user moved on

    assert _deferred_grade_superseded(session_obj, snap) == "turn counter advanced"


async def test_a_synthetic_turn_schedules_no_grade_of_its_own(monkeypatch):
    """Scheduling one would bump _deferred_reflect_seq and cancel the pending
    grade of the turn that did the work."""
    from sessions.hooks import _maybe_reflect
    from sessions.state import AgentSession

    monkeypatch.setattr("config.settings.reflect_enabled", True)
    monkeypatch.setattr("config.settings.reflect_min_messages", 2)
    monkeypatch.setattr("config.settings.reflect_deferred_normal", True)

    sid = db.create_session(title="Resumed")
    uid = db.add_message(sid, "user", f"{_WORKER_RESUME_PREFIX} — 1 total]")
    meta = json.dumps({"parent_user_msg_id": uid})
    db.add_message(sid, "assistant", "Synthesized the worker output", metadata=meta)
    db.add_message(sid, "tool", "worker summary", metadata=meta)

    session_obj = AgentSession(session_id=sid)
    session_obj.current_turn_user_msg_id = uid
    session_obj._turn_id = 7
    session_obj._synthetic_turn_ids.add(7)
    events: list = []

    await _maybe_reflect(sid, db.get_session(sid), emit=events.append, session_obj=session_obj)

    assert session_obj._deferred_reflect_seq == 0
    assert not any(e.get("type") == "reflect.deferred_scheduled" for e in events)


async def test_an_ordinary_turn_still_schedules_its_grade(monkeypatch):
    from sessions.hooks import _maybe_reflect
    from sessions.state import AgentSession

    monkeypatch.setattr("config.settings.reflect_enabled", True)
    monkeypatch.setattr("config.settings.reflect_min_messages", 2)
    monkeypatch.setattr("config.settings.reflect_deferred_normal", True)

    scheduled: list = []

    async def _fake_task(session_obj, snap):
        scheduled.append(snap)

    monkeypatch.setattr("sessions.hooks._deferred_reflect_task", _fake_task)

    sid = db.create_session(title="Ordinary")
    uid = db.add_message(sid, "user", "fix the login bug")
    meta = json.dumps({"parent_user_msg_id": uid})
    db.add_message(sid, "assistant", "fixed it", metadata=meta)
    db.add_message(sid, "tool", "file written", metadata=meta)

    session_obj = AgentSession(session_id=sid)
    session_obj.current_turn_user_msg_id = uid
    session_obj._turn_id = 3

    await _maybe_reflect(sid, db.get_session(sid), session_obj=session_obj)
    await asyncio.sleep(0)

    assert session_obj._deferred_reflect_seq == 1
    assert len(scheduled) == 1
