"""Tests for sessions/manager.py: lifecycle, prompt routing, events, maintenance."""

import asyncio

import pytest

from sessions.manager import SessionManager
from sessions.state import AgentSession, SessionState


def _make_manager() -> SessionManager:
    """Create a fresh SessionManager (not the singleton)."""
    return SessionManager()


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------


def test_create_session():
    mgr = _make_manager()
    sid = mgr.create_session(title="Test Session")
    assert sid is not None
    # Should be in memory
    assert mgr.get(sid) is not None
    # Should be in DB
    from db import models as db

    s = db.get_session(sid)
    assert s is not None
    assert s["title"] == "Test Session"


def test_create_session_worker():
    mgr = _make_manager()
    parent_sid = mgr.create_session(title="Parent")
    worker_sid = mgr.create_session(title="Worker", session_type="worker", parent_session_id=parent_sid)
    worker = mgr.get(worker_sid)
    assert worker.session_type == "worker"
    assert worker.parent_session_id == parent_sid


def test_get_or_create_from_db():
    from db import models as db

    mgr = _make_manager()
    # Create in DB only
    sid = db.create_session(title="DB-only session")
    # get_or_create should load it from DB
    session = mgr.get_or_create(sid)
    assert session.session_id == sid


def test_get_or_create_missing():
    mgr = _make_manager()
    with pytest.raises(ValueError, match="not found"):
        mgr.get_or_create("nonexistent-session-id")


def test_get_returns_none_for_missing():
    mgr = _make_manager()
    assert mgr.get("not-a-session") is None


def test_delete_session():
    mgr = _make_manager()
    sid = mgr.create_session(title="To Delete")
    assert mgr.get(sid) is not None
    mgr.delete_session(sid)
    assert mgr.get(sid) is None
    from db import models as db

    assert db.get_session(sid) is None


def test_delete_session_cascades_workers():
    mgr = _make_manager()
    parent = mgr.create_session(title="Parent")
    worker = mgr.create_session(title="Worker", session_type="worker", parent_session_id=parent)
    # Register worker in parent's worker_ids so cascade works
    parent_session = mgr.get(parent)
    parent_session.worker_ids.append(worker)
    mgr.delete_session(parent)
    assert mgr.get(worker) is None


def test_remove_does_not_delete_db():
    mgr = _make_manager()
    sid = mgr.create_session(title="Remove Only")
    mgr.remove(sid)
    assert mgr.get(sid) is None
    from db import models as db

    assert db.get_session(sid) is not None


# ---------------------------------------------------------------------------
# prompt routing
# ---------------------------------------------------------------------------


async def test_prompt_no_runner():
    mgr = _make_manager()
    sid = mgr.create_session(title="No Runner")
    events = []
    session = mgr.get(sid)
    session.subscribers.append(asyncio.Queue(maxsize=100))
    # No agent runner set — should emit stream.error
    await mgr.prompt(sid, "hello")
    # Check event was emitted
    all_events = list(session.events)
    assert any(e.get("type") == "stream.error" for e in all_events)


async def test_prompt_starts_task():
    mgr = _make_manager()
    sid = mgr.create_session(title="With Runner")

    completed = asyncio.Event()

    async def fake_runner(session_id, message, session, **kwargs):
        completed.set()

    mgr.set_agent_runner(fake_runner)
    await mgr.prompt(sid, "hello")

    # Task should be created
    session = mgr.get(sid)
    assert session.task is not None

    # Wait for it to finish
    try:
        await asyncio.wait_for(asyncio.shield(session.task), timeout=2.0)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass


async def test_prompt_queues_when_busy():
    mgr = _make_manager()
    sid = mgr.create_session(title="Busy Session")

    hold = asyncio.Event()

    async def blocking_runner(session_id, message, session, **kwargs):
        await asyncio.wait_for(hold.wait(), timeout=5.0)

    mgr.set_agent_runner(blocking_runner)

    # First prompt — starts agent
    await mgr.prompt(sid, "first message")

    # Second prompt while first is running — should queue
    session = mgr.get(sid)
    # Force PROCESSING state to simulate busy, and push last_user_msg_at
    # outside the rapid-fire window so the message is queued rather than
    # combined into the running turn's DB row.
    import time as _t

    session._force_state_for_tests(SessionState.PROCESSING)
    session.last_user_msg_at = _t.monotonic() - 10
    await mgr.prompt(sid, "second message")

    assert len(session.pending_messages) >= 1
    hold.set()


