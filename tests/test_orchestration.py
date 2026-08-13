"""Tests for core/extensions/orchestration — worker lifecycle helpers.

Focus: get_worker_result's quality gate, driven by the worker's latest
reflect verdict. Reflect is the authoritative trust signal — when reflect
didn't run, ran and returned escalate/retry, or the worker was cancelled
before reflect could fire, the output must be wrapped in a clear header.
"""

from __future__ import annotations

import json as _json

import pytest

from core.extensions.orchestration import get_worker_result
from sessions.manager import SessionManager, get_manager


@pytest.fixture
def mgr(monkeypatch):
    """Install a fresh manager as the module singleton so get_worker_result's
    internal `from sessions.manager import get_manager` picks it up."""
    fresh = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", fresh)
    return fresh


def _make_worker(
    mgr: SessionManager,
    last_content: str,
    termination_reason: str | None,
    reflect_verdict: str | None = None,
    reflect_reason: str = "",
):
    from db import models as db

    parent_sid = mgr.create_session(title="P")
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
                    "reasoning": reflect_reason or f"test: {reflect_verdict}",
                }
            ),
        )
    w = mgr.get(worker_sid)
    w.termination_reason = termination_reason
    return worker_sid


def test_get_worker_result_reflect_pass_returns_clean(mgr):
    """Reflect verdict=pass → no gating header, content returned as-is."""
    wid = _make_worker(mgr, "Done. Here is the result.", "complete", reflect_verdict="pass")
    out = get_worker_result(wid)
    assert not out.startswith("#")
    assert "Done" in out


def test_get_worker_result_reflect_escalate_wrapped(mgr):
    """Reflect verdict=escalate → ESCALATED header with reasoning + hint."""
    wid = _make_worker(
        mgr,
        "I'll research this systematically.",
        "complete",
        reflect_verdict="escalate",
        reflect_reason="final response is incomplete; only preamble",
    )
    out = get_worker_result(wid)
    assert out.startswith("# ESCALATED")
    assert "only preamble" in out
    assert "get_worker_transcript" in out  # hint to the parent


def test_get_worker_result_reflect_retry_wrapped(mgr):
    """Reflect verdict=retry (retries exhausted) → UNVERIFIED header."""
    wid = _make_worker(mgr, "partial progress", "complete", reflect_verdict="retry")
    out = get_worker_result(wid)
    assert out.startswith("# UNVERIFIED")
    assert "retry" in out


def test_get_worker_result_no_reflect_unverified(mgr):
    """No reflect row → UNVERIFIED (quality not gated)."""
    wid = _make_worker(mgr, "some output", "complete")  # no reflect verdict
    out = get_worker_result(wid)
    assert out.startswith("# UNVERIFIED")


def test_get_worker_result_cancelled_no_reflect(mgr):
    """Cancelled worker → CANCELLED header (reflect never had a chance)."""
    wid = _make_worker(mgr, "was working on it", "cancelled")
    out = get_worker_result(wid)
    assert out.startswith("# CANCELLED")


def test_get_worker_result_error_no_reflect(mgr):
    """Errored worker → INCOMPLETE header."""
    wid = _make_worker(mgr, "got this far", "error")
    out = get_worker_result(wid)
    assert out.startswith("# INCOMPLETE")


def test_get_worker_result_prefers_stamped_summary_with_marker(mgr):
    """If the stamped summary starts with a # marker, trust it as-is
    (the marker already encodes the trust state — don't double-gate)."""
    from pathlib import Path

    from config import settings as _s

    wid = _make_worker(mgr, "fallback that should not be read", "round_ceiling")
    workspace = Path(_s.workspace_dir)
    workspace.mkdir(parents=True, exist_ok=True)
    summary = workspace / f".worker_{wid[:12]}_summary.md"
    summary.write_text("# AUTO-STAMPED (reflect=pass)\nbody here")
    out = get_worker_result(wid)
    assert out.startswith("# AUTO-STAMPED")
    assert "body here" in out


def test_get_worker_result_stamped_summary_without_marker_gets_gated(mgr):
    """A raw summary file without a marker still goes through the gate."""
    from pathlib import Path

    from config import settings as _s

    wid = _make_worker(mgr, "ignored", "complete", reflect_verdict="escalate")
    workspace = Path(_s.workspace_dir)
    workspace.mkdir(parents=True, exist_ok=True)
    summary = workspace / f".worker_{wid[:12]}_summary.md"
    summary.write_text("raw body without marker")
    out = get_worker_result(wid)
    assert out.startswith("# ESCALATED")
    assert "raw body without marker" in out


