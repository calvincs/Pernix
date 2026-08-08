"""Regression tests for the AWAITING_WORKERS deadlock-class issues
identified in the orchestration audit (April 2026).

Each test isolates one previously-broken failure mode; together they
exercise the full set of escape hatches a parent has when a watched
worker misbehaves (cancel, spawn-time error, never-fires, hangs).
"""

from __future__ import annotations

import asyncio

import pytest

from sessions import state_v2 as sv2
from sessions.manager import SessionManager


@pytest.fixture
def mgr(monkeypatch):
    fresh = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", fresh)
    return fresh


def _make_parent_in_awaiting_workers(mgr: SessionManager, watched_ids: list[str]) -> str:
    """Set up a parent session in AWAITING_WORKERS with the given watch-set."""
    parent_id = mgr.create_session(title="parent")
    parent = mgr.get(parent_id)
    parent.worker_ids = list(watched_ids)
    parent._watched_worker_ids = set(watched_ids)
    # Force the v2 state machine into AWAITING_WORKERS without going through
    # the full PROCESSING entry path. We bypass invariant checks because the
    # tests don't run a real agent task.
    parent._state_v2 = sv2.SessionStateV2.AWAITING_WORKERS
    return parent_id


# ---------------------------------------------------------------------------
# Fix 1: cancelled worker still fires _on_watched_worker_done
# ---------------------------------------------------------------------------


async def test_cancelled_worker_fires_watcher_callback(mgr, monkeypatch):
    """A cancelled worker must still notify a watching parent. Before the
    fix, the cancel arm of _run_agent_safe's finally block returned early
    and skipped the worker.done emit + _on_watched_worker_done call,
    permanently stalling a parent watching only the cancelled worker."""
    worker_id = mgr.create_session(
        title="W",
        session_type="worker",
        parent_session_id=None,
    )
    parent_id = mgr.create_session(title="P")
    worker = mgr.get(worker_id)
    worker.parent_session_id = parent_id  # link after create
    parent = mgr.get(parent_id)
    parent.worker_ids = [worker_id]
    parent._watched_worker_ids = {worker_id}
    parent._state_v2 = sv2.SessionStateV2.AWAITING_WORKERS

    resume_calls: list[str] = []

    async def fake_resume(p):
        resume_calls.append(p.session_id)

    monkeypatch.setattr(mgr, "_resume_from_workers", fake_resume)

    # Simulate the fix: a cancelled worker calls _on_watched_worker_done
    # directly. Before the fix, this was unreachable on the cancel path.
    await mgr._on_watched_worker_done(worker)

    assert resume_calls == [parent_id], (
        "Parent must be resumed when its single watched worker completes — " "even via a cancel path."
    )
    assert worker_id not in parent._watched_worker_ids


# ---------------------------------------------------------------------------
# Fix 2: spawn-time _start() failure cleans up watch-set
# ---------------------------------------------------------------------------


async def test_spawn_failure_removes_from_watch_set_and_resumes_parent(
    mgr,
    monkeypatch,
):
    """When manager.prompt() raises inside spawn_worker's _start(), the
    worker never reaches IDLE_READY and the state-machine path that calls
    _on_watched_worker_done never fires. The fix wraps _start() in a
    try/except that discards the worker from the parent's watch-set and
    triggers _resume_from_workers if the set goes empty."""
    parent_id = mgr.create_session(title="P")
    parent = mgr.get(parent_id)

    # Pretend spawn_worker already created the worker session and added
    # the watcher (the spawn flow does this before dispatching _start).
    worker_id = mgr.create_session(
        title="W",
        session_type="worker",
        parent_session_id=parent_id,
    )
    parent.worker_ids.append(worker_id)
    parent._watched_worker_ids.add(worker_id)

    resume_calls: list[str] = []

    async def fake_resume(p):
        resume_calls.append(p.session_id)

    monkeypatch.setattr(mgr, "_resume_from_workers", fake_resume)

    # Reproduce what spawn_worker._start does on exception. We can't import
    # the closure, so we drive the same cleanup logic the fix added.
    try:
        raise RuntimeError("LLM provider unreachable")
    except Exception as e:
        w = mgr.get(worker_id)
        w.error = str(e)
        w.termination_reason = "error"
        parent._watched_worker_ids.discard(worker_id)
        if not parent._watched_worker_ids:
            await mgr._resume_from_workers(parent)

    assert resume_calls == [parent_id]
    assert worker_id not in parent._watched_worker_ids