async def test_prompt_rejects_full_queue(monkeypatch):
    monkeypatch.setattr("config.settings.max_pending_messages", 2)
    mgr = _make_manager()
    sid = mgr.create_session(title="Full Queue")
    session = mgr.get(sid)

    # Manually fill the queue
    session.pending_messages.append(("msg1", ""))
    session.pending_messages.append(("msg2", ""))
    # Force PROCESSING state
    session._force_state_for_tests(SessionState.PROCESSING)

    events_before = len(session.events)
    await mgr.prompt(sid, "overflow message")

    # queue_full event should be emitted
    new_events = list(session.events)[events_before:]
    assert any(e.get("type") == "session.queue_full" for e in new_events)


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


def test_broadcast_reaches_global_subscriber():
    mgr = _make_manager()
    q = mgr.subscribe_global()
    mgr.broadcast({"type": "test.event", "data": "hello"})
    assert not q.empty()
    event = q.get_nowait()
    assert event["type"] == "test.event"
    assert event["_global"] is True


def test_broadcast_reaches_session_subscriber():
    mgr = _make_manager()
    sid = mgr.create_session(title="Sub Test")
    session = mgr.get(sid)
    q = session.subscribe()
    mgr.broadcast({"type": "global.event"})
    assert not q.empty()


def test_subscribe_global_unsubscribe():
    mgr = _make_manager()
    q = mgr.subscribe_global()
    mgr.unsubscribe_global(q)
    mgr.broadcast({"type": "after_unsub"})
    assert q.empty()


# ---------------------------------------------------------------------------
# get_status
# ---------------------------------------------------------------------------


def test_get_status_unknown():
    mgr = _make_manager()
    status = mgr.get_status("unknown-id")
    assert status["status"] == "unknown"


def test_get_status_known():
    mgr = _make_manager()
    sid = mgr.create_session(title="Status Test")
    status = mgr.get_status(sid)
    assert status["status"] == "idle"
    assert status["session_id"] == sid
    assert "pending_messages" in status


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------


def test_active_count():
    mgr = _make_manager()
    assert mgr.active_count() == 0
    sid1 = mgr.create_session(title="S1")
    sid2 = mgr.create_session(title="S2")
    assert mgr.active_count() == 2


def test_reap_idle_sessions():
    mgr = _make_manager()
    sid = mgr.create_session(title="Idle")
    session = mgr.get(sid)
    # Force session to appear very old
    session.last_activity_time = session.last_activity_time - 7200  # 2 hours ago
    count = mgr.reap_idle_sessions(max_idle=60)
    assert count >= 1
    assert mgr.get(sid) is None


def test_reap_idle_skips_with_subscribers():
    mgr = _make_manager()
    sid = mgr.create_session(title="Active")
    session = mgr.get(sid)
    session.last_activity_time = session.last_activity_time - 7200
    q = session.subscribe()  # Add a subscriber
    count = mgr.reap_idle_sessions(max_idle=60)
    assert mgr.get(sid) is not None  # Should NOT be reaped


def test_reap_dead_subscribers():
    mgr = _make_manager()
    sid = mgr.create_session(title="Dead Sub")
    session = mgr.get(sid)
    # Add a full queue (dead subscriber)
    dead_q = asyncio.Queue(maxsize=1)
    dead_q.put_nowait({"type": "test"})  # Fill it
    session.subscribers.append(dead_q)
    count = mgr.reap_dead_subscribers()
    assert count >= 1
    assert dead_q not in session.subscribers


# ---------------------------------------------------------------------------
# _finalize_worker — header driven by termination_reason (not session.state,
# which is force-reset to IDLE before this runs)
# ---------------------------------------------------------------------------


def _make_worker_with_assistant(
    mgr: SessionManager,
    last_content: str,
    reflect_verdict: str | None = None,
) -> AgentSession:
    """Helper: create a worker session with one assistant message and return the in-memory obj.

    When reflect_verdict is provided, also appends a reflect row so
    _finalize_worker can see the quality gate's decision.
    """
    import json as _json

    from db import models as db

    parent_sid = mgr.create_session(title="Parent")
    worker_sid = mgr.create_session(
        title="W",
        session_type="worker",
        parent_session_id=parent_sid,
    )
    db.add_message(worker_sid, "user", "go")
    db.add_message(worker_sid, "assistant", last_content)
    if reflect_verdict is not None:
        db.add_message(
            worker_sid,
            "reflect",
            _json.dumps(
                {
                    "verdict": reflect_verdict,
                    "reasoning": f"test: {reflect_verdict}",
                }
            ),
        )
    return mgr.get(worker_sid)