# ---------------------------------------------------------------------------
# await_workers stale-detection: only PROCESSING/SCOUTING workers can stall
# ---------------------------------------------------------------------------
# Regression for workflow run 024c370f (2026-04-26): the crawl-subs worker
# entered FINALIZING to run reflect (a single ~60-180s LLM call that doesn't
# bump last_activity_time). With the prior 120s stale_threshold applying to
# every non-IDLE_READY state, await_workers returned a "stalled" warning
# mid-reflect; the orchestrator then finalized the step before the post-hook
# could write its verdict, the manifest was stamped failed, and the entire
# the run short-circuited even though the worker was about to verdict 'pass'.


def _make_worker_in_state(mgr, parent_id: str, *, v2_state, idle_seconds: float):
    """Spawn a worker, attach to parent.worker_ids, force its v2 state, and
    backdate last_activity_time so idle_seconds reads as `idle_seconds`."""
    import time as _time

    from sessions.state_v2 import SessionStateV2 as _S
    from sessions.state_v2 import _set_state

    wid = mgr.create_session(
        title="W",
        session_type="worker",
        parent_session_id=parent_id,
    )
    parent = mgr.get(parent_id)
    parent.worker_ids.append(wid)
    w = mgr.get(wid)
    _set_state(w, v2_state)
    # Simulate "has_started": the worker has been through scout already.
    w._turn_id = 1
    w.last_activity_time = _time.time() - idle_seconds
    # Sanity: the helper above must have actually moved the v2 field.
    assert w._state_v2 is v2_state, (w._state_v2, v2_state)
    return wid


def _patch_no_wait(monkeypatch):
    """Make await_workers's polling loop advance time without sleeping, so
    tests that need it to hit max_wait don't take 30 minutes wall-clock."""
    import core.extensions.orchestration as orch

    fake_clock = [orch.time.time()]

    def fast_time():
        return fake_clock[0]

    def fast_sleep(secs):
        # Skip past the entire poll interval so the next iteration's
        # time.time() check exits the while loop quickly.
        fake_clock[0] += max(secs, 60)

    monkeypatch.setattr(orch.time, "time", fast_time)
    monkeypatch.setattr(orch.time, "sleep", fast_sleep)


def test_await_workers_processing_with_idle_marks_stalled(mgr, monkeypatch):
    """Sanity: a worker in PROCESSING with idle > stale_threshold IS stalled."""
    from core.extensions.orchestration import await_workers
    from sessions.state_v2 import SessionStateV2 as S

    parent_id = mgr.create_session(title="P")
    _make_worker_in_state(mgr, parent_id, v2_state=S.PROCESSING, idle_seconds=200)

    out = await_workers(
        stale_threshold=10,
        _context={"session_id": parent_id},
    )
    assert "appear stalled" in out, out


def test_await_workers_finalizing_does_not_count_as_stalled(mgr, monkeypatch):
    """Regression: a worker in FINALIZING running its reflect post-hook is
    NOT stalled even when last_activity_time is older than stale_threshold.
    Reflect is a bounded LLM call that doesn't bump activity but is doing
    real work — await_workers must not bail on it.
    """
    from core.extensions.orchestration import await_workers
    from sessions.state_v2 import SessionStateV2 as S

    parent_id = mgr.create_session(title="P")
    _make_worker_in_state(mgr, parent_id, v2_state=S.FINALIZING, idle_seconds=200)
    _patch_no_wait(monkeypatch)

    out = await_workers(
        stale_threshold=10,
        _context={"session_id": parent_id},
    )
    # Old (buggy) behavior would return "appear stalled" almost immediately.
    # New behavior: the FINALIZING worker is excluded from stale detection,
    # so the polling loop runs out the clock and we see a Timeout (or done)
    # response instead.
    assert "appear stalled" not in out, "FINALIZING worker should not be classified as stalled — " f"got: {out!r}"