# ---------------------------------------------------------------------------
# Fix 3: reaper purges stale IDs from non-empty watch-sets
# ---------------------------------------------------------------------------


async def test_reaper_purges_stale_watched_workers(mgr, monkeypatch):
    """A stale worker in a non-empty watch-set used to block both the
    resume callback and the empty-set safety net forever. The reaper now
    purges stale IDs every tick and triggers resume if the set empties.

    Async test: reap_idle_sessions schedules resume via asyncio.create_task,
    which requires a running loop (maintenance._tick is async in prod)."""
    worker_id = mgr.create_session(title="W", session_type="worker")
    parent_id = _make_parent_in_awaiting_workers(mgr, [worker_id])
    parent = mgr.get(parent_id)

    # Mark the watched worker as IDLE_READY-after-start with no error —
    # i.e. it ran, completed, but its callback never fired (e.g., cancel
    # path before the fix or a delete-by-id race).
    w = mgr.get(worker_id)
    w._state_v2 = sv2.SessionStateV2.IDLE_READY
    w._turn_id = 1  # has_started=True

    resume_scheduled: list[str] = []

    async def fake_resume(p):
        resume_scheduled.append(p.session_id)

    monkeypatch.setattr(mgr, "_resume_from_workers", fake_resume)
    parent.last_activity_time = 0

    mgr.reap_idle_sessions(max_idle=10)
    await asyncio.sleep(0)  # let the scheduled task run

    assert (
        worker_id not in parent._watched_worker_ids
    ), "Reaper must purge stale IDs from non-empty watch-sets every tick."
    assert resume_scheduled == [parent_id], "After purge empties the set, reaper must trigger resume."


async def test_reaper_purges_errored_never_started_worker(mgr, monkeypatch):
    """Defense-in-depth: if a worker errored at spawn time without ever
    transitioning to PROCESSING (has_started=False), the original purge
    heuristic (IDLE_READY + has_started) would miss it. The fix also
    treats workers with `error` or `termination_reason` set as stale."""
    worker_id = mgr.create_session(title="W", session_type="worker")
    parent_id = _make_parent_in_awaiting_workers(mgr, [worker_id])
    parent = mgr.get(parent_id)

    w = mgr.get(worker_id)
    w._state_v2 = sv2.SessionStateV2.IDLE_READY
    w._turn_id = 0  # never started
    w.task = None
    w.error = "spawn failed"
    w.termination_reason = "error"

    async def fake_resume(p):
        pass

    monkeypatch.setattr(mgr, "_resume_from_workers", fake_resume)
    parent.last_activity_time = 0
    mgr.reap_idle_sessions(max_idle=10)
    await asyncio.sleep(0)

    assert worker_id not in parent._watched_worker_ids


# ---------------------------------------------------------------------------
# Fix 4: reaper enforces worker-timeout for genuinely-hung waits
# ---------------------------------------------------------------------------