async def test_finalize_worker_header_complete_reflect_pass():
    """Normal completion with reflect=pass → AUTO-STAMPED header."""
    from pathlib import Path

    from config import settings as _s

    mgr = _make_manager()
    w = _make_worker_with_assistant(mgr, "I finished.", reflect_verdict="pass")
    w.termination_reason = "complete"
    await mgr._finalize_worker(w)
    p = Path(_s.workspace_dir) / f".worker_{w.session_id[:12]}_summary.md"
    assert p.exists()
    text = p.read_text()
    assert text.startswith("# AUTO-STAMPED")
    assert "reflect=pass" in text
    assert "I finished." in text


async def test_finalize_worker_header_complete_no_reflect():
    """Completion without a reflect row → UNVERIFIED header (quality not gated)."""
    from pathlib import Path

    from config import settings as _s

    mgr = _make_manager()
    w = _make_worker_with_assistant(mgr, "I finished.")  # no reflect row
    w.termination_reason = "complete"
    await mgr._finalize_worker(w)
    text = (Path(_s.workspace_dir) / f".worker_{w.session_id[:12]}_summary.md").read_text()
    assert text.startswith("# UNVERIFIED")
    assert "I finished." in text


async def test_finalize_worker_header_reflect_escalate():
    """Reflect verdict=escalate → ESCALATED header with reasoning visible."""
    from pathlib import Path

    from config import settings as _s

    mgr = _make_manager()
    w = _make_worker_with_assistant(
        mgr,
        "I'll research this systematically.",
        reflect_verdict="escalate",
    )
    w.termination_reason = "complete"
    await mgr._finalize_worker(w)
    text = (Path(_s.workspace_dir) / f".worker_{w.session_id[:12]}_summary.md").read_text()
    assert text.startswith("# ESCALATED")
    assert "test: escalate" in text  # reasoning propagated
    assert "get_worker_transcript" in text  # actionable hint present


async def test_finalize_worker_header_round_ceiling():
    from pathlib import Path

    from config import settings as _s

    mgr = _make_manager()
    w = _make_worker_with_assistant(mgr, "mid-thought")
    w.termination_reason = "round_ceiling"
    await mgr._finalize_worker(w)
    text = (Path(_s.workspace_dir) / f".worker_{w.session_id[:12]}_summary.md").read_text()
    assert text.startswith("# INCOMPLETE")
    assert "round ceiling" in text


async def test_finalize_worker_header_cancelled():
    from pathlib import Path

    from config import settings as _s

    mgr = _make_manager()
    w = _make_worker_with_assistant(mgr, "stopped early")
    w.termination_reason = "cancelled"
    await mgr._finalize_worker(w)
    text = (Path(_s.workspace_dir) / f".worker_{w.session_id[:12]}_summary.md").read_text()
    assert text.startswith("# CANCELLED")


async def test_finalize_worker_header_error():
    from pathlib import Path

    from config import settings as _s

    mgr = _make_manager()
    w = _make_worker_with_assistant(mgr, "crashed")
    w.termination_reason = "error"
    w.error = "connection reset"
    await mgr._finalize_worker(w)
    text = (Path(_s.workspace_dir) / f".worker_{w.session_id[:12]}_summary.md").read_text()
    assert text.startswith("# ERROR")
    assert "connection reset" in text


async def test_finalize_worker_header_compaction_failed():
    from pathlib import Path

    from config import settings as _s

    mgr = _make_manager()
    w = _make_worker_with_assistant(mgr, "out of ctx")
    w.termination_reason = "compaction_failed"
    await mgr._finalize_worker(w)
    text = (Path(_s.workspace_dir) / f".worker_{w.session_id[:12]}_summary.md").read_text()
    assert text.startswith("# INCOMPLETE")