def test_await_workers_compacting_does_not_count_as_stalled(mgr, monkeypatch):
    """Sibling case: COMPACTING is also a bounded LLM operation that doesn't
    bump last_activity_time. Same exclusion applies."""
    from core.extensions.orchestration import await_workers
    from sessions.state_v2 import SessionStateV2 as S

    parent_id = mgr.create_session(title="P")
    _make_worker_in_state(mgr, parent_id, v2_state=S.COMPACTING, idle_seconds=200)
    _patch_no_wait(monkeypatch)

    out = await_workers(
        stale_threshold=10,
        _context={"session_id": parent_id},
    )
    assert "appear stalled" not in out, out


def test_await_workers_one_stalled_one_healthy_keeps_waiting(mgr, monkeypatch):
    """Regression for workflow run 1ec11d2b (2026-04-27 ai-tech-daily-brief):
    one stalled wave-mate caused await_workers to return early, after which
    the orchestrator finalized ALL eligible steps. That triggered redundant
    recovery-reflect runs (~100s each) on workers that were still actively
    producing output, and ultimately tore them down mid-flight.

    New behavior: if some workers are stalled but at least one peer is still
    healthy, keep waiting for the healthy ones to finish. Only abandon the
    wait when every pending worker is stalled.

    We assert via the log warning ("continuing to wait on N healthy peer(s)")
    rather than the return string — the polling loop's fake clock would
    eventually push the healthy peer over threshold too, which is fine in
    production (it really would be stalled) but not the behavior under test.
    """
    import logging as _logging

    import core.extensions.orchestration as orch
    from sessions.state_v2 import SessionStateV2 as S

    parent_id = mgr.create_session(title="P")
    _make_worker_in_state(mgr, parent_id, v2_state=S.PROCESSING, idle_seconds=200)
    _make_worker_in_state(mgr, parent_id, v2_state=S.PROCESSING, idle_seconds=0)

    # Capture warning logs from the orchestrator
    log_records: list[_logging.LogRecord] = []
    handler = _logging.Handler()
    handler.emit = lambda r: log_records.append(r)
    orch.logger.addHandler(handler)

    # Make the polling loop exit after one iteration so we can inspect
    # the first-iteration decision. Raising from sleep is the cleanest way
    # to break out without modifying production code.
    class _StopAfterOne(Exception):
        pass

    iter_count = [0]

    def fast_sleep(_secs):
        iter_count[0] += 1
        if iter_count[0] >= 1:
            raise _StopAfterOne()

    monkeypatch.setattr(orch.time, "sleep", fast_sleep)

    try:
        with pytest.raises(_StopAfterOne):
            orch.await_workers(
                stale_threshold=10,
                _context={"session_id": parent_id},
            )
    finally:
        orch.logger.removeHandler(handler)

    # The early-return path would NEVER reach the sleep — if we got there,
    # await_workers chose to keep polling instead of bailing on the stalled
    # worker. That's the fix.
    msgs = [r.getMessage() for r in log_records]
    assert any(
        "continuing to wait on 1 healthy peer" in m for m in msgs
    ), f"expected stalled-with-healthy-peer warning; saw: {msgs}"


def test_await_workers_all_stalled_returns_warning(mgr, monkeypatch):
    """Counterpart to the above: when EVERY pending worker is stalled there's
    nothing useful to wait for, so the early return is correct.
    """
    from core.extensions.orchestration import await_workers
    from sessions.state_v2 import SessionStateV2 as S

    parent_id = mgr.create_session(title="P")
    _make_worker_in_state(mgr, parent_id, v2_state=S.PROCESSING, idle_seconds=200)
    _make_worker_in_state(mgr, parent_id, v2_state=S.PROCESSING, idle_seconds=200)

    out = await_workers(
        stale_threshold=10,
        _context={"session_id": parent_id},
    )
    assert "appear stalled" in out, out


