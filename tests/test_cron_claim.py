"""Pernix — Cron claim-before-deliver (adaptation plan 1c).

The run row (status='claimed', fire_time) and the advanced last_fired_at hit
disk BEFORE the prompt is dispatched; a crash anywhere after surfaces as an
'uncertain' run at next startup — reported, never replayed. Missed ticks
across downtime coalesce into at most one catch-up run per job.
"""

from datetime import datetime, timezone

import core.extensions.scheduling as sched
from db import models as db

# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------


def test_add_cron_run_claim_roundtrip():
    run_id = db.add_cron_run("job-a", "sess-1", status="claimed", fire_time="2026-08-05T03:00:00+00:00")
    row = [r for r in db.list_cron_runs("job-a") if r["id"] == run_id][0]
    assert row["status"] == "claimed"
    assert row["fire_time"] == "2026-08-05T03:00:00+00:00"
    assert not row["completed_at"]


def test_update_cron_run_nonterminal_keeps_completed_at_empty():
    run_id = db.add_cron_run("job-b", None, status="claimed")
    db.update_cron_run(run_id, "running")
    row = [r for r in db.list_cron_runs("job-b") if r["id"] == run_id][0]
    assert row["status"] == "running"
    assert not row["completed_at"]

    db.update_cron_run(run_id, "completed")
    row = [r for r in db.list_cron_runs("job-b") if r["id"] == run_id][0]
    assert row["status"] == "completed"
    assert row["completed_at"]


def test_reconcile_marks_claimed_and_running_uncertain():
    claimed = db.add_cron_run("job-c", None, status="claimed", fire_time="x")
    running = db.add_cron_run("job-c", None, status="running")
    done = db.add_cron_run("job-c", None)
    db.update_cron_run(done, "completed")

    affected = db.reconcile_uncertain_cron_runs()
    assert {r["id"] for r in affected} == {claimed, running}

    rows = {r["id"]: r for r in db.list_cron_runs("job-c")}
    assert rows[claimed]["status"] == "uncertain"
    assert rows[running]["status"] == "uncertain"
    assert "not replayed" in rows[claimed]["error"]
    assert rows[done]["status"] == "completed"

    # Idempotent: second sweep finds nothing.
    assert db.reconcile_uncertain_cron_runs() == []


def test_reconcile_cron_runs_notifies():
    db.add_cron_run("job-d", None, status="running")
    count = sched.reconcile_cron_runs()
    assert count == 1
    notes = db.get_notifications()
    assert any("uncertain" in n["title"] for n in notes)


# ---------------------------------------------------------------------------
# Missed-fire computation
# ---------------------------------------------------------------------------


def test_count_missed_fires():
    now = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
    # Daily at 03:00, last fired 3 days ago at noon -> 3 missed (8/3, 8/4, 8/5).
    assert sched._count_missed_fires("0 3 * * *", "2026-08-02T12:00:00+00:00", now) == 3
    # Last fired after the most recent tick -> nothing missed.
    assert sched._count_missed_fires("0 3 * * *", "2026-08-05T04:00:00+00:00", now) == 0
    # Garbage inputs -> 0, never raises.
    assert sched._count_missed_fires("not-cron", "2026-08-02T12:00:00+00:00", now) == 0
    assert sched._count_missed_fires("0 3 * * *", "not-a-date", now) == 0
    # Cap prevents unbounded spins on every-minute jobs with stale baselines.
    assert sched._count_missed_fires("* * * * *", "2020-01-01T00:00:00+00:00", now, cap=50) == 50


# ---------------------------------------------------------------------------
# Fakes for scheduler-shaped tests (no real APScheduler loop)
# ---------------------------------------------------------------------------


class _FakeJob:
    def __init__(self, job_id, meta, func=None, trigger="cron", next_run_time="soon"):
        self.id = job_id
        self.kwargs = {"meta": meta}
        self.func = func if func is not None else sched._execute_cron_job
        self.trigger = trigger
        self.next_run_time = next_run_time