async def test_finalize_worker_truncates_large_last_message():
    """A verbose last assistant message should be truncated with a trailing marker."""
    from pathlib import Path

    from config import settings as _s

    mgr = _make_manager()
    giant = "x" * 50_000
    w = _make_worker_with_assistant(mgr, giant, reflect_verdict="pass")
    w.termination_reason = "complete"
    await mgr._finalize_worker(w)
    p = Path(_s.workspace_dir) / f".worker_{w.session_id[:12]}_summary.md"
    text = p.read_text()
    assert text.endswith("[truncated]")
    # Body cap is 8000 chars + header + truncation marker — comfortably below 50k.
    assert len(text) < 9000


async def test_finalize_worker_skips_if_summary_already_exists():
    """If the worker wrote its own summary file, finalize must not overwrite it."""
    from pathlib import Path

    from config import settings as _s

    mgr = _make_manager()
    w = _make_worker_with_assistant(mgr, "fallback content", reflect_verdict="pass")
    w.termination_reason = "complete"
    # Pre-write the file — as a real worker would via file_write.
    workspace = Path(_s.workspace_dir)
    workspace.mkdir(parents=True, exist_ok=True)
    p = workspace / f".worker_{w.session_id[:12]}_summary.md"
    p.write_text("worker's own summary")
    await mgr._finalize_worker(w)
    assert p.read_text() == "worker's own summary"


# ---------------------------------------------------------------------------
# prompt() reset (I1): per-turn reset must clear session.error and
# termination_reason, otherwise _finalize_worker's fallback branch can
# mis-classify a clean turn as errored based on last-turn state.
# ---------------------------------------------------------------------------


async def test_prompt_resets_session_error_and_termination_reason():
    """A new turn on a session that previously errored must start clean."""
    mgr = _make_manager()
    # Stub the agent runner so the task runs quickly and deterministically.
    runs: list[str] = []

    async def fake_runner(session_id, message, session, is_retry=False, pre_saved=False):
        runs.append(message)

    mgr.set_agent_runner(fake_runner)

    sid = mgr.create_session(title="Prior error")
    session = mgr.get(sid)
    session.error = "prior turn exploded"
    session.termination_reason = "error"

    await mgr.prompt(sid, "new message")
    # Wait for the agent task to finish.
    if session.task:
        await session.task

    # After a fresh turn starts, stale state from a prior turn is cleared.
    # (The new turn may set its own reason/error; what matters is the prompt()
    # reset ran BEFORE the agent started, which the stub records.)
    assert session.error is None
    assert runs == ["new message"]


@pytest.mark.asyncio
async def test_prompt_resets_llm_time_budget_for_new_user_turn(monkeypatch):
    """Regression for session 14af4333f6d8 (2026-04-28): a chat session
    got "LLM time budget exhausted (>1800s) — turn aborted before scout"
    after the user had been chatting with it. The cap was tracking
    wall-clock from the session's first ever LLM acquire, so a 30+ minute
    conversation locked the session out forever.

    Fix: SessionManager.prompt() calls reset_session_budget() before the
    new agent task starts. Each user turn gets a fresh wall-clock window
    (default 1800s).
    """
    mgr = _make_manager()

    async def fake_runner(session_id, message, session, is_retry=False, pre_saved=False):
        pass

    mgr.set_agent_runner(fake_runner)

    reset_calls: list[str] = []

    def fake_reset(sid):
        reset_calls.append(sid)

    monkeypatch.setattr(
        "core.llm.client.reset_session_budget",
        fake_reset,
    )

    sid = mgr.create_session(title="Long conversation")
    await mgr.prompt(sid, "hello")
    session = mgr.get(sid)
    if session.task:
        await session.task

    assert reset_calls == [sid], (
        f"prompt() must call reset_session_budget for the new turn; " f"reset_calls={reset_calls}"
    )

    # And again on the second user message — every fresh turn resets.
    reset_calls.clear()
    await mgr.prompt(sid, "follow up")
    if session.task:
        await session.task
    assert reset_calls == [sid]