def test_await_workers_treats_awaiting_user_as_stale(mgr, monkeypatch):
    """A worker that calls ask_user enters AWAITING_USER. There is
    no human in the loop for a cron-fired run, so the question deadlocks
    the wave forever — await_workers used to wait the full max_wait (30 min)
    in this case because AWAITING_USER wasn't in STALE_GATED_STATES.

    Now AWAITING_USER counts as stale: after stale_threshold seconds with no
    answer, the orchestrator reports the worker as stalled and (per the
    healthy-peer logic) finalizes it once no peer is making progress. Workers
    can no longer hang a batch by calling ask_user from an unattended context.
    """
    from core.extensions.orchestration import await_workers
    from sessions.state_v2 import SessionStateV2 as S

    parent_id = mgr.create_session(title="P")
    _make_worker_in_state(mgr, parent_id, v2_state=S.AWAITING_USER, idle_seconds=200)

    out = await_workers(
        stale_threshold=10,
        _context={"session_id": parent_id},
    )
    # Single worker, AWAITING_USER for >10s → it's the only pending worker
    # and it's stalled → all-stalled return path fires immediately.
    assert "appear stalled" in out, (
        f"AWAITING_USER worker should be detected as stalled " f"(no human to answer); got: {out!r}"
    )


def test_await_workers_awaiting_user_under_threshold_keeps_waiting(mgr, monkeypatch):
    """Counterpart: a worker that JUST entered AWAITING_USER (idle < threshold)
    must not be flagged as stale yet — gives the user-via-orchestrator a
    chance to answer before the wave times out.
    """
    import core.extensions.orchestration as orch
    from sessions.state_v2 import SessionStateV2 as S

    parent_id = mgr.create_session(title="P")
    _make_worker_in_state(mgr, parent_id, v2_state=S.AWAITING_USER, idle_seconds=2)

    # Force the polling loop to exit on first iteration via a sentinel raise.
    class _StopAfterOne(Exception):
        pass

    iter_count = [0]

    def fast_sleep(_secs):
        iter_count[0] += 1
        if iter_count[0] >= 1:
            raise _StopAfterOne()

    monkeypatch.setattr(orch.time, "sleep", fast_sleep)

    import pytest

    with pytest.raises(_StopAfterOne):
        orch.await_workers(
            stale_threshold=120,  # 2s idle is well under
            _context={"session_id": parent_id},
        )
    # Reaching the sleep means the wait loop did NOT bail with an
    # all-stalled return. The worker is being awaited normally.


def test_await_workers_does_not_mark_unstarted_worker_as_done(mgr, monkeypatch):
    """Regression for workflow run fdfe1872 (2026-04-27 ai-tech-daily-brief
    re-run, wave 1): when run_coroutine_threadsafe schedules an agent task,
    AgentSession.task is set IMMEDIATELY but the task hasn't actually run
    yet — its first transition (IDLE_READY → SCOUTING) hasn't fired, so
    _turn_id is still 0. The polling loop's `has_started = task is not
    None or _turn_id > 0` would mark this worker DONE because it sees
    IDLE_READY + task != None. The wave loop then ran _finalize_step on
    a worker with empty transcript, reflect verdict=retry, retry exhausted
    → escalate → run halt.

    Fix: has_started is now `_turn_id > 0` only. A scheduled-but-not-yet-
    started Task does not count as "started".
    """
    from core.extensions.orchestration import await_workers
    from sessions.state_v2 import SessionStateV2 as S

    parent_id = mgr.create_session(title="P")
    parent = mgr.get(parent_id)
    worker_id = mgr.create_session(
        title="W",
        session_type="worker",
        parent_session_id=parent_id,
    )
    parent.worker_ids.append(worker_id)
    w = mgr.get(worker_id)

    # Simulate: task scheduled (object exists), state still IDLE_READY,
    # turn never started (turn_id=0).
    class _DummyTask:
        def done(self):
            return False

    w.task = _DummyTask()
    # Don't bump _turn_id — that's the whole point of the test.
    assert getattr(w, "_turn_id", 0) == 0
    assert w.task is not None

    _patch_no_wait(monkeypatch)
    out = await_workers(
        worker_ids=[worker_id],
        _context={"session_id": parent_id},
    )
    # Without the fix, the polling loop would have marked this worker as
    # done immediately (IDLE_READY + task != None), returned, and the
    # wave loop would finalize a worker that never started.
    # With the fix, has_started is False → pending_count=1 → loop continues.
    # The poll runs out via _patch_no_wait's fast clock; the response shows
    # the worker is NOT done.
    assert "all done" not in out.lower(), (
        "freshly-scheduled (Task created, no turn started yet) worker was " f"wrongly marked done: {out!r}"
    )