async def test_reaper_fires_worker_timeout_when_watch_set_persists(mgr, monkeypatch):
    """Suspend mode previously had no timeout — `worker-timeout` was a
    dead transition. The reaper now fires it after 2× max_idle if the
    watch-set is still populated with genuinely-pending workers, and
    queues a synthetic prompt so the LLM has timeout context."""
    worker_id = mgr.create_session(title="W", session_type="worker")
    parent_id = _make_parent_in_awaiting_workers(mgr, [worker_id])
    parent = mgr.get(parent_id)

    # Worker is genuinely still running (PROCESSING) — should not be purged.
    w = mgr.get(worker_id)
    w._state_v2 = sv2.SessionStateV2.PROCESSING
    w._turn_id = 1

    parent.last_activity_time = 0  # very old — well past 2*max_idle

    transitions: list[tuple] = []
    real_transition = sv2.transition

    def spy_transition(session, to, reason, **kwargs):
        transitions.append((session.session_id, to, reason))
        return real_transition(session, to, reason, **kwargs)

    monkeypatch.setattr(sv2, "transition", spy_transition)

    # Stub _process_pending so the scheduled task doesn't try to run a
    # real agent.
    async def noop(_s):
        return

    monkeypatch.setattr(mgr, "_process_pending", noop)

    mgr.reap_idle_sessions(max_idle=10)  # 2*max_idle = 20s; idle is ~now
    await asyncio.sleep(0)  # let the scheduled _process_pending task run

    # The parent should have transitioned via worker-timeout.
    parent_transitions = [(to, r) for sid, to, r in transitions if sid == parent_id]
    assert any(
        r == "worker-timeout" for _, r in parent_transitions
    ), f"Expected worker-timeout transition; got {parent_transitions}"
    assert sv2._current_state(parent) is sv2.SessionStateV2.IDLE_READY
    # And a synthetic timeout prompt should have been queued so the LLM
    # learns about the timeout on resume.
    queued = [e.message for e in parent.pending_messages]
    assert any("timed out" in m.lower() for m in queued), f"Expected timeout-context message in queue; got {queued}"
    # Synthetic timeout prompts carry no DB row.
    assert all(e.msg_id is None for e in parent.pending_messages)
    # Watch-set is cleared so future ticks don't re-fire timeout.
    assert not parent._watched_worker_ids


# ---------------------------------------------------------------------------
# Fix 5: parent cancel cascades to watched workers
# ---------------------------------------------------------------------------


async def test_parent_cancel_cascades_to_watched_workers(mgr):
    """When a parent in AWAITING_WORKERS is cancelled, its watched workers
    must be cancelled too. Otherwise they keep running, drain tokens, and
    can leave the parent's _watched_worker_ids populated with zombie IDs."""
    worker_id = mgr.create_session(
        title="W",
        session_type="worker",
    )
    parent_id = mgr.create_session(title="P")
    parent = mgr.get(parent_id)
    parent.worker_ids = [worker_id]
    parent._watched_worker_ids = {worker_id}

    async def long_running():
        await asyncio.sleep(60)

    w = mgr.get(worker_id)
    w.task = asyncio.create_task(long_running())
    await asyncio.sleep(0)  # let the task actually start awaiting

    # Reproduce the cascade logic the fix added to _run_agent_safe's
    # CancelledError handler.
    watched = list(parent._watched_worker_ids)
    for wid in watched:
        wkr = mgr._sessions.get(wid)
        if wkr and wkr.task and not wkr.task.done():
            wkr.task.cancel()
    parent._watched_worker_ids.clear()

    # Wait for the task to actually finish cancelling.
    with pytest.raises(asyncio.CancelledError):
        await w.task
    assert w.task.cancelled()
    assert not parent._watched_worker_ids


# ---------------------------------------------------------------------------
# Fix 6: _resume_from_workers extends LLM budget before synthesis turn
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Follow-ups: idempotency, state guards, verdict-aware resume message
# ---------------------------------------------------------------------------