@pytest.mark.asyncio
async def test_budget_exhaustion_fires_user_notification(monkeypatch):
    """When a session's LLM time budget runs out mid-turn, the user must
    be notified — recovery is one user message away (the new prompt resets
    the budget) but the user has to know WHICH session needs nudging.
    Without this, a silent stalled session looks indistinguishable from
    "the model is just slow" and the user has no actionable signal.

    Verifies:
      1. LLMSessionTimeoutError raised by the agent runner triggers
         _broadcast_session_timeout_notification.
      2. The notification carries the source session_id and a body
         that explains the user's recovery path ("send a new message").
      3. db.add_notification persists for the bell panel.
    """
    from sessions import manager as mgr_mod

    mgr = _make_manager()

    from core.llm.semaphore import LLMSessionTimeoutError

    async def boom_runner(session_id, message, session, is_retry=False, pre_saved=False):
        raise LLMSessionTimeoutError(f"Session {session_id[:12]} LLM time budget exhausted (>1800s)")

    mgr.set_agent_runner(boom_runner)

    notify_calls: list[dict] = []
    real_add = mgr_mod.db.add_notification

    def capturing_add(session_id="", title="", body="", urgency="normal"):
        notify_calls.append(
            {
                "session_id": session_id,
                "title": title,
                "body": body,
                "urgency": urgency,
            }
        )
        return f"nid-{len(notify_calls)}"

    monkeypatch.setattr(mgr_mod.db, "add_notification", capturing_add)

    # Bypass scout so the runner is the only LLM-touching code path.
    async def passthrough_scout(*a, **kw):
        from core.scout.report import ScoutReport

        return ScoutReport(approach_guidance="x")

    monkeypatch.setattr("core.scout.runner.run_scout", passthrough_scout)
    monkeypatch.setattr("core.scout.runner.build_session_brief", lambda *a, **kw: "")

    sid = mgr.create_session(title="Long convo")
    await mgr.prompt(sid, "trigger the boom")
    session = mgr.get(sid)
    if session.task:
        await session.task

    assert notify_calls, "no notification was persisted on budget exhaustion"
    nc = notify_calls[-1]
    assert nc["session_id"] == sid, f"notification must carry source session_id; got {nc}"
    assert "ran out" in nc["title"].lower() or "time" in nc["title"].lower()
    assert (
        "send" in nc["body"].lower() and "message" in nc["body"].lower()
    ), f"notification body must tell the user how to recover; got: {nc['body']!r}"
    assert nc["urgency"] == "high"


@pytest.mark.asyncio
async def test_budget_exhaustion_does_not_notify_for_workers(monkeypatch):
    """Worker sessions that exhaust their budget must NOT spam the user —
    workers are managed by their orchestrator (run_workflow handles
    extending budgets, retrying, falling back). Per-worker notifications
    on a 7-step workflow with retries would be noise. The orchestrator can
    surface a single roll-up notification if the workflow as a whole fails.
    """
    from sessions import manager as mgr_mod

    mgr = _make_manager()

    from core.llm.semaphore import LLMSessionTimeoutError

    async def boom_runner(session_id, message, session, is_retry=False, pre_saved=False):
        raise LLMSessionTimeoutError(f"Session {session_id[:12]} budget exhausted")

    mgr.set_agent_runner(boom_runner)

    notify_calls: list[dict] = []
    monkeypatch.setattr(
        mgr_mod.db,
        "add_notification",
        lambda **kw: notify_calls.append(kw) or "nid",
    )

    async def passthrough_scout(*a, **kw):
        from core.scout.report import ScoutReport

        return ScoutReport(approach_guidance="x")

    monkeypatch.setattr("core.scout.runner.run_scout", passthrough_scout)
    monkeypatch.setattr("core.scout.runner.build_session_brief", lambda *a, **kw: "")

    parent_sid = mgr.create_session(title="Parent")
    worker_sid = mgr.create_session(
        title="A worker",
        session_type="worker",
        parent_session_id=parent_sid,
    )
    await mgr.prompt(worker_sid, "trigger boom in worker")
    worker = mgr.get(worker_sid)
    if worker.task:
        await worker.task

    # Only the user-facing notifications path should fire — workers stay silent.
    user_notifs = [
        n for n in notify_calls if "ran out" in (n.get("title", "")).lower() or "time" in (n.get("title", "")).lower()
    ]
    assert not user_notifs, f"worker budget exhaustion should not fire a user notification; " f"got: {user_notifs}"


