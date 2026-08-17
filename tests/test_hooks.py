"""Tests for sessions/hooks.py: strip_thinking, auto_title, distill, reflect, evaluate."""

import asyncio
import json

import pytest

from core.llm.types import ChatResponse, TokenUsage
from sessions.hooks import (
    _auto_title,
    _cleanup_stale_questions,
    _maybe_distill,
    _maybe_reflect,
    _strip_thinking,
    run_post_task_hooks,
)

# ---------------------------------------------------------------------------
# _strip_thinking
# ---------------------------------------------------------------------------


def test_strip_thinking_no_tags():
    text = "This is a normal response."
    assert _strip_thinking(text) == text


def test_strip_thinking_removes_think_block():
    text = "<think>I need to figure this out...</think>The actual answer."
    result = _strip_thinking(text)
    assert "<think>" not in result
    assert "The actual answer." in result


def test_strip_thinking_multiline():
    text = "<think>\nMultiple lines\nof reasoning\n</think>Final response."
    result = _strip_thinking(text)
    assert "<think>" not in result
    assert "Final response." in result


def test_strip_thinking_title_prefix():
    text = "Some preamble\nTITLE: Clean title\nSUBTITLE: sub"
    result = _strip_thinking(text)
    assert result.startswith("TITLE:")
    assert "preamble" not in result


# ---------------------------------------------------------------------------
# _cleanup_stale_questions
# ---------------------------------------------------------------------------


async def test_cleanup_stale_questions_no_questions():
    from db import models as db

    sid = db.create_session(title="Test")
    db.add_message(sid, "user", "hello")
    # No questions → should be a no-op
    await _cleanup_stale_questions(sid)


async def test_cleanup_stale_questions_cleans_old():
    import time

    from db import models as db

    sid = db.create_session(title="Cleanup")

    # Add question, then add user message after it
    qid = db.add_question(sid, "Press Y to continue?")
    # Wait briefly then add user message
    db.add_message(sid, "user", "Let's continue", latency_ms=None)
    db.add_message(sid, "assistant", "Ok!")

    await _cleanup_stale_questions(sid)
    # Question should be cleaned since a user message came after it
    remaining = db.get_questions(sid)
    assert len(remaining) == 0


# ---------------------------------------------------------------------------
# _auto_title
# ---------------------------------------------------------------------------


async def test_auto_title_no_messages():
    from db import models as db

    sid = db.create_session(title="New session")
    # No messages → should not change title
    await _auto_title(sid)
    s = db.get_session(sid)
    assert s["title"] == "New session"


async def test_auto_title_sets_title(mock_llm_client):
    from db import models as db

    sid = db.create_session(title="New session")
    db.add_message(sid, "user", "Help me fix the login bug")
    db.add_message(sid, "assistant", "I'll look at the authentication code.")

    mock_llm_client.responses = [
        ChatResponse(
            content="TITLE: Fix Login Bug\nSUBTITLE: auth debugging",
            tool_calls=None,
            usage=TokenUsage(10, 5, 15),
            model="test",
            provider="fake",
            finish_reason="stop",
        )
    ]

    events = []
    await _auto_title(sid, emit=events.append)
    s = db.get_session(sid)
    assert s["title"] == "Fix Login Bug"
    assert s.get("subtitle") == "auth debugging"
    assert any(e.get("type") == "session.title" for e in events)


async def test_auto_title_handles_llm_failure(mock_llm_client):
    from db import models as db

    sid = db.create_session(title="New session")
    db.add_message(sid, "user", "Hello")

    async def failing_chat(*args, **kwargs):
        raise ConnectionError("LLM down")

    mock_llm_client.chat = failing_chat

    # Should not raise — just log a warning
    await _auto_title(sid)


# ---------------------------------------------------------------------------
# _maybe_distill
# ---------------------------------------------------------------------------


async def test_maybe_distill_skips_few_messages(monkeypatch):
    from db import models as db

    monkeypatch.setattr("config.settings.memory_recall", True)
    sid = db.create_session()
    db.add_message(sid, "user", "hi")
    db.add_message(sid, "assistant", "hello")
    session = db.get_session(sid)
    # Only 2 messages — below threshold (need 4)
    # Should be a no-op (no distill call)
    await _maybe_distill(sid, session)


