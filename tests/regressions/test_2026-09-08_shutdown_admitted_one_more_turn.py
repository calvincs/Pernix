"""Shutdown raised its flag, took a snapshot, and then let a new turn start.

`lifespan`'s teardown set `manager.shutting_down`, snapshotted the live agent
tasks, cancelled them, awaited the gather, slept half a second, stopped the
maintenance runner — and only then stopped APScheduler. The flag was read in
exactly three places, all of them recovery paths (`_resume_from_workers` and
the two guards in `_process_pending`). `prompt()` never read it at all.

So a cron fire or a heartbeat tick landing anywhere in that window — 0.5s to
roughly 8.5s of wall clock — created a fresh `_run_agent_safe` task that
reached SCOUTING, un-cancellable by a drain that had already run, against an
LLM client, MCP bridge and browser being torn down. And `session.task` only
ever points at the newest turn, so even a second snapshot could miss one.

The producer the ordering missed entirely is orchestration: `message_worker`,
`resume_worker` and `_notify_parent_of_worker_result` call `prompt()` from
tool threads via `run_coroutine_threadsafe`. Those threads are never
cancelled by shutdown, so they could schedule a brand new turn right up until
the loop stopped. That is why the gate lives inside `prompt()` — every start
path is covered by construction, whichever thread it came from.

Admission now closes first, is rechecked after the session lock and after the
persistence hop, the scheduler is stopped before the final collection, and
the drain re-collects until nothing is left. A refused dispatch settles its
execution as rejected, so shutdown reads as a job that did not run rather
than as cron success.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from core.extensions import scheduling as sched
from db import models as db
from sessions.manager import SessionManager


@pytest.fixture
def mgr(monkeypatch):
    fresh = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", fresh)

    async def runner(session_id, message, session, **kw):
        pass

    fresh.set_agent_runner(runner)
    return fresh


async def test_a_prompt_after_admission_closes_starts_nothing(mgr):
    sid = mgr.create_session(title="closing time")
    session = mgr.get(sid)

    mgr.close_admission()
    admission = await mgr.prompt(sid, "one more thing")

    assert admission.rejected
    assert admission.reason == "shutting_down"
    assert admission.execution.result == "rejected"
    assert session.task is None
    assert mgr.live_turn_tasks() == []


async def test_a_dispatch_waiting_for_the_lock_is_refused_when_it_wakes(mgr):
    """The recheck after the lock: the snapshot has already been taken."""
    sid = mgr.create_session(title="contended")
    session = mgr.get(sid)

    await session.lock.acquire()
    pending = asyncio.create_task(mgr.prompt(sid, "queued behind the lock"))
    for _ in range(10):
        await asyncio.sleep(0)  # let it reach the lock
    assert not pending.done()

    mgr.close_admission()
    session.lock.release()
    admission = await asyncio.wait_for(pending, timeout=5)

    assert admission.rejected and admission.reason == "shutting_down"
    assert session.task is None


async def test_a_dispatch_inside_the_persistence_hop_never_creates_a_task(mgr, monkeypatch):
    """The last recheck: after the off-loop write, before create_task.

    The message row is already on disk when the refusal happens. It stays
    there deliberately — the orphan sweep re-offers it after the restart,
    which is how an accepted message survives a shutdown instead of being
    silently lost.
    """
    sid = mgr.create_session(title="mid-write")
    session = mgr.get(sid)

    in_write = threading.Event()
    may_finish = threading.Event()
    real_add = db.add_message

    def blocking_add(*args, **kwargs):
        row = real_add(*args, **kwargs)
        in_write.set()
        may_finish.wait(5)
        return row

    monkeypatch.setattr("sessions.manager.db.add_message", blocking_add)

    pending = asyncio.create_task(mgr.prompt(sid, "written but never run"))
    while not in_write.is_set():
        await asyncio.sleep(0)

    mgr.close_admission()
    may_finish.set()
    admission = await asyncio.wait_for(pending, timeout=5)

    assert admission.rejected and admission.reason == "shutting_down"
    assert session.task is None
    assert mgr.live_turn_tasks() == []
    rows = [m for m in db.get_messages(sid) if m["role"] == "user"]
    assert [r["content"] for r in rows] == ["written but never run"], "the message must survive for recovery"


async def test_the_drain_finds_a_turn_session_task_no_longer_points_at(mgr):
    sid = mgr.create_session(title="two turns deep")
    session = mgr.get(sid)
    running = asyncio.Event()

    async def slow_runner(session_id, message, session, **kw):
        running.set()
        await asyncio.Event().wait()

    mgr.set_agent_runner(slow_runner)
    await mgr.prompt(sid, "the turn that is really running")
    await running.wait()
    forgotten = session.task
    session.task = None  # what the next turn's dispatch does to the handle

    mgr.close_admission()
    drained = await mgr.drain_turns(timeout=5.0)

    assert drained == 1
    assert forgotten.cancelled() or forgotten.done()
    assert mgr.live_turn_tasks() == []


async def test_the_drain_refuses_to_run_before_admission_closes(mgr):
    with pytest.raises(RuntimeError, match="close_admission"):
        await mgr.drain_turns(timeout=0.1)


async def test_a_cron_fire_across_the_window_is_recorded_as_not_run(mgr, monkeypatch):
    class _StubSnooze:
        def request_cancel(self):
            pass

        def notify_activity(self):
            pass

    events = []

    class _Bus:
        def emit(self, event):
            events.append(event)

    monkeypatch.setattr("core.snooze.get_snooze", lambda: _StubSnooze())
    monkeypatch.setattr("core.events.get_event_bus", lambda: _Bus())
    monkeypatch.setattr(sched, "_save_jobs", lambda: None)

    mgr.close_admission()
    await sched._execute_cron_job({"name": "S-shutdown", "prompt": "go", "session_id": None, "model": ""})

    row = db.list_cron_runs("S-shutdown")[0]
    assert row["status"] == "error"
    assert "shutting_down" in row["error"]
    assert "job.completed" not in [e.get("type") for e in events]
    # Every unattended job at once would be a notification storm on each
    # restart; the row carries the record instead.
    assert not any("S-shutdown" in (n["title"] or "") for n in db.get_notifications())


async def test_the_pending_dispatcher_rechecks_after_its_own_lock(mgr):
    """_process_pending's entry guard is not enough — it awaits the lock."""
    sid = mgr.create_session(title="queued work")
    session = mgr.get(sid)
    from sessions.state import PendingMessage

    session.pending_messages.append(PendingMessage("queued", "", False, 0.0, None))

    await session.lock.acquire()
    pending = asyncio.create_task(mgr._process_pending(session))
    for _ in range(10):
        await asyncio.sleep(0)
    assert not pending.done()

    mgr.close_admission()
    session.lock.release()
    await asyncio.wait_for(pending, timeout=5)

    assert session.task is None
    assert mgr.live_turn_tasks() == []


def test_the_lifespan_closes_admission_before_it_collects_anything():
    import inspect

    import api.app as app_mod

    src = inspect.getsource(app_mod.lifespan)
    close = src.index("_mgr.close_admission()")
    stop_producer = src.index("sched.shutdown(wait=False)")
    drain = src.index("_mgr.drain_turns(")
    browser = src.index("_close_browser")
    assert close < stop_producer < drain, "producers stop before the final collection"
    assert drain < browser, "and everything admitted drains before resources close"