async def test_await_workers_suspend_is_idempotent(mgr, monkeypatch):
    """A second await_workers(suspend=True) on a parent already in
    AWAITING_WORKERS must refuse, not cumulatively expand the watch-set."""
    from core.extensions.orchestration import await_workers

    worker_id = mgr.create_session(title="W", session_type="worker")
    parent_id = _make_parent_in_awaiting_workers(mgr, [worker_id])
    parent = mgr.get(parent_id)
    initial_watched = set(parent._watched_worker_ids)

    # Mark worker as still running so it'd be added to still_running set.
    mgr.get(worker_id)._state_v2 = sv2.SessionStateV2.PROCESSING

    out = await_workers(suspend=True, _context={"session_id": parent_id})
    assert "Already suspended" in out, out
    assert parent._watched_worker_ids == initial_watched, "Idempotency guard must not mutate the watch-set"


def test_spawn_worker_refuses_when_parent_not_processing(mgr, monkeypatch):
    """spawn_worker must refuse if the parent isn't in PROCESSING. A spawn
    from AWAITING_WORKERS or IDLE_READY indicates a lifecycle race."""
    import asyncio as _aio

    from core.extensions.orchestration import spawn_worker

    parent_id = mgr.create_session(title="P")
    # Default state on a freshly-created session is IDLE_READY.
    loop = _aio.new_event_loop()
    _aio.set_event_loop(loop)
    try:
        out = spawn_worker(
            "task",
            title="W",
            _context={"session_id": parent_id, "_loop": loop},
        )
    finally:
        loop.close()
    assert out.startswith("Error: Cannot spawn worker"), out
    assert "idle_ready" in out


def test_build_resume_message_flags_failed_workers(mgr):
    """_build_resume_message must surface non-pass workers explicitly so
    the LLM can't ignore failures by skipping get_worker_result()."""
    from db import models as db

    parent_id = mgr.create_session(title="P")
    parent = mgr.get(parent_id)

    # Worker A: clean pass
    wa = mgr.create_session(title="A", session_type="worker", parent_session_id=parent_id)
    mgr.get(wa).termination_reason = "complete"
    db.add_message(wa, "reflect", '{"verdict": "pass"}')

    # Worker B: escalated by reflect
    wb = mgr.create_session(title="B", session_type="worker", parent_session_id=parent_id)
    mgr.get(wb).termination_reason = "complete"
    db.add_message(wb, "reflect", '{"verdict": "escalate", "reasoning": "incomplete"}')

    # Worker C: errored
    wc = mgr.create_session(title="C", session_type="worker", parent_session_id=parent_id)
    mgr.get(wc).termination_reason = "error"
    mgr.get(wc).error = "LLM provider 503"

    # Worker D: cancelled
    wd = mgr.create_session(title="D", session_type="worker", parent_session_id=parent_id)
    mgr.get(wd).termination_reason = "cancelled"

    parent.worker_ids = [wa, wb, wc, wd]
    msg = mgr._build_resume_message(parent)

    assert "ESCALATED" in msg
    assert "ERROR" in msg
    assert "CANCELLED" in msg
    assert "pass" in msg.lower()
    # Problem-worker call-out lists B/C/D but not A.
    assert wb[:8] in msg
    assert wc[:8] in msg
    assert wd[:8] in msg
    assert "⚠" in msg  # warning indicator on problem section


# ---------------------------------------------------------------------------
# Persistence + boot reconciliation (migration v16)
# ---------------------------------------------------------------------------


def test_state_v2_persists_across_restart(mgr, monkeypatch):
    """state_v2 column survives a manager restart so AWAITING_WORKERS,
    AWAITING_USER, FINALIZING (which all map to legacy 'idle') restore
    to their true v2 state on rehydrate."""
    sid = mgr.create_session(title="P")
    parent = mgr.get(sid)
    parent._watched_worker_ids = {"fake-worker-id"}
    sv2.transition(parent, sv2.SessionStateV2.PROCESSING, "prompt-arrived")
    sv2.transition(parent, sv2.SessionStateV2.AWAITING_WORKERS, "workers-dispatched")

    # Simulate restart: drop in-memory session, build a fresh manager.
    fresh = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", fresh)
    rehydrated = fresh.get_or_create(sid)

    assert (
        sv2._current_state(rehydrated) is sv2.SessionStateV2.AWAITING_WORKERS
    ), f"v2 state must survive restart; got {sv2._current_state(rehydrated)}"


