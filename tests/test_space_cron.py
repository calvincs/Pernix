"""Space-bound cron (v33): extra_meta round-trip, dispatch-session space,
schedule_job inheritance, unbind helper, pinned-cron prune exclusion."""

from __future__ import annotations

import pytest

import core.extensions.scheduling as sched
from core import spaces as spaces_lib
from db import models as db


@pytest.fixture(autouse=True)
def _fresh_space_cache():
    spaces_lib.invalidate_space_cache()
    yield
    spaces_lib.invalidate_space_cache()


class _FakeJob:
    def __init__(self, jid, meta):
        self.id = jid
        self.kwargs = {"meta": meta}
        self.trigger = "cron"
        self.next_run_time = object()


class _FakeScheduler:
    def __init__(self, jobs):
        self._jobs = jobs

    def get_jobs(self):
        return self._jobs


def test_space_id_rides_extra_meta_roundtrip(tmp_path, monkeypatch):
    """_save_jobs writes space_id (non-structural), _load_jobs re-reads it
    verbatim into extra_meta — a restart keeps the binding."""
    monkeypatch.setattr(sched, "CRON_PATH", tmp_path / "cron_jobs.json")
    meta = {
        "name": "j1",
        "cron_expr": "*/5 * * * *",
        "prompt": "do it",
        "model": "",
        "session_id": None,
        "created_at": "2026-09-01",
        "space_id": "spaceid123",
    }
    fake = _FakeScheduler([_FakeJob("j1", meta)])
    monkeypatch.setattr(sched, "_scheduler", fake)
    monkeypatch.setattr(sched, "_get_scheduler", lambda: fake)

    sched._save_jobs()
    import json

    entries = json.loads((tmp_path / "cron_jobs.json").read_text())
    assert entries[0]["space_id"] == "spaceid123"
    # And space_id must NOT be structural — the round-trip depends on it.
    assert "space_id" not in sched._ENTRY_STRUCTURAL_KEYS


def test_ensure_dispatch_session_creates_in_space(monkeypatch):
    created = {}

    class _Mgr:
        def create_session(self, title="", session_type="normal", space_id=None):
            created.update(title=title, session_type=session_type, space_id=space_id)
            return "newsid"

    monkeypatch.setattr("sessions.manager.get_manager", lambda: _Mgr())
    out = sched._ensure_dispatch_session(None, title="Cron: j1", space_id="sp1")
    assert out == "newsid"
    assert created == {"title": "Cron: j1", "session_type": "cron", "space_id": "sp1"}
    # Pinned session ids are reused verbatim, space or not.
    assert sched._ensure_dispatch_session("pinned", title="x", space_id="sp1") == "pinned"


def test_schedule_job_inherits_caller_space(monkeypatch, tmp_path):
    sp = db.create_space("Lab", "#123456", "lab")
    sid = db.create_session(title="in lab", space_id=sp["id"])
    monkeypatch.setattr(sched, "CRON_PATH", tmp_path / "cron_jobs.json")

    captured = {}

    def _fake_add(name, cron_expr, prompt, session_id=None, model="", extra_meta=None):
        captured["extra_meta"] = extra_meta

    monkeypatch.setattr(sched, "_add_job_internal", _fake_add)
    monkeypatch.setattr(sched, "_save_jobs", lambda: None)
    monkeypatch.setattr(sched, "_scheduler", object())

    out = sched.schedule_job("j2", "*/5 * * * *", "prompt text", _context={"session_id": sid})
    assert "scheduled" in out
    assert captured["extra_meta"]["space_id"] == sp["id"]

    # space_id="none" opts out of inheritance.
    out = sched.schedule_job("j3", "*/5 * * * *", "prompt text", space_id="none", _context={"session_id": sid})
    assert "scheduled" in out
    assert "space_id" not in captured["extra_meta"]

    # An unknown explicit space is refused.
    out = sched.schedule_job("j4", "*/5 * * * *", "prompt text", space_id="missing")
    assert out.startswith("Error")


def test_unbind_and_list_space_jobs(monkeypatch):
    m1 = {"space_id": "sp1"}
    m2 = {"space_id": "sp2"}
    fake = _FakeScheduler([_FakeJob("a", m1), _FakeJob("b", m2)])
    monkeypatch.setattr(sched, "_get_scheduler", lambda: fake)
    saved = []
    monkeypatch.setattr(sched, "_save_jobs", lambda: saved.append(True))

    assert sched.jobs_for_space("sp1") == ["a"]
    assert sched.unbind_space_jobs("sp1") == 1
    assert "space_id" not in m1
    assert m2["space_id"] == "sp2"
    assert saved  # persisted


def test_pinned_cron_sessions_survive_prune():
    old = "2020-01-01T00:00:00+00:00"
    doomed = db.create_session(title="Cron: sweep-me")
    kept = db.create_session(title="Cron: keep-me")
    with db.connect_sessions() as conn:
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id IN (?, ?)", (old, doomed, kept))
        conn.execute("UPDATE sessions SET pinned = 1 WHERE id = ?", (kept,))
    ids = {r["id"] for r in db.list_cron_sessions_before(max_age_days=7)}
    assert doomed in ids
    assert kept not in ids
