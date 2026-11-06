"""Three gaps found by live-testing the 2026-11-05 resume-chain fixes.

1. A suspended turn left the task ungraded (session 2bf19276665e). The turn
   that called await_workers(suspend=True) never reaches post-hooks, so it
   schedules no grade; the resume turn then skipped its own grade because it
   is synthetic. A resume turn now grades the task when no grade is waiting,
   against the user's real request.
2. A queued resume suppressed the real turn's hooks (session 810bc5142b05).
   The parent blocked in await_workers while watching the same workers; the
   resume was queued, `pending_messages` was non-empty, and _run_post_hooks
   returned early (no grade, no distill). Then the redundant resume ran.
3. LLM streaming read as a stall (session 0e6c653fae70). A worker streamed a
   130 s file_write call; nothing touched the session, so await_workers called
   it idle > 120 s and the parent stopped waiting.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from core.llm.stream_ladder import stream_with_failover
from core.llm.types import StreamEvent, StreamEventType
from db import models as db
from sessions import state_v2 as sv2
from sessions.manager import _WORKER_RESUME_PREFIX, SessionManager, _is_worker_resume
from sessions.state import PendingMessage


@pytest.fixture
def mgr(monkeypatch):
    fresh = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", fresh)
    return fresh


# --- 3. streaming is activity ----------------------------------------------


class _SlowClient:
    def has_capacity(self, model=""):
        return True

    def resolve_provider(self, model=""):
        return "ollama"

    def chat_stream(self, messages, **kwargs):
        async def _gen():
            yield StreamEvent(type=StreamEventType.TOKEN, content="a")
            yield StreamEvent(type=StreamEventType.TOKEN, content="b")
            yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")

        return _gen()


async def test_every_stream_event_counts_as_activity():
    sid = db.create_session(title="stream")
    hits: list[float] = []
    await stream_with_failover(
        client=_SlowClient(),
        session_id=sid,
        emit=lambda e: None,
        on_activity=lambda: hits.append(time.time()),
        messages=[{"role": "user", "content": "hi"}],
        base_messages=[{"role": "user", "content": "hi"}],
        static_prefix_chars=0,
        tools=None,
        model="m",
        max_output_cap=0,
        goal_id=None,
        sched_created_at=0.0,
        sched_priority=0,
    )
    # Counted before the type dispatch, so tool-call deltas (which emit
    # nothing to the UI) count the same way.
    assert len(hits) == 3


def test_the_agent_loop_passes_the_session_touch():
    import inspect

    import core.agent as agent

    src = inspect.getsource(agent)
    assert src.count("on_activity=session.touch") == 2


# --- 2. a queued resume ------------------------------------------------------


def _resume_entry():
    return PendingMessage(_WORKER_RESUME_PREFIX + " — 1 total]", None, False)


def test_a_resume_entry_is_recognized():
    assert _is_worker_resume(_resume_entry())
    assert not _is_worker_resume(PendingMessage("hello", "", True, 0.0, 42))
    # A user typing the prefix has a DB row — still real work.
    assert not _is_worker_resume(PendingMessage(_WORKER_RESUME_PREFIX, "", True, 0.0, 43))


async def test_a_queued_resume_does_not_suppress_post_hooks(mgr, monkeypatch):
    sid = mgr.create_session(title="P")
    s = mgr.get(sid)
    sv2.transition(s, sv2.SessionStateV2.SCOUTING, "prompt-arrived")
    sv2.transition(s, sv2.SessionStateV2.PROCESSING, "scout-done")
    sv2.transition(s, sv2.SessionStateV2.FINALIZING, "loop-complete")
    ran: list = []

    async def _hooks(session_id, **kw):
        ran.append(session_id)

    monkeypatch.setattr("sessions.hooks.run_post_task_hooks", _hooks)

    s.pending_messages.append(_resume_entry())
    await mgr._run_post_hooks(s)
    assert ran == [sid]

    s.pending_messages.append(PendingMessage("a real message", "", True, 0.0, 99))
    await mgr._run_post_hooks(s)
    assert ran == [sid], "real queued work still supersedes the hooks"


def _parent_collected(mgr):
    parent_id = mgr.create_session(title="P")
    worker_id = mgr.create_session(title="W", session_type="worker", parent_session_id=parent_id)
    parent = mgr.get(parent_id)
    parent.worker_ids = [worker_id]
    parent._watched_worker_ids = {worker_id}
    db.add_message(parent_id, "user", "summarize these pages")
    call = {
        "id": "c1",
        "type": "function",
        "function": {"name": "get_worker_result", "arguments": json.dumps({"worker_id": worker_id})},
    }
    db.add_message(parent_id, "assistant", "", tool_calls=json.dumps([call]))
    db.add_message(parent_id, "tool", "summary", tool_call_id="c1")
    db.add_message(parent_id, "assistant", "Here is the digest.")
    return parent


async def test_a_redundant_queued_resume_is_dropped(mgr, monkeypatch):
    parent = _parent_collected(mgr)
    started: list = []

    async def _fake_run(session, message, system_prompt, **kwargs):
        started.append(message)

    monkeypatch.setattr(mgr, "_run_agent_safe", _fake_run)
    parent.pending_messages.append(_resume_entry())

    await mgr._process_pending(parent)
    await asyncio.sleep(0)

    assert started == []
    assert not parent.pending_messages
    assert parent._watched_worker_ids == set()


async def test_a_needed_queued_resume_still_runs(mgr, monkeypatch):
    parent_id = mgr.create_session(title="P")
    worker_id = mgr.create_session(title="W", session_type="worker", parent_session_id=parent_id)
    parent = mgr.get(parent_id)
    parent.worker_ids = [worker_id]
    db.add_message(parent_id, "user", "summarize these pages")
    db.add_message(parent_id, "assistant", "Started.")
    started: list = []

    async def _fake_run(session, message, system_prompt, **kwargs):
        started.append(message)

    monkeypatch.setattr(mgr, "_run_agent_safe", _fake_run)
    parent.pending_messages.append(_resume_entry())

    await mgr._process_pending(parent)
    await asyncio.sleep(0)

    assert started and started[0].startswith(_WORKER_RESUME_PREFIX)


# --- 1. a resume turn with no grade waiting --------------------------------


def _resume_turn_session(mgr, monkeypatch):
    monkeypatch.setattr("config.settings.reflect_deferred_normal", True)
    monkeypatch.setattr("config.settings.reflect_min_messages", 1)
    sid = mgr.create_session(title="P")
    s = mgr.get(sid)
    real = db.add_message(sid, "user", "tabulate the asyncio tasks")
    db.add_message(sid, "assistant", "", tool_calls="[]")
    resume = db.add_message(sid, "user", _WORKER_RESUME_PREFIX + " — 1 total]")
    db.add_message(sid, "assistant", "Here is the table.", metadata=json.dumps({"parent_user_msg_id": resume}))
    s.current_turn_user_msg_id = resume
    s._turn_id = 2
    s._synthetic_turn_ids.add(2)
    return sid, s, real


async def test_a_resume_turn_grades_the_task_when_nothing_is_pending(mgr, monkeypatch):
    from sessions import hooks

    sid, s, real = _resume_turn_session(mgr, monkeypatch)
    scheduled: list = []

    async def _sched(session_id, session, session_obj, gate_results, emit=None, user_msg_id=None):
        scheduled.append(user_msg_id)

    monkeypatch.setattr(hooks, "_schedule_deferred_reflect", _sched)
    await hooks._maybe_reflect(sid, db.get_session(sid), session_obj=s)
    assert scheduled == [real], "graded against the user's real request"


async def test_a_resume_turn_defers_to_a_pending_grade(mgr, monkeypatch):
    from sessions import hooks

    sid, s, _ = _resume_turn_session(mgr, monkeypatch)
    s._deferred_grade_pending = 1
    scheduled: list = []

    async def _sched(*a, **kw):
        scheduled.append(1)

    monkeypatch.setattr(hooks, "_schedule_deferred_reflect", _sched)
    await hooks._maybe_reflect(sid, db.get_session(sid), session_obj=s)
    assert scheduled == []


async def test_the_pending_marker_clears_when_the_grade_finishes(mgr, monkeypatch):
    from sessions import hooks

    monkeypatch.setattr("config.settings.reflect_defer_idle_s", 0)

    async def _noop(*a, **kw):
        return None

    monkeypatch.setattr(hooks, "_deferred_reflect_body", _noop)
    sid = mgr.create_session(title="P")
    s = mgr.get(sid)
    await hooks._schedule_deferred_reflect(sid, db.get_session(sid), s, [])
    assert s._deferred_grade_pending == s._deferred_reflect_seq
    for _ in range(20):
        await asyncio.sleep(0)
        if not s._deferred_grade_pending:
            break
    assert s._deferred_grade_pending == 0


# --- 4. the stretched window carries the resume turn's tool record ----------


def test_the_stretched_grade_uses_the_resume_turns_tool_record(mgr):
    """Session 4b432ce67064: a 3x spawn_worker summary beside a transcript of
    3x get_worker_result read as the user re-sending the request."""
    from sessions.hooks import _DeferredGrade, _extend_to_resume_chain

    sid = mgr.create_session(title="P")
    s = mgr.get(sid)
    db.add_message(sid, "user", "summarize these pages")
    last = db.add_message(sid, "assistant", "3 workers started.")
    snap = _DeferredGrade(
        session_id=sid,
        ticket=1,
        turn_id=2,
        turn_user_msg_id=None,
        attempt=1,
        turn_last_msg_id=last,
        tool_summary={"spawn_worker": {"calls": 3, "failures": 0, "total_latency_ms": 60}},
    )
    s._turn_id = 3
    s._synthetic_turn_ids.add(3)
    s.turn.tool_summary = {"get_worker_result": {"calls": 3, "failures": 0, "total_latency_ms": 9}}
    db.add_message(sid, "assistant", "Here is the digest.")

    out = _extend_to_resume_chain(s, snap)
    assert list(out.tool_summary) == ["get_worker_result"]
