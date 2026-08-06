"""Regression: cron job variant fields were silently dropped on restart.

Shipped defect (found in the 2026-08-05 adaptation-plan review, fixed in 1c):
`_save_jobs` persisted a fixed field list and `_load_jobs` reconstructed jobs
from only five hardcoded keys, so any extra metadata on a job — the workflow
variant's fields survived only via special-casing, and anything else
(a future `kind`, `last_fired_at`, ...) was erased every restart. The fix
makes both sides round-trip every non-structural key verbatim.

Kept as a regression pin because Phases 3/3.5 (heartbeat and canary job
kinds) depend on this round-trip; silently re-breaking it would erase those
jobs' identities on the next restart.
"""

import core.extensions.scheduling as sched


class _Job:
    def __init__(self, job_id, meta):
        self.id = job_id
        self.kwargs = {"meta": meta}
        self.func = sched._execute_cron_job
        self.trigger = "cron"
        self.next_run_time = "soon"


class _Sched:
    def __init__(self, jobs):
        self._jobs = {j.id: j for j in jobs}

    def get_jobs(self):
        return list(self._jobs.values())

    def get_job(self, name):
        return None  # coalescer no-ops in this pin

    def pause_job(self, name):
        pass


def test_unknown_job_fields_survive_save_load_cycle(tmp_path, monkeypatch):
    monkeypatch.setattr(sched, "CRON_PATH", tmp_path / "cron_jobs.json")
    meta = {
        "name": "j",
        "cron_expr": "0 9 * * *",
        "prompt": "p",
        "model": "",
        "session_id": None,
        "kind": "future-variant",  # the class of field that used to vanish
        "owner": "user",
        "last_fired_at": "2026-08-04T09:00:00+00:00",
    }
    monkeypatch.setattr(sched, "_scheduler", _Sched([_Job("j", meta)]))
    sched._save_jobs()

    captured = {}
    monkeypatch.setattr(
        sched,
        "_add_job_internal",
        lambda name, expr, prompt, session_id=None, model="", extra_meta=None: captured.update(extra_meta or {}),
    )
    monkeypatch.setattr(sched, "_schedule_coalesced_catchup", lambda entries: None)
    sched._load_jobs()

    assert captured["kind"] == "future-variant"
    assert captured["owner"] == "user"
    assert captured["last_fired_at"] == "2026-08-04T09:00:00+00:00"