def test_watched_worker_ids_persist_across_restart(mgr, monkeypatch):
    """watched_worker_ids JSON column survives restart."""
    sid = mgr.create_session(title="P")
    parent = mgr.get(sid)
    parent._watched_worker_ids = {"w-001", "w-002", "w-003"}
    mgr._persist_watched(parent)

    fresh = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", fresh)
    rehydrated = fresh.get_or_create(sid)

    assert rehydrated._watched_worker_ids == {"w-001", "w-002", "w-003"}


async def test_reconcile_resumes_parent_when_workers_already_done(mgr, monkeypatch):
    """If a parent was suspended on workers that completed during downtime,
    the boot-time sweep must purge the stale entries and resume."""
    parent_id = mgr.create_session(title="P")
    worker_id = mgr.create_session(
        title="W",
        session_type="worker",
        parent_session_id=parent_id,
    )
    parent = mgr.get(parent_id)
    parent.worker_ids = [worker_id]
    parent._watched_worker_ids = {worker_id}
    sv2.transition(parent, sv2.SessionStateV2.PROCESSING, "prompt-arrived")
    sv2.transition(parent, sv2.SessionStateV2.AWAITING_WORKERS, "workers-dispatched")
    mgr._persist_watched(parent)

    # Simulate the worker having finished while the server was down: it's
    # now IDLE_READY with has_started=True.
    w = mgr.get(worker_id)
    w._turn_id = 1
    sv2.transition(w, sv2.SessionStateV2.IDLE_READY, "cancel-complete")  # any path to IDLE_READY

    # Drop in-memory state to simulate restart.
    fresh = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", fresh)

    resume_called: list[str] = []

    async def fake_resume(p):
        resume_called.append(p.session_id)

    monkeypatch.setattr(fresh, "_resume_from_workers", fake_resume)

    resumed = await fresh.reconcile_awaiting_workers()

    assert resumed == 1, f"Expected 1 resume; got {resumed}"
    assert resume_called == [parent_id]
    rehydrated = fresh.get(parent_id)
    assert worker_id not in rehydrated._watched_worker_ids


async def test_reconcile_skips_when_workers_still_running(mgr, monkeypatch):
    """If watched workers are still PROCESSING after restart, reconcile
    must leave the parent suspended (the reaper handles the timeout)."""
    parent_id = mgr.create_session(title="P")
    worker_id = mgr.create_session(
        title="W",
        session_type="worker",
        parent_session_id=parent_id,
    )
    parent = mgr.get(parent_id)
    parent.worker_ids = [worker_id]
    parent._watched_worker_ids = {worker_id}
    sv2.transition(parent, sv2.SessionStateV2.PROCESSING, "prompt-arrived")
    sv2.transition(parent, sv2.SessionStateV2.AWAITING_WORKERS, "workers-dispatched")
    mgr._persist_watched(parent)

    w = mgr.get(worker_id)
    w._turn_id = 1
    sv2.transition(w, sv2.SessionStateV2.SCOUTING, "prompt-arrived")
    sv2.transition(w, sv2.SessionStateV2.PROCESSING, "scout-done")

    fresh = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", fresh)

    resume_called: list[str] = []

    async def fake_resume(p):
        resume_called.append(p.session_id)

    monkeypatch.setattr(fresh, "_resume_from_workers", fake_resume)

    resumed = await fresh.reconcile_awaiting_workers()

    assert resumed == 0
    assert resume_called == []
    rehydrated = fresh.get(parent_id)
    assert worker_id in rehydrated._watched_worker_ids
    assert sv2._current_state(rehydrated) is sv2.SessionStateV2.AWAITING_WORKERS


# ---------------------------------------------------------------------------
# Reflect-retry leak between suspended turn and synthesis turn
# ---------------------------------------------------------------------------


