"""Three harness misfires from one session (c8152c896a11, 2026-11-05).

1. Grading a half-done turn. The turn ended with a status line while workers
   ran; a worker-resume turn then did the rest. At the 300s quiet mark the
   pending grade judged the partial turn "retry" — a false bell notification —
   and the resume turn that wrote the real answer was never graded (resume
   turns are skipped so they cannot cancel this grade). The grade now waits
   for the resume chain to settle and stretches its window over it.
2. A false "stalled" worker. A 6.5-minute rlm_process never touched the
   worker session, so await_workers reported it idle > 120s and gave up.
3. "Finished with no output". check_workers printed 8-char ids and every
   lookup was exact, so the short id matched nothing.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from db import models as db
from sessions import state_v2 as sv2
from sessions.hooks import (
    _await_deferred_grade,
    _DeferredGrade,
    _extend_to_resume_chain,
    _resume_chain_live,
)
from sessions.manager import SessionManager


@pytest.fixture
def mgr(monkeypatch):
    fresh = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", fresh)
    return fresh


def _snap(session_obj, turn_id: int, last_id: int | None = None) -> _DeferredGrade:
    return _DeferredGrade(
        session_id=session_obj.session_id,
        ticket=getattr(session_obj, "_deferred_reflect_seq", 0),
        turn_id=turn_id,
        turn_user_msg_id=None,
        attempt=1,
        turn_last_msg_id=last_id,
    )


# --- 1. resume-chain grading ---------------------------------------------


def test_an_idle_session_with_no_workers_is_not_a_live_chain(mgr):
    s = mgr.get(mgr.create_session(title="P"))
    assert _resume_chain_live(s) is False


def test_a_running_worker_is_a_live_chain(mgr):
    parent_id = mgr.create_session(title="P")
    worker_id = mgr.create_session(title="W", session_type="worker", parent_session_id=parent_id)
    parent = mgr.get(parent_id)
    parent.worker_ids = [worker_id]
    sv2.transition(mgr.get(worker_id), sv2.SessionStateV2.SCOUTING, "prompt-arrived")
    assert _resume_chain_live(parent) is True


def test_a_finished_worker_is_not_a_live_chain(mgr):
    parent_id = mgr.create_session(title="P")
    worker_id = mgr.create_session(title="W", session_type="worker", parent_session_id=parent_id)
    parent = mgr.get(parent_id)
    parent.worker_ids = [worker_id]
    assert _resume_chain_live(parent) is False


def test_a_resume_turn_in_flight_is_a_live_chain(mgr):
    s = mgr.get(mgr.create_session(title="P"))
    sv2.transition(s, sv2.SessionStateV2.SCOUTING, "prompt-arrived")
    s._synthetic_turn_ids.add(s._turn_id)
    assert _resume_chain_live(s) is True


def test_a_real_turn_in_flight_is_not_a_resume_chain(mgr):
    s = mgr.get(mgr.create_session(title="P"))
    sv2.transition(s, sv2.SessionStateV2.SCOUTING, "prompt-arrived")
    assert _resume_chain_live(s) is False


async def test_the_grade_waits_out_a_live_chain(mgr, monkeypatch):
    monkeypatch.setattr("config.settings.reflect_next_turn_grading", True)
    monkeypatch.setattr("config.settings.reflect_defer_idle_s", 0)
    monkeypatch.setattr("sessions.hooks._RESUME_CHAIN_POLL_S", 0.01)
    checks = iter([True, True, True, False])
    seen: list = []

    def _live(_s):
        v = next(checks)
        seen.append(v)
        return v

    monkeypatch.setattr("sessions.hooks._resume_chain_live", _live)
    s = mgr.get(mgr.create_session(title="P"))
    await _await_deferred_grade(s, _snap(s, s._turn_id))
    assert seen == [True, True, True, False]


async def test_the_chain_wait_is_bounded(mgr, monkeypatch):
    monkeypatch.setattr("config.settings.reflect_next_turn_grading", True)
    monkeypatch.setattr("config.settings.reflect_defer_idle_s", 0)
    monkeypatch.setattr("sessions.hooks._RESUME_CHAIN_POLL_S", 0.01)
    monkeypatch.setattr("sessions.hooks._RESUME_CHAIN_MAX_WAIT_S", 0.05)
    monkeypatch.setattr("sessions.hooks._resume_chain_live", lambda _s: True)
    s = mgr.get(mgr.create_session(title="P"))
    await asyncio.wait_for(_await_deferred_grade(s, _snap(s, s._turn_id)), timeout=2)


async def test_a_real_user_turn_ends_the_chain_wait(mgr, monkeypatch):
    monkeypatch.setattr("config.settings.reflect_next_turn_grading", True)
    monkeypatch.setattr("config.settings.reflect_defer_idle_s", 0)
    monkeypatch.setattr("sessions.hooks._resume_chain_live", lambda _s: True)
    s = mgr.get(mgr.create_session(title="P"))
    snap = _snap(s, s._turn_id)
    s._turn_id += 1  # the user replied: not a synthetic turn
    await asyncio.wait_for(_await_deferred_grade(s, snap), timeout=2)


def test_the_window_stretches_over_resume_turns(mgr):
    sid = mgr.create_session(title="P")
    s = mgr.get(sid)
    db.add_message(sid, "user", "summarize these with timestamp links")
    last = db.add_message(sid, "assistant", "Nine done, tracking the rest.")
    snap = _snap(s, 2, last)
    s._turn_id = 3
    s._synthetic_turn_ids.add(3)
    final = db.add_message(sid, "assistant", "Done. Here is the digest.")

    assert _extend_to_resume_chain(s, snap).turn_last_msg_id == final


def test_the_window_stays_put_when_the_user_moved_on(mgr):
    sid = mgr.create_session(title="P")
    s = mgr.get(sid)
    last = db.add_message(sid, "assistant", "first answer")
    snap = _snap(s, 2, last)
    s._turn_id = 3  # a real turn
    db.add_message(sid, "user", "something else")

    assert _extend_to_resume_chain(s, snap).turn_last_msg_id == last


def test_the_window_stays_put_with_no_later_turn(mgr):
    sid = mgr.create_session(title="P")
    s = mgr.get(sid)
    last = db.add_message(sid, "assistant", "answer")
    snap = _snap(s, s._turn_id, last)
    assert _extend_to_resume_chain(s, snap) is snap


# --- 2. RLM progress is worker activity -----------------------------------


def test_rlm_progress_touches_the_running_session(mgr):
    from core.extensions.rlm import _make_progress_fn

    s = mgr.get(mgr.create_session(title="W"))
    s.last_activity_time = time.time() - 400
    progress = _make_progress_fn("run-x", None, s.session_id)

    progress({"type": "heartbeat", "iteration": 1, "subcalls": 0})

    assert s.idle_seconds < 5


# --- 3. worker ids --------------------------------------------------------


def _parent_and_worker(mgr):
    parent_id = mgr.create_session(title="P")
    worker_id = mgr.create_session(title="W", session_type="worker", parent_session_id=parent_id)
    mgr.get(parent_id).worker_ids = [worker_id]
    return parent_id, worker_id


def test_a_unique_prefix_resolves_to_the_full_id(mgr):
    from core.extensions.orchestration import _resolve_worker_id

    parent_id, worker_id = _parent_and_worker(mgr)
    assert _resolve_worker_id(worker_id[:8], {"session_id": parent_id}) == (worker_id, "")


def test_an_exact_id_passes_through(mgr):
    from core.extensions.orchestration import _resolve_worker_id

    parent_id, worker_id = _parent_and_worker(mgr)
    assert _resolve_worker_id(worker_id, {"session_id": parent_id}) == (worker_id, "")
    # Any session id is exact — the generic /resume route depends on it.
    assert _resolve_worker_id(parent_id, None) == (parent_id, "")


def test_an_unknown_id_is_an_error_not_no_output(mgr):
    from core.extensions.orchestration import _resolve_worker_id, get_worker_result

    parent_id, _ = _parent_and_worker(mgr)
    _, err = _resolve_worker_id("zzzzzzzz", {"session_id": parent_id})
    assert err.startswith("Error: no worker")
    out = get_worker_result("zzzzzzzz", _context={"session_id": parent_id})
    assert "no output" not in out and out.startswith("Error:")


def test_get_worker_result_accepts_the_short_id(mgr):
    from core.extensions.orchestration import get_worker_result

    parent_id, worker_id = _parent_and_worker(mgr)
    db.add_message(worker_id, "assistant", "Wrote yt_summaries/x_summary.md")
    out = get_worker_result(worker_id[:8], _context={"session_id": parent_id})
    assert "Wrote yt_summaries/x_summary.md" in out


def test_check_workers_prints_full_ids(mgr):
    from core.extensions.orchestration import check_workers

    parent_id, worker_id = _parent_and_worker(mgr)
    assert f"- {worker_id} " in check_workers(_context={"session_id": parent_id})