async def test_maybe_distill_skips_if_disabled(monkeypatch):
    from db import models as db

    monkeypatch.setattr("config.settings.memory_recall", False)
    sid = db.create_session()
    for _ in range(10):
        db.add_message(sid, "user", "message " * 50)
        db.add_message(sid, "assistant", "response " * 50)
    session = db.get_session(sid)
    # Even with enough messages, disabled → no distillation
    await _maybe_distill(sid, session)


# ---------------------------------------------------------------------------
# _maybe_reflect
# ---------------------------------------------------------------------------


async def test_maybe_reflect_runs_on_workers(mock_llm_client, monkeypatch):
    """Workers use reflect_max_retries_worker as cap (not reflect_max_retries)."""
    from db import models as db
    from sessions.state import AgentSession

    monkeypatch.setattr("config.settings.reflect_enabled", True)
    monkeypatch.setattr("config.settings.reflect_min_messages", 2)
    monkeypatch.setattr("config.settings.reflect_max_retries", 5)
    monkeypatch.setattr("config.settings.reflect_max_retries_worker", 2)

    parent_sid = db.create_session(title="Parent")
    worker_sid = db.create_session(
        title="Worker",
        session_type="worker",
        parent_session_id=parent_sid,
    )
    db.add_message(worker_sid, "user", "Do the thing")
    db.add_message(worker_sid, "assistant", "Did half the thing")
    db.add_message(worker_sid, "tool", "output")

    session = db.get_session(worker_sid)

    mock_llm_client.responses = [
        ChatResponse(
            content=json.dumps(
                {
                    "verdict": "retry",
                    "reasoning": "Task not complete",
                    "diagnostic": "",
                    "what_worked": "",
                    "what_failed": "",
                    "strategy": "Try harder",
                }
            ),
            tool_calls=None,
            usage=TokenUsage(10, 5, 15),
            model="test",
            provider="fake",
            finish_reason="stop",
        )
    ]

    session_obj = AgentSession(session_id=worker_sid, session_type="worker")
    events = []
    await _maybe_reflect(worker_sid, session, emit=events.append, session_obj=session_obj)

    # Worker-specific cap (2), not main cap (5). First retry: count 0→1, 1 < 2
    # so retry is honored, reflect.retry fires with max=2.
    retry_events = [e for e in events if e.get("type") == "reflect.retry"]
    assert len(retry_events) == 1
    assert retry_events[0]["max"] == 2  # worker-specific cap, not 5
    assert session_obj.turn.reflect_count == 1
    assert session_obj.turn.reflect_retry_requested is True


async def test_maybe_reflect_worker_cap_emits_exhausted_not_phantom_retry(
    mock_llm_client,
    monkeypatch,
):
    """When retry verdict would cross the cap, emit exhausted (not retry)."""
    from db import models as db
    from sessions.state import AgentSession

    monkeypatch.setattr("config.settings.reflect_enabled", True)
    monkeypatch.setattr("config.settings.reflect_min_messages", 2)
    monkeypatch.setattr("config.settings.reflect_max_retries_worker", 2)

    parent_sid = db.create_session(title="Parent")
    worker_sid = db.create_session(
        title="Worker",
        session_type="worker",
        parent_session_id=parent_sid,
    )
    db.add_message(worker_sid, "user", "Do the thing")
    db.add_message(worker_sid, "assistant", "Still not done")
    db.add_message(worker_sid, "tool", "output")

    session = db.get_session(worker_sid)

    mock_llm_client.responses = [
        ChatResponse(
            content=json.dumps(
                {
                    "verdict": "retry",
                    "reasoning": "Still not done",
                    "diagnostic": "",
                    "what_worked": "",
                    "what_failed": "",
                    "strategy": "Try harder",
                }
            ),
            tool_calls=None,
            usage=TokenUsage(10, 5, 15),
            model="test",
            provider="fake",
            finish_reason="stop",
        )
    ]

    session_obj = AgentSession(session_id=worker_sid, session_type="worker")
    session_obj.turn.reflect_count = 1  # one retry already used, cap=2

    events = []
    await _maybe_reflect(worker_sid, session, emit=events.append, session_obj=session_obj)

    # 2nd retry would increment to 2, 2 < 2 is False → exhausted, not retry.
    assert not any(e.get("type") == "reflect.retry" for e in events)
    assert any(e.get("type") == "reflect.exhausted" for e in events)
    assert session_obj.turn.reflect_retry_requested is False