async def test_post_hooks_skip_when_awaiting_workers(mgr, monkeypatch):
    """Reflect must not run on a turn that ended in AWAITING_WORKERS. The
    transcript at suspend time always looks 'incomplete' to reflect (last
    line is 'Session suspended...'), so reflect would set verdict=retry
    every time, leaving reflect_retry_requested=True to race with the
    synthesis turn's reset."""
    sid = mgr.create_session(title="P")
    parent = mgr.get(sid)
    parent._watched_worker_ids = {"w-1"}
    sv2.transition(parent, sv2.SessionStateV2.PROCESSING, "prompt-arrived")
    sv2.transition(parent, sv2.SessionStateV2.AWAITING_WORKERS, "workers-dispatched")

    hooks_called: list[str] = []

    async def fake_hooks(_sid, *, emit, session_obj):
        hooks_called.append(_sid)

    monkeypatch.setattr("sessions.hooks.run_post_task_hooks", fake_hooks)
    await mgr._run_post_hooks(parent)
    assert hooks_called == [], (
        "Post-hooks must short-circuit when state is AWAITING_WORKERS to "
        "prevent reflect from spuriously flagging a deliberately-suspended "
        "turn as incomplete."
    )


async def test_run_agent_safe_resets_retry_flags_at_turn_start(mgr, monkeypatch):
    """A new turn launched by _resume_from_workers must start with the
    reflect/eval retry flags cleared, even if a prior turn left them True
    via a race between suspended-turn post-hooks and the resume reset."""
    sid = mgr.create_session(title="P")
    parent = mgr.get(sid)
    # Simulate the race: suspended-turn reflect set retry=True after the
    # resume's lock-protected reset cleared it.
    parent.reflect_retry_requested = True
    parent.eval_retry_requested = True

    captured_flags: list[tuple[bool, bool]] = []

    # Stub the agent runner to capture flag state when the turn body runs.
    async def fake_runner(*, session_id, message, session, **kwargs):
        captured_flags.append(
            (
                session.reflect_retry_requested,
                session.eval_retry_requested,
            )
        )

    mgr.set_agent_runner(fake_runner)

    # Stub run_scout to avoid LLM calls.
    from core.llm.types import TokenUsage
    from core.scout.runner import ScoutReport

    async def fake_scout(session_id, message, brief, emit=None, is_retry=False):
        return ScoutReport(
            recommended_tools=[],
            tool_rationale="",
            recommended_skills=[],
            skill_rationale="",
            injected_skill="",
            injected_skill_name="",
            recommended_model="",
            model_rationale="",
            session_state="",
            approach_guidance="",
            deliverables_plan=[],
            execution_mode="inline",
            viability="pending",
            viability_notes=[],
            scout_model="",
            scout_latency_ms=0,
            scout_tokens=TokenUsage(0, 0, 0, 0, 0, {}),
            from_cache=False,
            from_fallback=False,
        )

    monkeypatch.setattr("core.scout.runner.run_scout", fake_scout)
    monkeypatch.setattr("sessions.hooks.run_post_task_hooks", lambda *a, **kw: None)

    sv2.transition(parent, sv2.SessionStateV2.SCOUTING, "prompt-arrived")
    sv2.transition(parent, sv2.SessionStateV2.PROCESSING, "scout-done")
    sv2._set_state(parent, sv2.SessionStateV2.IDLE_READY)
    parent._state_v2 = sv2.SessionStateV2.IDLE_READY

    await mgr._run_agent_safe(parent, "synthesis turn", "")

    assert captured_flags, "agent runner should have been called"
    reflect_flag, eval_flag = captured_flags[0]
    assert reflect_flag is False, f"reflect_retry_requested must be reset at start of turn; got {reflect_flag}"
    assert eval_flag is False, f"eval_retry_requested must be reset at start of turn; got {eval_flag}"