def test_await_workers_drains_pending_worker_id_appends(mgr, monkeypatch):
    """Regression for workflow run 38eb8522 (2026-04-27 ai-tech-daily-brief
    re-run): spawn_worker uses loop.call_soon_threadsafe to append to
    parent.worker_ids. The append is QUEUED on the event loop, not
    executed synchronously. If await_workers is called on the same thread
    immediately after, parent.worker_ids may still be empty — the entry
    "No workers to wait for" check would then return early and the
    orchestrator's wave loop would jump straight to _finalize_step on
    workers that hadn't even started yet.

    Fix: when the caller passes an explicit worker_ids list AND the loop
    is available, give the loop up to 5s to drain pending appends. Then
    proceed with the populated parent.worker_ids.
    """
    import asyncio as _asyncio
    import threading as _t
    import time as _ti

    from core.extensions.orchestration import await_workers
    from sessions.state_v2 import SessionStateV2 as S
    from sessions.state_v2 import _set_state

    parent_id = mgr.create_session(title="P")
    parent = mgr.get(parent_id)
    # Spawn the "real" worker first so manager.get() can find it.
    worker_id = mgr.create_session(
        title="W",
        session_type="worker",
        parent_session_id=parent_id,
    )
    w = mgr.get(worker_id)
    _set_state(w, S.IDLE_READY)
    w._turn_id = 1  # has_started = True so first poll marks it done

    # Set up a real event loop on a separate thread (matching how the
    # production app runs the loop on the main thread while tools run on
    # the executor pool).
    loop = _asyncio.new_event_loop()
    started = _t.Event()

    def _run_loop():
        _asyncio.set_event_loop(loop)
        started.set()
        loop.run_forever()

    t = _t.Thread(target=_run_loop, daemon=True)
    t.start()
    started.wait(timeout=2)

    try:
        # Schedule the append the same way spawn_worker does — but DON'T
        # let it drain before await_workers is called. The loop-runner
        # thread will service it as a callback.
        loop.call_soon_threadsafe(parent.worker_ids.append, worker_id)
        assert worker_id not in parent.worker_ids, "the test setup expects the append to be queued, not synchronous"

        out = await_workers(
            worker_ids=[worker_id],
            _context={"session_id": parent_id, "_loop": loop},
        )

        # Without the fix, parent.worker_ids would still be [] when the
        # entry check ran, and we'd see "No workers to wait for".
        # With the fix, the drain loop waits for the append to land,
        # then the polling loop sees IDLE_READY+has_started and returns
        # the normal "done" response.
        assert "No workers to wait for" not in out, (
            "await_workers gave up before the queued worker_ids.append " "could drain — race re-introduced"
        )
        assert worker_id in parent.worker_ids, "drain loop should have allowed the append to land"
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=2)
        loop.close()


# ---------------------------------------------------------------------------
# spawn_worker extends parent's LLM budget
# ---------------------------------------------------------------------------
# Regression for sessions bc6e98/cdbf08/8b6345 (cron daily-brief sessions):
# A "normal" parent that hand-rolls spawn_worker + await_workers (instead of
# spawning workers directly) would never extend its session-time budget,
# hit the 1800s cap mid-flight, and die on the synthesis turn's first scout
# acquire with LLMSessionTimeoutError. spawn_worker now extends the parent's
# budget by (worker_count + 1) × base_timeout per call, capped at 24h.


def test_spawn_worker_extends_parent_session_budget(mgr, monkeypatch):
    """spawn_worker must extend the parent session's LLM budget, mirroring
    the batch-scaled timeout pattern orchestrators rely on."""
    parent_id = mgr.create_session(title="Parent orchestrator")
    # spawn_worker now requires the parent to be in PROCESSING state.
    from sessions import state_v2 as sv2

    mgr.get(parent_id)._state_v2 = sv2.SessionStateV2.PROCESSING

    extend_calls: list[tuple] = []

    def fake_extend(sid, secs):
        extend_calls.append((sid, secs))
        return 1800.0 + secs

    import core.extensions.orchestration as orch

    monkeypatch.setattr(
        "core.llm.client.extend_session_budget",
        fake_extend,
    )
    monkeypatch.setattr("config.settings.llm_session_timeout", 1800)

    # spawn_worker dispatches the task on the running event loop; provide one.
    import asyncio

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # First spawn: parent.worker_ids is empty before this call, so the
    # extension uses (0 + 1 + 1) × 1800 = 3600s.
    out = orch.spawn_worker(
        "first task",
        title="W1",
        _context={"session_id": parent_id, "_loop": loop},
    )
    assert out.startswith("Worker spawned:"), out
    assert extend_calls, "spawn_worker must call extend_session_budget"
    sid, secs = extend_calls[0]
    assert sid == parent_id
    assert secs == 2 * 1800.0, f"first spawn should extend by 2*base, got {secs}"

    # Drain the queued worker_ids.append before the next spawn so worker_count
    # reflects the prior spawn.
    loop.run_until_complete(asyncio.sleep(0))

    # Second spawn: now worker_ids has 1 entry, so extension = (1+1+1)*1800 = 5400.
    extend_calls.clear()
    orch.spawn_worker(
        "second task",
        title="W2",
        _context={"session_id": parent_id, "_loop": loop},
    )
    sid2, secs2 = extend_calls[0]
    assert sid2 == parent_id
    assert secs2 == 3 * 1800.0, f"second spawn should extend by 3*base, got {secs2}"

    loop.close()