async def test_maybe_reflect_skips_errored():
    from db import models as db
    from sessions.state import AgentSession

    sid = db.create_session()
    session = db.get_session(sid)
    session_obj = AgentSession(session_id=sid)
    session_obj.error = "Something went wrong"
    # Error state → skip reflect
    await _maybe_reflect(sid, session, session_obj=session_obj)


async def test_maybe_reflect_skips_insufficient_messages():
    from db import models as db
    from sessions.state import AgentSession

    sid = db.create_session()
    db.add_message(sid, "user", "hi")
    session = db.get_session(sid)
    session_obj = AgentSession(session_id=sid)
    # Fewer messages than reflect_min_messages → skip
    await _maybe_reflect(sid, session, session_obj=session_obj)


async def test_maybe_reflect_pass_verdict(mock_llm_client, monkeypatch):
    from db import models as db
    from sessions.state import AgentSession

    monkeypatch.setattr("config.settings.reflect_enabled", True)
    # Synchronous path under test — interactive sessions defer their grade
    # by default now (reflect_deferred_normal), which skips this code.
    monkeypatch.setattr("config.settings.reflect_deferred_normal", False)
    monkeypatch.setattr("config.settings.reflect_min_messages", 2)
    monkeypatch.setattr("config.settings.reflect_max_retries", 2)

    sid = db.create_session()
    db.add_message(sid, "user", "Fix the login bug")
    db.add_message(sid, "assistant", "I fixed the bug in auth.py")
    db.add_message(sid, "tool", "file written successfully")

    session = db.get_session(sid)
    session_obj = AgentSession(session_id=sid)

    mock_llm_client.responses = [
        ChatResponse(
            content='{"verdict": "pass", "reasoning": "Task completed"}',
            tool_calls=None,
            usage=TokenUsage(10, 5, 15),
            model="test",
            provider="fake",
            finish_reason="stop",
        )
    ]

    events = []
    await _maybe_reflect(sid, session, emit=events.append, session_obj=session_obj)
    # reflect.done event should be emitted
    assert any(e.get("type") == "reflect.done" for e in events)
    # No retry should be requested on pass
    assert not session_obj.turn.reflect_retry_requested


async def test_maybe_reflect_retry_verdict(mock_llm_client, monkeypatch):
    from db import models as db
    from sessions.state import AgentSession

    monkeypatch.setattr("config.settings.reflect_enabled", True)
    monkeypatch.setattr("config.settings.reflect_deferred_normal", False)
    monkeypatch.setattr("config.settings.reflect_min_messages", 2)
    monkeypatch.setattr("config.settings.reflect_max_retries", 2)

    sid = db.create_session()
    db.add_message(sid, "user", "Create a report")
    db.add_message(sid, "assistant", "I tried but failed")
    db.add_message(sid, "tool", "error")

    session = db.get_session(sid)
    session_obj = AgentSession(session_id=sid)

    mock_llm_client.responses = [
        ChatResponse(
            content=json.dumps(
                {
                    "verdict": "retry",
                    "reasoning": "Report not created",
                    "diagnostic": "Wrong approach",
                    "what_worked": "",
                    "what_failed": "all tools",
                    "strategy": "Use different method",
                }
            ),
            tool_calls=None,
            usage=TokenUsage(10, 5, 15),
            model="test",
            provider="fake",
            finish_reason="stop",
        )
    ]

    await _maybe_reflect(sid, session, session_obj=session_obj)
    # Retry should be requested
    assert session_obj.turn.reflect_retry_requested
    assert session_obj.turn.reflect_count == 1