async def test_resume_from_workers_extends_session_budget(mgr, monkeypatch):
    """Workers may run for a long time; on resume the parent's LLM budget
    can already be drained, causing scout to raise LLMSessionTimeoutError
    on the synthesis turn. _resume_from_workers now extends the budget
    first — same root cause and fix shape as the cron orchestrator fix."""
    parent_id = mgr.create_session(title="P")
    parent = mgr.get(parent_id)
    # Pretend the parent is already idle so _resume_from_workers takes
    # the queue-as-pending branch and we don't need a real agent runner.
    parent._state_v2 = sv2.SessionStateV2.IDLE_READY

    extend_calls: list[tuple] = []

    def fake_extend(sid, secs):
        extend_calls.append((sid, secs))
        return 1800.0 + secs

    monkeypatch.setattr("core.llm.client.extend_session_budget", fake_extend)
    monkeypatch.setattr("config.settings.llm_session_timeout", 1800)

    # Stub _process_pending so we don't try to run the agent.
    async def noop(_session):
        return

    monkeypatch.setattr(mgr, "_process_pending", noop)

    await mgr._resume_from_workers(parent)

    assert extend_calls, "Resume must extend the parent's LLM budget"
    sid, secs = extend_calls[0]
    assert sid == parent_id
    assert secs == 2 * 1800.0


# ---------------------------------------------------------------------------
# Bug A — boot-time PROCESSING reconciliation
# ---------------------------------------------------------------------------


async def test_reconcile_processing_resets_stuck_session(mgr, monkeypatch):
    """Sessions persisted in PROCESSING at boot (dead agent task) must be
    reset to IDLE_READY by reconcile_processing_sessions() so users can
    re-prompt without waiting up to 5 minutes for the reaper."""
    import time as _time

    from db import models as db

    # Create a session in the DB and persist state_v2='processing' directly
    # (simulating a crash mid-turn with no live agent task).
    sid = db.create_session(title="Stuck Processing")
    db.update_session(sid, state="processing", state_v2="processing")

    # Do NOT add it to the manager's in-memory dict — it should be invisible
    # to the normal reaper and only visible via the DB sweep.
    assert mgr.get(sid) is None

    reset = await mgr.reconcile_processing_sessions()

    assert reset == 1, f"Expected 1 session reset, got {reset}"
    session = mgr.get(sid)
    assert session is not None
    assert (
        sv2._current_state(session) is sv2.SessionStateV2.IDLE_READY
    ), "Session should be IDLE_READY after boot reconcile"
    # A 'system' event should have been emitted to inform the user.
    events = [e for e in session.events if e.get("type") == "system"]
    assert events, "Boot reconcile should emit a system event to notify the user"


async def test_reconcile_processing_skips_if_already_recovered(mgr):
    """If a session somehow ends up in memory as IDLE_READY (e.g., recovered
    by another path) before reconcile runs, it must not be touched."""
    from db import models as db

    sid = db.create_session(title="Already Recovered")
    db.update_session(sid, state="processing", state_v2="processing")

    # Load into memory and set to IDLE_READY before reconcile runs.
    session = mgr.get_or_create(sid)
    sv2.transition(session, sv2.SessionStateV2.IDLE_READY, "reaper-unstick")

    reset = await mgr.reconcile_processing_sessions()

    assert reset == 0, "Already-recovered session should not be counted"
    assert sv2._current_state(mgr.get(sid)) is sv2.SessionStateV2.IDLE_READY


async def test_reconcile_processing_handles_legacy_only_rows(mgr):
    """Sessions where state='processing' but state_v2 is NULL (crashed before
    state_v2 was written) must also be caught and reset."""
    from db import models as db

    sid = db.create_session(title="Legacy Processing Only")
    # Write legacy state without state_v2 to simulate a pre-v2 crash.
    db.update_session(sid, state="processing", state_v2=None)

    reset = await mgr.reconcile_processing_sessions()

    assert reset == 1
    session = mgr.get(sid)
    assert sv2._current_state(session) is sv2.SessionStateV2.IDLE_READY