class _FakeScheduler:
    def __init__(self, jobs=None):
        self._jobs = {j.id: j for j in (jobs or [])}
        self.added = []

    def get_jobs(self):
        return list(self._jobs.values())

    def get_job(self, name):
        return self._jobs.get(name)

    def add_job(self, func, trigger=None, id=None, replace_existing=False, misfire_grace_time=None, kwargs=None):
        job = _FakeJob(id, (kwargs or {}).get("meta", {}), func=func, trigger=trigger)
        self._jobs[id] = job
        self.added.append(job)
        return job

    def pause_job(self, name):
        pass


# ---------------------------------------------------------------------------
# Persistence round-trip
# ---------------------------------------------------------------------------


def test_save_jobs_roundtrips_extra_meta_and_skips_transient(tmp_path, monkeypatch):
    monkeypatch.setattr(sched, "CRON_PATH", tmp_path / "cron_jobs.json")
    meta = {
        "name": "j1",
        "cron_expr": "0 9 * * *",
        "prompt": "do things",
        "model": "",
        "session_id": None,
        "session_mode": "fresh",
        "created_at": "2026-08-01T00:00:00+00:00",
        "kind": "heartbeat",
        "last_fired_at": "2026-08-04T09:00:00+00:00",
        "workflow_name": "wf",
    }
    transient = {"name": "j1__coalesced", "transient": True, "prompt": "x", "cron_expr": "0 9 * * *"}
    fake = _FakeScheduler([_FakeJob("j1", meta), _FakeJob("j1__coalesced", transient)])
    monkeypatch.setattr(sched, "_scheduler", fake)

    sched._save_jobs()
    entries = sched._read_jobs_json()
    assert len(entries) == 1  # transient skipped
    e = entries[0]
    assert e["name"] == "j1"
    assert e["kind"] == "heartbeat"
    assert e["last_fired_at"] == "2026-08-04T09:00:00+00:00"
    assert e["workflow_name"] == "wf"

    # Load side: every non-structural key round-trips into extra_meta.
    captured = []
    monkeypatch.setattr(
        sched,
        "_add_job_internal",
        lambda name, expr, prompt, session_id=None, model="", extra_meta=None: captured.append(
            (name, extra_meta or {})
        ),
    )
    monkeypatch.setattr(sched, "_schedule_coalesced_catchup", lambda entries: None)
    sched._load_jobs()
    assert len(captured) == 1
    name, extra = captured[0]
    assert name == "j1"
    assert extra["kind"] == "heartbeat"
    assert extra["last_fired_at"] == "2026-08-04T09:00:00+00:00"
    assert extra["created_at"] == "2026-08-01T00:00:00+00:00"
    assert "cron_expr" not in extra  # structural keys stay positional


# ---------------------------------------------------------------------------
# Coalesced catch-up
# ---------------------------------------------------------------------------


def test_coalesced_catchup_dispatches_one_transient_run(monkeypatch, tmp_path):
    monkeypatch.setattr(sched, "CRON_PATH", tmp_path / "cron_jobs.json")
    meta = {
        "name": "daily",
        "cron_expr": "0 3 * * *",
        "prompt": "morning digest",
        "last_fired_at": "2026-08-01T03:00:00+00:00",
    }
    fake = _FakeScheduler([_FakeJob("daily", meta)])
    monkeypatch.setattr(sched, "_scheduler", fake)
    saved = []
    monkeypatch.setattr(sched, "_save_jobs", lambda: saved.append(True))

    entries = [{"name": "daily", "cron_expr": "0 3 * * *", "paused": False}]
    sched._schedule_coalesced_catchup(entries)

    assert len(fake.added) == 1
    co = fake.added[0]
    assert co.id == "daily__coalesced"
    assert co.kwargs["meta"]["transient"] is True
    assert co.kwargs["meta"]["prompt"].startswith("[coalesced")
    assert "morning digest" in co.kwargs["meta"]["prompt"]
    # Parent advanced before dispatch so a crash can't re-coalesce the span.
    assert meta["last_fired_at"] > "2026-08-01T03:00:00+00:00"
    assert saved


