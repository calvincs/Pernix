"""Heartbeats (recurring instructions steered into running work) were
retired in the 2026-10 prune: no tools, no API, no setting.

A box that had heartbeats set still carries `kind: "heartbeat"` entries in
data/cron_jobs.json. The loader drops them and rewrites the file once
without them; ordinary cron entries load untouched, and a cron entry that
fails to load is NOT erased by that rewrite.
"""

from __future__ import annotations

import json

import core.extensions.scheduling as sched

_HB = {
    "name": "hb_user_abc123def456",
    "cron_expr": "",
    "prompt": "",
    "kind": "heartbeat",
    "owner": "user",
    "heartbeat_session_id": "abc123def456xyz",
    "instruction": "stay focused",
    "every": "5m",
    "paused": False,
}


def test_load_jobs_drops_retired_heartbeats_and_keeps_cron(monkeypatch, tmp_path):
    path = tmp_path / "cron_jobs.json"
    broken = {"name": "broken-job", "cron_expr": "not a cron", "prompt": "x", "paused": False}
    normal = {"name": "normal-job", "cron_expr": "0 9 * * *", "prompt": "daily", "paused": False}
    path.write_text(json.dumps([_HB, normal, broken]))
    monkeypatch.setattr(sched, "CRON_PATH", path)

    loaded = []

    def _add(name, expr, prompt, session_id=None, model="", extra_meta=None):
        if name == "broken-job":
            raise ValueError("bad cron expression")
        loaded.append(name)

    monkeypatch.setattr(sched, "_add_job_internal", _add)
    monkeypatch.setattr(sched, "_schedule_coalesced_catchup", lambda entries: None)
    sched._load_jobs()

    assert loaded == ["normal-job"]
    on_disk = json.loads(path.read_text())
    assert [j["name"] for j in on_disk] == ["normal-job", "broken-job"]


def test_load_jobs_leaves_the_file_alone_without_heartbeats(monkeypatch, tmp_path):
    path = tmp_path / "cron_jobs.json"
    raw = json.dumps([{"name": "normal-job", "cron_expr": "0 9 * * *", "prompt": "daily"}])
    path.write_text(raw)
    monkeypatch.setattr(sched, "CRON_PATH", path)
    monkeypatch.setattr(sched, "_add_job_internal", lambda *a, **kw: None)
    monkeypatch.setattr(sched, "_schedule_coalesced_catchup", lambda entries: None)
    sched._load_jobs()
    assert path.read_text() == raw


def test_heartbeat_tools_setting_and_api_are_gone():
    from api.app import app
    from config import Settings
    from core.tools.registry import ToolRegistry

    reg = ToolRegistry()
    sched.register(reg)
    for name in ("set_heartbeat", "clear_heartbeat", "list_heartbeats"):
        assert reg.get(name) is None, name
    assert not hasattr(Settings(), "heartbeats_enabled")
    assert not any(getattr(r, "path", "").endswith("/heartbeat") for r in app.routes)