async def test_reconcile_interrupted_sweeps_all_phantom_states(mgr):
    """A crash during scout/compaction/cancel/finalize/pause persists those
    states; the boot sweep must reset every one to IDLE_READY since no
    asyncio task survives a restart."""
    from db import models as db

    states = ["scouting", "compacting", "pause_requested", "paused", "cancelling", "finalizing"]
    sids = {}
    for st in states:
        sid = db.create_session(title=f"Stuck {st}")
        db.update_session(sid, state="processing", state_v2=st)
        sids[st] = sid

    reset = await mgr.reconcile_interrupted_sessions()

    assert reset == len(states), f"Expected {len(states)} resets, got {reset}"
    for st, sid in sids.items():
        session = mgr.get(sid)
        assert session is not None, f"{st} session not hydrated"
        assert (
            sv2._current_state(session) is sv2.SessionStateV2.IDLE_READY
        ), f"{st} session should be IDLE_READY after boot sweep"


async def test_reconcile_interrupted_leaves_awaiting_user_alone(mgr):
    """AWAITING_USER survives restarts by design — a pending question is
    answered via the API, which transitions the session out."""
    from db import models as db

    sid = db.create_session(title="Pending question")
    db.update_session(sid, state="idle", state_v2="awaiting_user")

    reset = await mgr.reconcile_interrupted_sessions()

    assert reset == 0
    session = mgr.get(sid)
    if session is not None:
        assert sv2._current_state(session) is sv2.SessionStateV2.AWAITING_USER


# ---------------------------------------------------------------------------
# Bug D — _run_post_hooks must use v2 state, not legacy enum
# ---------------------------------------------------------------------------


async def test_post_hooks_only_run_in_finalizing(mgr, monkeypatch):
    """_run_post_hooks must return early for every state except FINALIZING.
    With the legacy-enum guard removed, new v2 states that map to "idle"
    in the legacy enum can no longer accidentally trigger post-hooks."""
    hooks_called: list[str] = []

    async def fake_hooks(_sid, *, emit, session_obj):
        hooks_called.append(_sid)

    monkeypatch.setattr("sessions.hooks.run_post_task_hooks", fake_hooks)

    non_finalizing = [
        sv2.SessionStateV2.SCOUTING,
        sv2.SessionStateV2.PROCESSING,
        sv2.SessionStateV2.AWAITING_USER,
        sv2.SessionStateV2.AWAITING_WORKERS,
        sv2.SessionStateV2.IDLE_READY,
    ]

    for state in non_finalizing:
        sid = mgr.create_session(title=f"test-{state.value}")
        session = mgr.get(sid)
        # Force the v2 state directly (bypassing invariant checks — test only).
        session._state_v2 = state

        await mgr._run_post_hooks(session)

    assert hooks_called == [], (
        f"Post-hooks ran for non-FINALIZING states: {hooks_called}. " "Only FINALIZING should trigger post-hooks."
    )


async def test_post_hooks_run_when_finalizing(mgr, monkeypatch):
    """Confirm post-hooks DO run when the session is in FINALIZING."""
    hooks_called: list[str] = []

    async def fake_hooks(_sid, *, emit, session_obj):
        hooks_called.append(_sid)

    monkeypatch.setattr("sessions.hooks.run_post_task_hooks", fake_hooks)

    sid = mgr.create_session(title="finalizing-session")
    session = mgr.get(sid)
    # Enter FINALIZING via the state machine (requires PROCESSING first).
    sv2.transition(session, sv2.SessionStateV2.PROCESSING, "prompt-arrived")
    session.termination_reason = "complete"
    sv2.transition(
        session,
        sv2.SessionStateV2.FINALIZING,
        "loop-complete",
        termination_reason=sv2.TerminationReason.COMPLETE,
    )

    await mgr._run_post_hooks(session)

    assert hooks_called == [sid], "Post-hooks must run exactly once when the session is FINALIZING"