def test_coalesced_catchup_skips_fresh_paused_and_legacy(monkeypatch, tmp_path):
    monkeypatch.setattr(sched, "CRON_PATH", tmp_path / "cron_jobs.json")
    now_iso = datetime.now(timezone.utc).isoformat()
    fresh = {"name": "fresh", "cron_expr": "0 3 * * *", "prompt": "p", "last_fired_at": now_iso}
    legacy = {"name": "legacy", "cron_expr": "0 3 * * *", "prompt": "p"}
    paused = {"name": "paused", "cron_expr": "0 3 * * *", "prompt": "p", "last_fired_at": "2026-08-01T03:00:00+00:00"}
    fake = _FakeScheduler([_FakeJob("fresh", fresh), _FakeJob("legacy", legacy), _FakeJob("paused", paused)])
    monkeypatch.setattr(sched, "_scheduler", fake)
    monkeypatch.setattr(sched, "_save_jobs", lambda: None)

    entries = [
        {"name": "fresh", "cron_expr": "0 3 * * *", "paused": False},
        {"name": "legacy", "cron_expr": "0 3 * * *", "paused": False},
        {"name": "paused", "cron_expr": "0 3 * * *", "paused": True},
    ]
    sched._schedule_coalesced_catchup(entries)

    assert fake.added == []  # nothing dispatched
    assert legacy["last_fired_at"]  # legacy job got a baseline, no catch-up


# ---------------------------------------------------------------------------
# Claim-before-deliver ordering in _execute_cron_job
# ---------------------------------------------------------------------------


class _StubSnooze:
    def request_cancel(self):
        pass

    def notify_activity(self):
        pass


class _StubBus:
    def emit(self, *_a, **_k):
        pass


async def test_execute_cron_job_claims_before_prompt(monkeypatch):
    observed = {}

    class _Mgr:
        def create_session(self, title="", session_type="normal"):
            return "sess-claim"

        def get(self, sid):
            return None

        async def prompt(self, sid, prompt):
            # By the time the prompt is dispatched, the claim must be durable.
            runs = db.list_cron_runs("claim-job")
            observed["status_at_prompt"] = runs[0]["status"]
            observed["fire_time_at_prompt"] = runs[0]["fire_time"]
            observed["last_fired_meta"] = meta.get("last_fired_at")

        def broadcast(self, *_a, **_k):
            pass

    monkeypatch.setattr("sessions.manager.get_manager", lambda: _Mgr())
    monkeypatch.setattr("core.snooze.get_snooze", lambda: _StubSnooze())
    monkeypatch.setattr("core.events.get_event_bus", lambda: _StubBus())
    monkeypatch.setattr(sched, "_save_jobs", lambda: None)

    meta = {"name": "claim-job", "prompt": "run it", "session_id": None, "model": ""}
    await sched._execute_cron_job(meta)

    assert observed["status_at_prompt"] == "running"
    assert observed["fire_time_at_prompt"]
    assert observed["last_fired_meta"] == observed["fire_time_at_prompt"]
    final = db.list_cron_runs("claim-job")[0]
    assert final["status"] == "completed"


async def test_execute_cron_job_error_path(monkeypatch):
    class _Mgr:
        def create_session(self, title="", session_type="normal"):
            return "sess-err"

        def get(self, sid):
            return None

        async def prompt(self, sid, prompt):
            raise RuntimeError("boom")

        def broadcast(self, *_a, **_k):
            pass

    monkeypatch.setattr("sessions.manager.get_manager", lambda: _Mgr())
    monkeypatch.setattr("core.snooze.get_snooze", lambda: _StubSnooze())
    monkeypatch.setattr("core.events.get_event_bus", lambda: _StubBus())
    monkeypatch.setattr(sched, "_save_jobs", lambda: None)

    meta = {"name": "err-job", "prompt": "run it", "session_id": None, "model": ""}
    await sched._execute_cron_job(meta)

    final = db.list_cron_runs("err-job")[0]
    assert final["status"] == "error"
    assert "boom" in final["error"]
