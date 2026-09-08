"""Eight ways a cron run reached `completed`, and five of them never ran.

`_execute_cron_job` treated a normal return from `_dispatch_prompt` as the
job having succeeded. It is not: `prompt()` returns just as readily after
refusing a cancelling session, after refusing a full queue, after queueing
the message behind somebody else's turn, after finding no agent runner at
all, and after folding the prompt into another message's row. When a turn
did run, `_run_agent_safe` recorded its failure on the session rather than
raising, so a turn that died still returned quietly; and the shielded wait
gave up at `cron_dispatch_timeout` while the turn carried on. Every one of
those wrote `status=completed` and emitted `job.completed`. Because
`reconcile_cron_runs()` only rewrites `claimed`/`running` rows, a false
`completed` was permanent, and `_notify_job_failure` never fired.

Admission and outcome are now separate facts. `prompt()` returns an
Admission naming what it decided; the TurnExecution it carries settles once,
from whichever path actually ends the work, and cron history is written from
that. A wait that expires is not an outcome — the row stays `running`, which
is the one thing that is true, and a watcher settles it when the turn really
ends. If the process dies first, the boot reconcile still calls it uncertain:
reported, never replayed.
"""

from __future__ import annotations

import asyncio

import pytest

from core.extensions import scheduling as sched
from db import models as db
from sessions import state_v2 as sv2
from sessions.manager import SessionManager
from sessions.state import PendingMessage


class _StubSnooze:
    def request_cancel(self):
        pass

    def notify_activity(self):
        pass


class _RecordingBus:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)

    def types(self):
        return [e.get("type") for e in self.events]


@pytest.fixture
def cron(monkeypatch):
    """A real manager plus the ambient stubs _execute_cron_job needs."""
    mgr = SessionManager()
    bus = _RecordingBus()
    monkeypatch.setattr("sessions.manager._manager", mgr)
    monkeypatch.setattr("core.snooze.get_snooze", lambda: _StubSnooze())
    monkeypatch.setattr("core.events.get_event_bus", lambda: bus)
    monkeypatch.setattr(sched, "_save_jobs", lambda: None)
    return mgr, bus


def _row(job_name):
    return db.list_cron_runs(job_name)[0]


# ---------------------------------------------------------------------------
# Path A: the session was cancelling
# ---------------------------------------------------------------------------


async def test_a_cancelling_session_is_a_failure_not_a_completion(cron):
    mgr, bus = cron
    sid = mgr.create_session(title="cancelling")
    mgr.get(sid)._state_v2 = sv2.SessionStateV2.CANCELLING

    await sched._execute_cron_job({"name": "A-cancelling", "prompt": "go", "session_id": sid, "model": ""})

    row = _row("A-cancelling")
    assert row["status"] == "error"
    assert "cancelling" in row["error"]
    assert "job.completed" not in bus.types()
    assert "job.error" in bus.types()


# ---------------------------------------------------------------------------
# Path B: the queue was full
# ---------------------------------------------------------------------------


async def test_a_full_queue_is_a_failure_not_a_completion(cron, monkeypatch):
    mgr, bus = cron
    monkeypatch.setattr("config.settings.max_pending_messages", 1)
    sid = mgr.create_session(title="backed up")
    session = mgr.get(sid)
    session._state_v2 = sv2.SessionStateV2.PROCESSING
    session.pending_messages.append(PendingMessage("already waiting", ""))

    await sched._execute_cron_job({"name": "B-full", "prompt": "go", "session_id": sid, "model": ""})

    row = _row("B-full")
    assert row["status"] == "error"
    assert "queue_full" in row["error"]
    assert "job.completed" not in bus.types()
    # A job that could not be admitted is a job the user should hear about.
    assert any("B-full" in (n["title"] or "") for n in db.get_notifications())


# ---------------------------------------------------------------------------
# Path C: queued behind a live turn
# ---------------------------------------------------------------------------