@pytest.mark.asyncio
async def test_prompt_resets_budget_when_answering_ask_user(monkeypatch):
    """When the agent calls ask_user, the session enters AWAITING_USER and
    pauses — possibly for a long time while the human reads, considers,
    and types a response. That waiting time accumulates against the wall-
    clock LLM budget too. When the user finally answers, the new prompt
    must reset the budget just like a fresh turn would.

    SessionManager.prompt() handles both IDLE_READY and AWAITING_USER on
    the same code path (the v2 state log distinguishes via reason
    "answer-received" vs "prompt-arrived"). This test pins that behavior:
    the budget reset MUST fire on the answer path too — otherwise a 20-
    minute "I'll think about this" pause could lock the session out the
    moment the user finally replies.
    """
    mgr = _make_manager()

    async def fake_runner(session_id, message, session, is_retry=False, pre_saved=False):
        pass

    mgr.set_agent_runner(fake_runner)

    reset_calls: list[str] = []
    monkeypatch.setattr(
        "core.llm.client.reset_session_budget",
        lambda sid: reset_calls.append(sid),
    )

    sid = mgr.create_session(title="Awaiting answer")
    session = mgr.get(sid)

    # Force the session into AWAITING_USER as if the agent had just called
    # ask_user — bypass a real turn for test speed/determinism.
    from sessions.state_v2 import SessionStateV2 as S
    from sessions.state_v2 import _set_state

    _set_state(session, S.AWAITING_USER)
    session._turn_id = 1  # has_started

    # User answers the question.
    await mgr.prompt(sid, "my answer is X")
    if session.task:
        await session.task

    assert reset_calls == [sid], (
        "AWAITING_USER → answer must reset the budget too — otherwise the "
        f"user's thinking time counts against the cap. reset_calls={reset_calls}"
    )


# ---------------------------------------------------------------------------
# Fix regressions
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# _check_session_budget_or_raise — preempt the LLMSessionTimeoutError cascade
# ---------------------------------------------------------------------------
# Regression for sessions bc6e98 / 4b184273 / 7b97cf7: when a session's LLM
# time budget was exhausted before turn start, run_scout's primary + fallback
# attempts both raised LLMSessionTimeoutError instantly, then the agent's
# first acquire raised it again — three ERROR log lines for one turn and a
# 0-content "agent-error" finalize. The helper raises ONCE at scout-phase
# entry so the existing scout-error path produces a single clean terminal.


def test_check_session_budget_silent_when_budget_remains(monkeypatch):
    """Helper must return silently when the budget is not exhausted."""
    from sessions.manager import _check_session_budget_or_raise

    monkeypatch.setattr(
        "core.llm.client.session_seconds_remaining",
        lambda _sid: 600.0,
    )
    # No exception means pass.
    _check_session_budget_or_raise("any-session")


def test_check_session_budget_raises_when_exhausted(monkeypatch):
    """Helper raises LLMSessionTimeoutError when remaining is 0.

    The exception type matters: the existing _run_agent_safe except-Exception
    block routes any error in SCOUTING state to FINALIZING via scout-error,
    so any exception works for control-flow — but using LLMSessionTimeoutError
    keeps log/error diagnostics accurate.
    """
    from core.llm.semaphore import LLMSessionTimeoutError
    from sessions.manager import _check_session_budget_or_raise

    monkeypatch.setattr(
        "core.llm.client.session_seconds_remaining",
        lambda _sid: 0.0,
    )
    with pytest.raises(LLMSessionTimeoutError):
        _check_session_budget_or_raise("dead-session")


def test_check_session_budget_raises_on_negative_remaining(monkeypatch):
    """Negative remaining (already past cap) is treated the same as zero."""
    from core.llm.semaphore import LLMSessionTimeoutError
    from sessions.manager import _check_session_budget_or_raise

    monkeypatch.setattr(
        "core.llm.client.session_seconds_remaining",
        lambda _sid: -42.0,
    )
    with pytest.raises(LLMSessionTimeoutError):
        _check_session_budget_or_raise("over-budget")


def test_check_session_budget_silent_on_lookup_failure(monkeypatch):
    """If session_seconds_remaining itself raises (router not initialised in
    test contexts), the helper must NOT propagate — preempting is best-effort.
    """
    from sessions.manager import _check_session_budget_or_raise

    def _explode(_sid):
        raise RuntimeError("router unavailable")

    monkeypatch.setattr(
        "core.llm.client.session_seconds_remaining",
        _explode,
    )
    # Must not raise — returns silently so the caller proceeds.
    _check_session_budget_or_raise("any-session")


