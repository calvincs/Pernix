"""Two cron-schedule defects.

1. _load_jobs guarded heartbeat entries but not cron ones, so a single
   malformed record — a missing "prompt" key, an expression this
   APScheduler rejects — aborted the loop: every job after it went
   unscheduled AND the coalesced catch-up never ran for any of them,
   behind one "Failed to load cron jobs" line.
2. last_fired_at was never re-baselined on resume or on a schedule change,
   so a job paused for ten days (or moved from weekly to hourly) came back
   and dispatched a catch-up run claiming the server had been down across
   slots it was deliberately paused for.
"""

import json

import pytest

from core.extensions import scheduling as sched


@pytest.fixture
def cron_file(tmp_path, monkeypatch):
    path = tmp_path / "cron_jobs.json"
    monkeypatch.setattr(sched, "CRON_PATH", path)
    return path


def test_one_bad_entry_does_not_drop_the_others(cron_file, monkeypatch):
    cron_file.write_text(
        json.dumps(
            [
                {"name": "first", "cron_expr": "0 3 * * *", "prompt": "a"},
                {"name": "broken", "cron_expr": "0 3 * * *"},  # no prompt
                {"name": "third", "cron_expr": "0 4 * * *", "prompt": "c"},
            ]
        )
    )
    added = []
    monkeypatch.setattr(
        sched,
        "_add_job_internal",
        lambda name, cron, prompt, **kw: added.append(name) if prompt else (_ for _ in ()).throw(KeyError("prompt")),
    )
    monkeypatch.setattr(sched, "_scheduler", None)
    caught = []
    monkeypatch.setattr(sched, "_schedule_coalesced_catchup", lambda jobs: caught.append(len(jobs)))

    sched._load_jobs()
    assert added == ["first", "third"], "a job after the bad one must still be scheduled"
    assert caught == [3], "and the catch-up must still run"


def test_resume_rebaselines_the_fire_clock(cron_file, monkeypatch):
    updates = {}
    monkeypatch.setattr(sched, "_update_job_field", lambda name, field, value: updates.__setitem__(field, value))

    class _Sched:
        def resume_job(self, name):
            pass

    monkeypatch.setattr(sched, "_get_scheduler", lambda: _Sched())
    out = sched.resume_job("nightly")
    assert "resumed" in out
    assert updates["paused"] is False
    assert updates["last_fired_at"], "a paused stretch is not downtime to catch up on"


def test_a_failed_resume_does_not_rebaseline(cron_file, monkeypatch):
    updates = {}
    monkeypatch.setattr(sched, "_update_job_field", lambda name, field, value: updates.__setitem__(field, value))

    class _Sched:
        def resume_job(self, name):
            raise KeyError("no such job")

    monkeypatch.setattr(sched, "_get_scheduler", lambda: _Sched())
    assert "Error" in sched.resume_job("ghost")
    assert updates == {}