async def test_a_queued_job_completes_only_when_its_own_turn_does(cron):
    mgr, bus = cron
    sid = mgr.create_session(title="busy")
    session = mgr.get(sid)
    running = asyncio.Event()
    release = asyncio.Event()
    ran = []

    async def runner(session_id, message, session, **kw):
        ran.append(message)
        # A real turn leaves an assistant row; without one the post-turn
        # orphan sweep re-queues the message it just answered.
        db.add_message(session_id, "assistant", f"done: {message}")
        if message == "the user's request":
            running.set()
            await release.wait()

    mgr.set_agent_runner(runner)
    await mgr.prompt(sid, "the user's request")
    await running.wait()
    session.last_user_msg_at -= 60

    job = asyncio.create_task(
        sched._execute_cron_job({"name": "C-queued", "prompt": "the job", "session_id": sid, "model": ""})
    )
    while not session.pending_messages:
        await asyncio.sleep(0)

    assert _row("C-queued")["status"] == "running", "queued is not completed"
    assert "job.completed" not in bus.types()

    release.set()
    await asyncio.wait_for(job, timeout=10)

    assert ran == ["the user's request", "the job"]
    assert _row("C-queued")["status"] == "completed"
    assert bus.types().count("job.completed") == 1


# ---------------------------------------------------------------------------
# Path E: there was no agent runner
# ---------------------------------------------------------------------------


async def test_a_manager_with_no_runner_reports_a_failure(cron):
    mgr, bus = cron  # set_agent_runner deliberately not called

    await sched._execute_cron_job({"name": "E-no-runner", "prompt": "go", "session_id": None, "model": ""})

    row = _row("E-no-runner")
    assert row["status"] == "error"
    assert "no_agent_runner" in row["error"]
    assert "job.completed" not in bus.types()


# ---------------------------------------------------------------------------
# Path F: the Window-B orphan branch
# ---------------------------------------------------------------------------


async def test_a_job_queued_behind_a_restart_orphan_waits_for_its_own_turn(cron):
    """The orphan branch dispatches through _process_pending and returns.

    The job's message goes into the queue behind the recovered orphan, so the
    job has not run when _dispatch_prompt's caller resumes.
    """
    mgr, bus = cron
    sid = mgr.create_session(title="restarted")
    session = mgr.get(sid)
    # A user row a prior process never answered: the Window-B shape.
    db.add_message(sid, "user", "what a previous boot never got to")

    ran = []
    done = asyncio.Event()

    async def runner(session_id, message, session, **kw):
        ran.append(message)
        db.add_message(session_id, "assistant", f"done: {message}")
        if len(ran) == 2:
            done.set()

    mgr.set_agent_runner(runner)

    await sched._execute_cron_job({"name": "F-orphan", "prompt": "the job", "session_id": sid, "model": ""})
    await asyncio.wait_for(done.wait(), timeout=10)

    assert ran == ["what a previous boot never got to", "the job"]
    assert _row("F-orphan")["status"] == "completed"
    assert bus.types().count("job.completed") == 1


# ---------------------------------------------------------------------------
# Path G: the turn ran and failed, and the failure was swallowed
# ---------------------------------------------------------------------------


async def test_a_turn_that_failed_without_raising_is_recorded_as_an_error(cron):
    mgr, bus = cron

    async def runner(session_id, message, session, **kw):
        raise RuntimeError("the tool blew up")

    mgr.set_agent_runner(runner)

    await sched._execute_cron_job({"name": "G-failed", "prompt": "go", "session_id": None, "model": ""})

    row = _row("G-failed")
    assert row["status"] == "error"
    assert "the tool blew up" in row["error"]
    assert "job.completed" not in bus.types()
    assert any("G-failed" in (n["title"] or "") for n in db.get_notifications())


# ---------------------------------------------------------------------------
# Path H: the turn was cancelled mid-flight
# ---------------------------------------------------------------------------


async def test_a_cancelled_turn_is_not_a_completed_job(cron):
    mgr, bus = cron
    sid = mgr.create_session(title="to be cancelled")
    session = mgr.get(sid)
    running = asyncio.Event()

    async def runner(session_id, message, session, **kw):
        running.set()
        await asyncio.Event().wait()  # never returns; the cancel is the exit

    mgr.set_agent_runner(runner)

    job = asyncio.create_task(
        sched._execute_cron_job({"name": "H-cancelled", "prompt": "go", "session_id": sid, "model": ""})
    )
    await running.wait()
    session.task.cancel()
    await asyncio.wait_for(job, timeout=10)

    row = _row("H-cancelled")
    assert row["status"] == "error"
    assert "stopped before it finished" in row["error"]
    assert "job.completed" not in bus.types()
    # A deliberate stop is not a breakage: no high-urgency push for it.
    assert not any("H-cancelled" in (n["title"] or "") for n in db.get_notifications())