async def test_maybe_reflect_retry_blocked_when_budget_below_3x_scout(
    mock_llm_client,
    monkeypatch,
):
    """Regression for session 4b184273f4b5: reflect verdict=retry was honored
    even though only ~420s of budget remained, but scout's worst case is
    primary + retry + fallback = 3 × scout_timeout. The retry then burned
    the entire remaining budget in scout and the agent's first acquire
    instantly raised LLMSessionTimeoutError.

    Guard: with scout_timeout=180 the floor is 3*180+30 = 570s. A session
    with 500s remaining must be refused (escalated, not retried).
    """
    from db import models as db
    from sessions.state import AgentSession

    monkeypatch.setattr("config.settings.reflect_enabled", True)
    monkeypatch.setattr("config.settings.reflect_deferred_normal", False)
    monkeypatch.setattr("config.settings.reflect_min_messages", 2)
    monkeypatch.setattr("config.settings.reflect_max_retries", 2)
    monkeypatch.setattr("config.settings.scout_timeout", 180)
    # 500s remaining < 3*180+30 = 570s ⇒ retry should be blocked.
    monkeypatch.setattr(
        "core.llm.client.session_seconds_remaining",
        lambda _sid: 500.0,
    )

    sid = db.create_session()
    db.add_message(sid, "user", "Create a report")
    db.add_message(sid, "assistant", "I tried but failed")
    db.add_message(sid, "tool", "error")

    session = db.get_session(sid)
    session_obj = AgentSession(session_id=sid)

    mock_llm_client.responses = [
        ChatResponse(
            content=json.dumps(
                {
                    "verdict": "retry",
                    "reasoning": "Report not created",
                    "strategy": "Try again",
                }
            ),
            tool_calls=None,
            usage=TokenUsage(10, 5, 15),
            model="test",
            provider="fake",
            finish_reason="stop",
        )
    ]

    events = []
    await _maybe_reflect(
        sid,
        session,
        emit=events.append,
        session_obj=session_obj,
    )
    # Retry MUST be refused — budget < 3x scout floor.
    assert not session_obj.turn.reflect_retry_requested, "retry should be blocked when remaining < 3*scout_timeout+30"
    assert any(e.get("type") == "reflect.budget_exhausted" for e in events), (
        f"expected reflect.budget_exhausted event, got: " f"{[e.get('type') for e in events]}"
    )


async def test_maybe_reflect_retry_allowed_when_budget_above_3x_scout(
    mock_llm_client,
    monkeypatch,
):
    """Sibling: with budget comfortably above 3*scout_timeout+30, retry runs."""
    from db import models as db
    from sessions.state import AgentSession

    monkeypatch.setattr("config.settings.reflect_enabled", True)
    monkeypatch.setattr("config.settings.reflect_deferred_normal", False)
    monkeypatch.setattr("config.settings.reflect_min_messages", 2)
    monkeypatch.setattr("config.settings.reflect_max_retries", 2)
    monkeypatch.setattr("config.settings.scout_timeout", 180)
    # 800s > 3*180+30 = 570s ⇒ retry allowed.
    monkeypatch.setattr(
        "core.llm.client.session_seconds_remaining",
        lambda _sid: 800.0,
    )

    sid = db.create_session()
    db.add_message(sid, "user", "Create a report")
    db.add_message(sid, "assistant", "I tried but failed")
    db.add_message(sid, "tool", "error")

    session = db.get_session(sid)
    session_obj = AgentSession(session_id=sid)

    mock_llm_client.responses = [
        ChatResponse(
            content=json.dumps(
                {
                    "verdict": "retry",
                    "reasoning": "Report not created",
                    "strategy": "Try again",
                }
            ),
            tool_calls=None,
            usage=TokenUsage(10, 5, 15),
            model="test",
            provider="fake",
            finish_reason="stop",
        )
    ]

    await _maybe_reflect(sid, session, session_obj=session_obj)
    assert session_obj.turn.reflect_retry_requested
    assert session_obj.turn.reflect_count == 1


# ---------------------------------------------------------------------------
# Deferred reflect (interactive sessions)
# ---------------------------------------------------------------------------


def _reflect_rows(sid):
    from db import models as db

    return [json.loads(m["content"]) for m in db.get_messages(sid) if m["role"] == "reflect"]


def _notice_rows(sid):
    from db import models as db

    return [m["content"] for m in db.get_messages(sid) if m["role"] == "notice"]