def test_map_termination_to_v2_reason_includes_error():
    """'error' must map to agent-error/ERROR so agent.py's return-path errors
    don't get recorded as loop-complete in the state log."""
    from sessions import state_v2 as sv2
    from sessions.manager import _map_termination_to_v2_reason

    reason, term = _map_termination_to_v2_reason("error")
    assert reason == "agent-error"
    assert term is sv2.TerminationReason.ERROR


def test_map_termination_to_v2_reason_all_known_values():
    from sessions import state_v2 as sv2
    from sessions.manager import _map_termination_to_v2_reason

    cases = {
        "complete": ("loop-complete", sv2.TerminationReason.COMPLETE),
        "round_ceiling": ("round-ceiling", sv2.TerminationReason.ROUND_CEILING),
        "compaction_failed": ("compaction-failed", sv2.TerminationReason.COMPACTION_FAILED),
        "error": ("agent-error", sv2.TerminationReason.ERROR),
        None: ("loop-complete", sv2.TerminationReason.COMPLETE),  # default
    }
    for inp, (exp_reason, exp_term) in cases.items():
        got_reason, got_term = _map_termination_to_v2_reason(inp)
        assert got_reason == exp_reason, f"for {inp!r}: reason {got_reason!r} != {exp_reason!r}"
        assert got_term is exp_term, f"for {inp!r}: term {got_term!r} != {exp_term!r}"


async def test_process_pending_does_not_drain_while_awaiting_user():
    """Queued messages must not be dispatched while the session is AWAITING_USER.
    The legacy state mirrors AWAITING_USER as 'idle', so without the v2 check
    the old code would incorrectly dispatch M2 as 'answer-received'."""
    from sessions import state_v2 as sv2

    mgr = _make_manager()
    dispatched: list[str] = []

    async def fake_runner(session_id, message, session, is_retry=False):
        dispatched.append(message)

    mgr.set_agent_runner(fake_runner)
    sid = mgr.create_session(title="AskUser test")
    session = mgr.get(sid)

    # Manually transition to AWAITING_USER (simulates ask_user firing mid-turn).
    from db import models as _db

    _db.add_question(
        sid,
        question="What color?",
        session_title="AskUser test",
        session_type="normal",
        context="",
        urgency="normal",
        question_type="question",
    )
    sv2.transition(session, sv2.SessionStateV2.SCOUTING, "prompt-arrived")
    sv2.transition(session, sv2.SessionStateV2.PROCESSING, "scout-done")
    sv2.transition(session, sv2.SessionStateV2.AWAITING_USER, "ask-user")

    # Simulate a queued message (arrived while session was PROCESSING).
    session.pending_messages.append(("follow-up", ""))

    # _process_pending must NOT dispatch this message — session is AWAITING_USER.
    await mgr._process_pending(session)
    assert len(session.pending_messages) == 1, "_process_pending drained the queue while AWAITING_USER"
    assert dispatched == [], f"Unexpected dispatch(es) while AWAITING_USER: {dispatched}"


def test_paused_reaper_edge_exists_in_transition_table():
    """PAUSED must have a reaper-unstick edge so the reaper can force it to
    IDLE_READY without logging an invariant violation."""
    from sessions import state_v2 as sv2

    assert (sv2.SessionStateV2.PAUSED, "reaper-unstick") in sv2.TRANSITIONS
    assert sv2.TRANSITIONS[(sv2.SessionStateV2.PAUSED, "reaper-unstick")] is sv2.SessionStateV2.IDLE_READY


# ---------------------------------------------------------------------------
# Bug B — FINALIZING reaper must respect has_background_tasks
# ---------------------------------------------------------------------------


def test_finalizing_reaper_respects_background_refs():
    """The FINALIZING reaper must not force-unstick a session while
    post-hooks (e.g. reflect) are still running. _run_post_hooks holds a
    background ref during its execution; the reaper should honour it the
    same way the PROCESSING reaper already does."""
    import time

    from sessions import state_v2 as sv2

    mgr = _make_manager()
    sid = mgr.create_session(title="Finalizing With Hooks")
    session = mgr.get(sid)

    # Drive into FINALIZING via the state machine.
    sv2.transition(session, sv2.SessionStateV2.PROCESSING, "prompt-arrived")
    session.termination_reason = "complete"
    sv2.transition(
        session,
        sv2.SessionStateV2.FINALIZING,
        "loop-complete",
        termination_reason=sv2.TerminationReason.COMPLETE,
    )

    # Simulate post-hooks holding a background ref (as _run_post_hooks does).
    session.add_background_ref()

    # Make the session look idle for longer than the 120s FINALIZING threshold.
    session.last_activity_time = time.time() - 200

    mgr.reap_idle_sessions(max_idle=1800)

    assert sv2._current_state(session) is sv2.SessionStateV2.FINALIZING, (
        "Reaper must not force-unstick a FINALIZING session while "
        "background tasks (post-hooks / reflect) are still running."
    )