# ---------------------------------------------------------------------------
# The wait is not the work
# ---------------------------------------------------------------------------


async def test_a_wait_that_expires_leaves_the_run_running_then_settles_it(cron, monkeypatch):
    mgr, bus = cron
    monkeypatch.setattr("config.settings.cron_dispatch_timeout", 0.2)
    running = asyncio.Event()
    release = asyncio.Event()

    async def runner(session_id, message, session, **kw):
        running.set()
        await release.wait()

    mgr.set_agent_runner(runner)

    await sched._execute_cron_job({"name": "T-slow", "prompt": "go", "session_id": None, "model": ""})
    await running.wait()

    row = _row("T-slow")
    assert row["status"] == "running", "the wait gave up; the turn did not"
    assert not row["completed_at"]
    assert "job.completed" not in bus.types()
    assert "job.error" not in bus.types(), "and it is not a failure either — it is still going"

    release.set()
    for _ in range(1000):
        if _row("T-slow")["status"] != "running":
            break
        await asyncio.sleep(0.01)
    assert _row("T-slow")["status"] == "completed"
    assert bus.types().count("job.completed") == 1, "and it announces exactly once, at the end"


async def test_a_run_the_process_died_inside_is_still_uncertain(cron, monkeypatch):
    """Restart uncertainty is preserved: never replayed, always reported."""
    mgr, bus = cron
    monkeypatch.setattr("config.settings.cron_dispatch_timeout", 0.2)
    running = asyncio.Event()
    release = asyncio.Event()

    async def runner(session_id, message, session, **kw):
        running.set()
        await release.wait()

    mgr.set_agent_runner(runner)
    await sched._execute_cron_job({"name": "U-crash", "prompt": "go", "session_id": None, "model": ""})
    await running.wait()
    assert _row("U-crash")["status"] == "running"

    affected = db.reconcile_uncertain_cron_runs()
    assert any(r["job_name"] == "U-crash" for r in affected)
    row = _row("U-crash")
    assert row["status"] == "uncertain"
    assert "not replayed" in row["error"]

    release.set()
    session = mgr.get(row["session_id"])
    if session is not None and session.task is not None:
        await asyncio.wait({session.task})
    for task in list(sched._unresolved_watchers):
        task.cancel()


# ---------------------------------------------------------------------------
# The handle itself
# ---------------------------------------------------------------------------


async def test_an_admission_is_not_an_outcome(cron):
    mgr, _bus = cron
    sid = mgr.create_session(title="idle")
    release = asyncio.Event()
    running = asyncio.Event()

    async def runner(session_id, message, session, **kw):
        running.set()
        await release.wait()

    mgr.set_agent_runner(runner)

    admission = await mgr.prompt(sid, "go")
    await running.wait()
    assert admission.outcome == "started"
    assert admission.accepted
    assert not admission.execution.settled, "accepted is not finished"

    release.set()
    assert await asyncio.wait_for(admission.execution.wait(), timeout=10) == "completed"
    assert admission.execution.succeeded


async def test_a_handle_settles_exactly_once(cron):
    mgr, _bus = cron
    sid = mgr.create_session(title="idle")

    async def runner(session_id, message, session, **kw):
        pass

    mgr.set_agent_runner(runner)
    admission = await mgr.prompt(sid, "go")
    await asyncio.wait_for(admission.execution.wait(), timeout=10)

    assert admission.execution.result == "completed"
    assert admission.execution.settle("failed", error="a later writer") is False
    assert admission.execution.result == "completed"


async def test_cancelling_the_queue_settles_the_work_it_dropped(cron):
    """A queued job whose queue is cleared must not leave a caller waiting."""
    mgr, _bus = cron
    sid = mgr.create_session(title="busy")
    session = mgr.get(sid)
    running = asyncio.Event()
    release = asyncio.Event()

    async def runner(session_id, message, session, **kw):
        running.set()
        await release.wait()

    mgr.set_agent_runner(runner)
    await mgr.prompt(sid, "the user's request")
    await running.wait()
    session.last_user_msg_at -= 60

    admission = await mgr.prompt(sid, "the job", origin="scheduled")
    assert admission.outcome == "queued"

    mgr.drop_pending_for_cancel(session)
    assert admission.execution.result == "cancelled"

    release.set()
    await asyncio.wait({session.task})