def _graded_turn(title="Deferred"):
    """A session with just enough turn substance to clear reflect_min_messages.

    Rows carry the parent_user_msg_id stamp the manager writes, so the
    turn-scoped substance gate counts them. Returns (session_id, user_msg_id).
    """
    from db import models as db

    sid = db.create_session(title=title)
    uid = db.add_message(sid, "user", "Fix the login bug")
    meta = json.dumps({"parent_user_msg_id": uid})
    db.add_message(sid, "assistant", "Fixed it in auth.py", metadata=meta)
    db.add_message(sid, "tool", "file written", metadata=meta)
    return sid, uid


def _verdict_response(**overrides):
    payload = {"verdict": "pass", "reasoning": "Task completed", "failure_cause": "none"}
    payload.update(overrides)
    return ChatResponse(
        content=json.dumps(payload),
        tool_calls=None,
        usage=TokenUsage(10, 5, 15),
        model="test",
        provider="fake",
        finish_reason="stop",
    )


async def test_normal_session_defers_the_grade(mock_llm_client, monkeypatch):
    """The measured cost of synchronous reflect (16.5s median, 47s p90) is paid
    entirely in front of a waiting human. Interactive turns now schedule the
    grade and finalize immediately — no reflect LLM call on this path."""
    from db import models as db
    from sessions.state import AgentSession

    monkeypatch.setattr("config.settings.reflect_enabled", True)
    monkeypatch.setattr("config.settings.reflect_min_messages", 2)
    monkeypatch.setattr("config.settings.reflect_deferred_normal", True)

    scheduled = []

    async def _fake_task(session_obj, snap):
        scheduled.append(snap)

    monkeypatch.setattr("sessions.hooks._deferred_reflect_task", _fake_task)

    sid, uid = _graded_turn()
    session_obj = AgentSession(session_id=sid)
    session_obj.current_turn_user_msg_id = uid
    events = []

    await _maybe_reflect(sid, db.get_session(sid), emit=events.append, session_obj=session_obj)
    await asyncio.sleep(0)  # let the detached task start

    assert mock_llm_client.call_count == 0, "deferred path must not spend an LLM call inline"
    assert _reflect_rows(sid) == []
    assert not session_obj.turn.reflect_retry_requested
    assert any(e.get("type") == "reflect.deferred_scheduled" for e in events)
    assert len(scheduled) == 1
    assert scheduled[0].ticket == 1
    assert scheduled[0].turn_user_msg_id == session_obj.current_turn_user_msg_id


async def test_cron_sessions_keep_the_synchronous_grade(mock_llm_client, monkeypatch):
    """Deferral is about a human waiting. Unattended session types keep the
    blocking, retry-capable path even with the setting on."""
    from db import models as db
    from sessions.state import AgentSession

    monkeypatch.setattr("config.settings.reflect_enabled", True)
    monkeypatch.setattr("config.settings.reflect_min_messages", 2)
    monkeypatch.setattr("config.settings.reflect_deferred_normal", True)

    # Lesson recall pins the process-wide memory-store singleton to this
    # test's tmp dir; keep it out of the way.
    monkeypatch.setattr("core.memory.store.get_memory_store", lambda: None)
    sid = db.create_session(title="Cron", session_type="cron")
    db.add_message(sid, "user", "Nightly sweep")
    db.add_message(sid, "assistant", "Swept")
    db.add_message(sid, "tool", "ok")

    session_obj = AgentSession(session_id=sid, session_type="cron")
    mock_llm_client.responses = [_verdict_response()]
    events = []

    await _maybe_reflect(sid, db.get_session(sid), emit=events.append, session_obj=session_obj)

    assert mock_llm_client.call_count == 1
    assert any(e.get("type") == "reflect.done" for e in events)
    assert not any(e.get("type") == "reflect.deferred_scheduled" for e in events)