def test_spawn_worker_budget_extension_capped_at_24h(mgr, monkeypatch):
    """The extension is hard-capped at 24h. Use a huge base_timeout so a
    single spawn would compute beyond the cap without the min()."""
    parent_id = mgr.create_session(title="Pathological parent")
    from sessions import state_v2 as sv2

    mgr.get(parent_id)._state_v2 = sv2.SessionStateV2.PROCESSING

    extend_calls: list[tuple] = []
    monkeypatch.setattr(
        "core.llm.client.extend_session_budget",
        lambda sid, secs: extend_calls.append((sid, secs)) or (1.0 + secs),
    )
    # 1_000_000s base × 2 = 2_000_000 — must be capped to 86400 (24h).
    monkeypatch.setattr("config.settings.llm_session_timeout", 1_000_000)

    import asyncio

    import core.extensions.orchestration as orch

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    orch.spawn_worker(
        "task",
        title="W",
        _context={"session_id": parent_id, "_loop": loop},
    )
    loop.close()

    assert extend_calls, "spawn_worker must call extend_session_budget"
    _sid, secs = extend_calls[0]
    assert secs == 24 * 3600.0, f"extension must cap at 24h, got {secs}"


# ---------------------------------------------------------------------------
# Active-worker accounting
# ---------------------------------------------------------------------------


def test_reaped_workers_do_not_count_as_active():
    """A worker reaped from memory reports status "unknown". Counting it as
    active meant a long-lived parent whose finished workers had been reaped
    eventually could never spawn again — the LLM-capacity gate refused every
    call on behalf of workers that completed hours earlier."""
    from core.extensions.orchestration import _count_active_workers
    from sessions.manager import SessionManager
    from sessions.state import AgentSession

    mgr = SessionManager()
    parent = AgentSession(session_id="parent")
    # Three workers that ran and were subsequently reaped from memory.
    parent.worker_ids.extend(["gone1", "gone2", "gone3"])

    assert mgr.get_status("gone1")["status"] == "unknown"
    assert _count_active_workers(mgr, parent) == 0


def test_live_workers_still_count_as_active():
    from core.extensions.orchestration import _count_active_workers
    from sessions import state_v2 as sv2
    from sessions.manager import SessionManager
    from sessions.state import AgentSession

    mgr = SessionManager()
    parent = AgentSession(session_id="parent")

    running = AgentSession(session_id="running", session_type="worker")
    running._state_v2 = sv2.SessionStateV2.PROCESSING
    mgr._sessions["running"] = running

    settled = AgentSession(session_id="settled", session_type="worker")
    settled._state_v2 = sv2.SessionStateV2.IDLE_READY
    mgr._sessions["settled"] = settled

    parent.worker_ids.extend(["running", "settled", "reaped"])
    assert _count_active_workers(mgr, parent) == 1


def test_both_spawn_gates_share_one_definition():
    """The capacity warning and the max_concurrent_workers limit used to
    carry separate inline status tuples that disagreed about "unknown"."""
    import inspect

    from core.extensions import orchestration

    src = inspect.getsource(orchestration.spawn_worker)
    assert src.count("_count_active_workers(manager, parent)") == 2
    assert '"deleted"' not in src, "status tuples must not be re-inlined in spawn_worker"