def test_finalizing_reaper_fires_when_no_background_refs():
    """When there are no background tasks and FINALIZING has been idle
    for > 120s (genuine stuck), the reaper should force it to IDLE_READY."""
    import time

    from sessions import state_v2 as sv2

    mgr = _make_manager()
    sid = mgr.create_session(title="Truly Stuck FINALIZING")
    session = mgr.get(sid)

    sv2.transition(session, sv2.SessionStateV2.PROCESSING, "prompt-arrived")
    session.termination_reason = "complete"
    sv2.transition(
        session,
        sv2.SessionStateV2.FINALIZING,
        "loop-complete",
        termination_reason=sv2.TerminationReason.COMPLETE,
    )

    # No background ref — genuinely stuck.
    session.last_activity_time = time.time() - 200

    mgr.reap_idle_sessions(max_idle=1800)

    assert sv2._current_state(session) is sv2.SessionStateV2.IDLE_READY, (
        "A genuinely stuck FINALIZING session (no background tasks, idle > 120s) "
        "must be force-unstuck to IDLE_READY by the reaper."
    )


# ---------------------------------------------------------------------------
# Concurrent mutation of the session map
# ---------------------------------------------------------------------------


def test_broadcast_survives_concurrent_session_insert():
    """broadcast() runs on tool threads (ask_user/notify_user) while
    spawn_worker — also on a tool thread — inserts via create_session.
    Iterating the live dict raised "dictionary changed size during
    iteration" and surfaced as a bogus error from ask_user."""
    import threading

    from sessions.state import AgentSession

    mgr = _make_manager()
    # Subscribers make the loop body do real work, widening the window in
    # which a concurrent insert can be observed mid-iteration.
    for i in range(200):
        s = AgentSession(session_id=f"seed{i}")
        s.subscribe()
        mgr._sessions[f"seed{i}"] = s

    errors: list = []
    stop = threading.Event()

    def inserter():
        i = 0
        while not stop.is_set():
            mgr._sessions[f"new{i}"] = AgentSession(session_id=f"new{i}")
            mgr._sessions.pop(f"new{max(0, i - 50)}", None)
            i += 1

    def broadcaster():
        try:
            for _ in range(300):
                mgr.broadcast({"type": "test"})
                mgr.has_active_work()
        except RuntimeError as e:  # pragma: no cover - the bug being fixed
            errors.append(e)

    t = threading.Thread(target=inserter, daemon=True)
    t.start()
    try:
        broadcaster()
    finally:
        stop.set()
        t.join(timeout=5)

    assert not errors, f"session map iteration raced a concurrent insert: {errors}"


def test_snooze_idle_check_survives_concurrent_session_insert(monkeypatch):
    """Same race, reached through SnoozeRunner._is_idle."""
    import threading

    import sessions.manager as manager_mod
    from core.snooze import SnoozeRunner
    from sessions.state import AgentSession

    # _is_idle reads the module singleton, so install ours as the singleton.
    mgr = _make_manager()
    monkeypatch.setattr(manager_mod, "_manager", mgr)
    for i in range(200):
        mgr._sessions[f"seed{i}"] = AgentSession(session_id=f"seed{i}")

    runner = SnoozeRunner()
    errors: list = []
    stop = threading.Event()

    def inserter():
        i = 0
        while not stop.is_set():
            mgr._sessions[f"new{i}"] = AgentSession(session_id=f"new{i}")
            mgr._sessions.pop(f"new{max(0, i - 50)}", None)
            i += 1

    t = threading.Thread(target=inserter, daemon=True)
    t.start()
    try:
        for _ in range(300):
            try:
                runner._is_idle()
            except RuntimeError as e:  # pragma: no cover - the bug being fixed
                errors.append(e)
                break
    finally:
        stop.set()
        t.join(timeout=5)

    assert not errors, f"_is_idle raced a concurrent insert: {errors}"