async def test_sync_reflect_row_is_stamped_sync(mock_llm_client, monkeypatch):
    """Setting off → today's behavior, plus the regime marker."""
    from db import models as db
    from sessions.state import AgentSession

    monkeypatch.setattr("config.settings.reflect_enabled", True)
    monkeypatch.setattr("config.settings.reflect_min_messages", 2)
    monkeypatch.setattr("config.settings.reflect_deferred_normal", False)

    # Lesson recall pins the process-wide memory-store singleton to this
    # test's tmp dir; keep it out of the way.
    monkeypatch.setattr("core.memory.store.get_memory_store", lambda: None)
    sid, uid = _graded_turn("Sync stamp")
    session_obj = AgentSession(session_id=sid)
    mock_llm_client.responses = [_verdict_response()]

    await _maybe_reflect(sid, db.get_session(sid), session_obj=session_obj)

    rows = _reflect_rows(sid)
    assert len(rows) == 1
    assert rows[0]["reflect_mode"] == "sync"


async def test_deferred_grade_skips_a_superseded_turn(mock_llm_client, monkeypatch):
    """Rapid-fire policy: only the latest completed turn gets graded. A turn
    the user has already moved past is marked ungraded, not graded late."""
    from db import models as db
    from sessions.hooks import _deferred_reflect_task, _DeferredGrade
    from sessions.state import AgentSession

    monkeypatch.setattr("config.settings.reflect_defer_idle_s", 0)

    sid, uid = _graded_turn("Superseded")
    session_obj = AgentSession(session_id=sid)
    snap = _DeferredGrade(session_id=sid, ticket=1, turn_id=0, turn_user_msg_id=None, attempt=1)
    # A later turn scheduled its own grade while this one slept.
    session_obj._deferred_reflect_seq = 2

    await _deferred_reflect_task(session_obj, snap)

    assert mock_llm_client.call_count == 0
    assert _reflect_rows(sid) == []
    assert any("superseded by a newer turn" in n for n in _notice_rows(sid))


async def test_deferred_grade_skips_while_a_turn_is_in_flight(mock_llm_client, monkeypatch):
    from db import models as db
    from sessions.hooks import _deferred_reflect_task, _DeferredGrade
    from sessions.state import AgentSession

    monkeypatch.setattr("config.settings.reflect_defer_idle_s", 0)

    sid, uid = _graded_turn("In flight")
    session_obj = AgentSession(session_id=sid)
    session_obj._deferred_reflect_seq = 1
    session_obj.current_turn_user_msg_id = 4242  # a new turn owns the session

    snap = _DeferredGrade(session_id=sid, ticket=1, turn_id=0, turn_user_msg_id=None, attempt=1)
    await _deferred_reflect_task(session_obj, snap)

    assert mock_llm_client.call_count == 0
    assert _reflect_rows(sid) == []


async def test_deferred_grade_is_observe_only(mock_llm_client, monkeypatch):
    """A deferred retry verdict records everything and retries nothing: the
    turn is over, its scratchpad is gone, and a new turn may already own the
    session."""
    from db import models as db
    from sessions.hooks import _deferred_reflect_task, _DeferredGrade
    from sessions.state import AgentSession

    monkeypatch.setattr("config.settings.reflect_defer_idle_s", 0)

    # Lesson recall pins the process-wide memory-store singleton to this
    # test's tmp dir; keep it out of the way.
    monkeypatch.setattr("core.memory.store.get_memory_store", lambda: None)
    sid, uid = _graded_turn("Observe only")
    session_obj = AgentSession(session_id=sid)
    session_obj._deferred_reflect_seq = 1
    mock_llm_client.responses = [
        _verdict_response(verdict="retry", reasoning="report.md missing", failure_cause="agent", strategy="write it")
    ]

    events = []
    monkeypatch.setattr(session_obj, "emit_event", events.append, raising=False)

    snap = _DeferredGrade(session_id=sid, ticket=1, turn_id=0, turn_user_msg_id=None, attempt=1)
    await _deferred_reflect_task(session_obj, snap)

    rows = _reflect_rows(sid)
    assert len(rows) == 1
    assert rows[0]["verdict"] == "retry"
    assert rows[0]["reflect_mode"] == "deferred"
    # The whole point: no retry flag, no retry counter movement, no exclusions.
    assert not session_obj.turn.reflect_retry_requested
    assert session_obj.turn.reflect_count == 0
    assert session_obj.turn.reflect_lessons == ""
    assert session_obj.turn.retry_excluded_tools == set()
    assert any(e.get("type") == "reflect.deferred" and e.get("verdict") == "retry" for e in events)
    # Post-mortem carries the regime marker for the calibration review.
    payload = json.loads(db.list_post_mortems(session_id=sid)[0]["payload_json"])
    assert payload["reflect_mode"] == "deferred"
    assert session_obj.has_background_tasks is False, "background ref must be released"


async def test_failing_gate_still_clamps_when_reflect_is_deferred(mock_llm_client, monkeypatch):
    """Gates are deterministic and material by construction, so their retry
    survives the move to observe-only grading — it is now the only mechanical
    retry path an interactive turn has."""
    from core.gates import GateResult
    from db import models as db
    from sessions.state import AgentSession

    monkeypatch.setattr("config.settings.reflect_enabled", True)
    monkeypatch.setattr("config.settings.reflect_min_messages", 2)
    monkeypatch.setattr("config.settings.reflect_deferred_normal", True)
    monkeypatch.setattr("config.settings.reflect_max_retries", 2)

    async def _fake_task(session_obj, snap):
        return None

    monkeypatch.setattr("sessions.hooks._deferred_reflect_task", _fake_task)

    sid, uid = _graded_turn("Gate clamp")
    session_obj = AgentSession(session_id=sid)
    failing = GateResult(name="tests", command="pytest -q", passed=False, exit_code=1, output_tail="2 failed")

    events = []
    await _maybe_reflect(sid, db.get_session(sid), emit=events.append, session_obj=session_obj, gate_results=[failing])
    await asyncio.sleep(0)

    assert session_obj.turn.reflect_retry_requested is True
    assert session_obj.turn.reflect_count == 1
    assert "tests" in session_obj.turn.reflect_lessons
    assert mock_llm_client.call_count == 0


async def test_deferred_grade_feeds_candor_the_verdict_and_experience(mock_llm_client, monkeypatch):
    """Candor ran during post-hooks with no verdict (the grade hadn't happened
    yet). The deferred grade emits the two families only reflect can produce —
    otherwise interactive turns, the ones the experience read exists for, would
    contribute none of it."""
    from db import models as db
    from sessions.hooks import _deferred_reflect_task, _DeferredGrade
    from sessions.state import AgentSession

    monkeypatch.setattr("config.settings.reflect_defer_idle_s", 0)
    monkeypatch.setattr("config.settings.candor_enabled", True)
    monkeypatch.setattr("config.settings.reflect_experience", True)
    monkeypatch.setattr("core.memory.store.get_memory_store", lambda: None)

    recorded = []

    class _FakeBridge:
        async def record(self, observations):
            recorded.append(observations)
            return {"observed": len(observations)}

    monkeypatch.setattr("core.extensions.candor.bridge.get_candor_bridge", lambda: _FakeBridge())

    sid, uid = _graded_turn("Candor deferred")
    session_obj = AgentSession(session_id=sid)
    session_obj._deferred_reflect_seq = 1
    mock_llm_client.responses = [
        _verdict_response(
            experience={
                "user_sentiment": "frustrated",
                "clarification_loop": True,
                "first_response_sufficient": False,
                "friction": ["tone_mismatch"],
                "note": "user had to restate the ask",
            }
        )
    ]

    snap = _DeferredGrade(
        session_id=sid,
        ticket=1,
        turn_id=0,
        turn_user_msg_id=None,
        attempt=1,
        model="test-model",
        session_kind="normal",
    )
    await _deferred_reflect_task(session_obj, snap)

    assert recorded, "deferred grade recorded no Candor observations"
    preds = {o["pred"] for o in recorded[0]}
    assert "reflect_verdict" in preds
    assert {"user_sentiment", "friction_mode", "no_clarification_needed", "first_response_sufficient"} <= preds
    verdict_obs = next(o for o in recorded[0] if o["pred"] == "reflect_verdict")
    assert verdict_obs["value"] == "pass"
    assert verdict_obs["ctx"]["model"] == "test-model"
    # Tool and turn outcomes belong to the synchronous emission — re-observing
    # them here would double-count every tool call in the reliability ledger.
    assert not ({"tool_ok", "turn_ok", "tool_failure_mode"} & preds)
    # The turn ledger belongs to a turn that is over.
    assert session_obj.turn.candor_emitted is None
    assert session_obj.turn.candor_reflect is None
