"""Canary and skill-verify producers go through core/notices.py (2026-10).

Before the registry these sites wrote bell rows directly, each picking its own
urgency: every contaminated run, retired probe, auto-admission, stale nudge and
maintenance summary landed in the bell, which is why ~95% of bell items were
self-maintenance receipts. Pinned here: each producer's category and the tier
it lands in, that the two pre-existing dedup keys still write (and honour) the
same `notify_dedup:<date>:<key>` markers, and that the two bell items that
describe a CONDITION (parked, suite unhealthy) close themselves once it clears.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from core.canary import contamination
from core.canary.contamination import contamination_record
from core.canary.maintain import _CONTAMINATION_WINDOW, _report_suite_health, run_maintenance
from core.canary.parser import load_canary
from core.canary.propose import materialize_canary, queue_canary_proposals
from db import models as db

_SPEC = {
    "name": "pin",
    "prompt": "Create out.txt containing DONE.",
    "gates": [{"name": "out", "command": "grep -qx DONE out.txt", "watch_paths": []}],
    "rationale": "test canary",
}


@pytest.fixture(autouse=True)
def _canaries_tmp(monkeypatch, tmp_path):
    monkeypatch.setattr("config.settings.canaries_dir", str(tmp_path / "canaries"))
    monkeypatch.setattr("config.settings.skills_dir", str(tmp_path / "skills"))
    monkeypatch.setattr("config.settings.canary_enabled", True)
    monkeypatch.setattr("config.settings.canary_auto_maintain", True)
    monkeypatch.setattr("config.settings.canary_auto_admit", False)
    monkeypatch.setattr("config.settings.canary_vetting_runs", 3)
    monkeypatch.setattr("config.settings.canary_park_after_passes", 5)


def _base() -> Path:
    from config import settings

    return Path(settings.canaries_dir)


def _mk(name: str) -> None:
    got, err = materialize_canary(dict(_SPEC, name=name), vetting=False)
    assert got == name, err


def _contaminated_run(name: str) -> None:
    db.add_canary_run(
        task=name,
        trigger="scheduled",
        session_id=None,
        gate_results_json=json.dumps([contamination_record(["read outside the workspace: /app/data/x"])]),
        passed=False,
        outcome="contaminated",
    )


def _rows(category: str) -> list[dict]:
    return [n for n in db.list_notifications("log", limit=500) if n["category"] == category]


def _bell(category: str) -> list[dict]:
    return [n for n in db.get_notifications() if n["category"] == category]


def _today_marker(key: str) -> str:
    return f"notify_dedup:{datetime.now(timezone.utc).strftime('%Y-%m-%d')}:{key}"


def _park(name: str) -> None:
    _mk(name)
    for _ in range(_CONTAMINATION_WINDOW):
        _contaminated_run(name)
    run_maintenance()
    assert load_canary(name, base=_base()).parked is True


# ---------------------------------------------------------------------------
# category + tier per producer
# ---------------------------------------------------------------------------


def test_a_contaminated_run_is_a_log_line_with_its_session():
    contamination.notify("leaky", "sess-abcdef123456", ["memory tool called: recall"])
    rows = _rows("canary.contaminated")
    assert len(rows) == 1 and rows[0]["tier"] == "log"
    assert rows[0]["subject"] == "leaky" and rows[0]["session_id"] == "sess-abcdef123456"
    assert _bell("canary.contaminated") == []


def test_a_retired_probe_is_a_log_line():
    _mk("probe-x")
    md = _base() / "probe-x" / "CANARY.md"
    md.write_text(md.read_text().replace("flaky: false", "flaky: false\nmax_runs: 1", 1))
    db.add_canary_run(task="probe-x", trigger="scheduled", session_id=None, gate_results_json="[]", passed=True)
    run_maintenance()
    rows = _rows("canary.probe_retired")
    assert len(rows) == 1 and rows[0]["tier"] == "log" and rows[0]["subject"] == "probe-x"


def test_park_is_a_bell_item_on_the_canary_tab():
    _park("evidence-gate")
    rows = _bell("canary.parked")
    assert len(rows) == 1 and rows[0]["subject"] == "evidence-gate"
    assert rows[0]["link"] == {"kind": "tab", "tab": "canary"}


def test_suite_health_chronic_is_log_and_noop_blackout_are_bell():
    _report_suite_health({"chronic": ["solo"], "noop": [], "blackout": False})
    assert [r["tier"] for r in _rows("canary.suite_chronic")] == ["log"]

    _report_suite_health({"chronic": ["a", "b"], "noop": ["a", "b"], "blackout": True})
    rows = _bell("canary.suite_unhealthy")
    assert len(rows) == 1 and rows[0]["subject"] == "suite" and "not running" in rows[0]["title"]

    _report_suite_health({"chronic": ["a", "b", "c"], "noop": [], "blackout": True})
    # Same open (category, subject) row coalesces rather than stacking.
    rows = _bell("canary.suite_unhealthy")
    assert len(rows) == 1 and "every scored canary" in rows[0]["title"]


def test_maintenance_summary_is_a_log_line():
    _mk("pin")
    for _ in range(5):
        db.add_canary_run(task="pin", trigger="scheduled", session_id=None, gate_results_json="[]", passed=True)
    run_maintenance()  # parks the long-green canary -> one mutation summary
    rows = _rows("canary.maintenance")
    assert len(rows) == 1 and rows[0]["tier"] == "log"
    assert _bell("canary.maintenance") == []


def test_auto_admission_is_a_log_line(monkeypatch):
    monkeypatch.setattr("config.settings.canary_auto_admit", True)
    monkeypatch.setattr("core.extensions.scheduling.enqueue_manual_canary", lambda name: True)
    assert queue_canary_proposals([dict(_SPEC, name="admitted")], "refine", session_id="s-1") == 1
    rows = _rows("canary.auto_admitted")
    assert len(rows) == 1 and rows[0]["tier"] == "log" and rows[0]["subject"] == "admitted"


async def test_the_stale_nudge_is_a_log_line_and_keeps_its_marker(monkeypatch):
    from core.retention import nudge_stale_canaries

    stale = SimpleNamespace(name="old-canary", last_reviewed="2025-01-01")
    monkeypatch.setattr("core.canary.scan_canaries", lambda *a, **k: [stale])
    assert await nudge_stale_canaries() == 1
    assert await nudge_stale_canaries() == 0
    rows = _rows("canary.stale")
    assert len(rows) == 1 and rows[0]["tier"] == "log"
    assert db.get_snooze_state("canary_stale_notified:old-canary:2025-01-01")


def test_an_unsafe_verify_block_is_a_bell_item_once_per_content():
    from core.canary.skill_verify import _notify_unsafe_once

    _notify_unsafe_once("my-skill", "d1", "gate command uses a pipe")
    _notify_unsafe_once("my-skill", "d1", "gate command uses a pipe")
    rows = _bell("skills.verify_unsafe")
    assert len(rows) == 1 and rows[0]["subject"] == "my-skill"
    _notify_unsafe_once("my-skill", "d2", "gate command uses a pipe")
    assert len(_rows("skills.verify_unsafe")) == 2


# ---------------------------------------------------------------------------
# dedup-marker continuity (deploy day must not re-announce)
# ---------------------------------------------------------------------------


def test_a_park_marker_written_by_the_old_code_still_suppresses():
    db.set_snooze_state(_today_marker("canary_contaminated_park:evidence-gate"), "1")
    _park("evidence-gate")
    assert _rows("canary.parked") == []


def test_the_park_writes_the_same_marker_key():
    _park("evidence-gate")
    assert db.get_snooze_state(_today_marker("canary_contaminated_park:evidence-gate"))


def test_the_maintenance_summary_keeps_its_sha_keyed_marker():
    _mk("pin")
    for _ in range(5):
        db.add_canary_run(task="pin", trigger="scheduled", session_id=None, gate_results_json="[]", passed=True)
    run_maintenance()
    body = _rows("canary.maintenance")[0]["body"]
    summary = body.split(". Parked canaries", 1)[0]
    key = "canary_maintain:" + hashlib.sha1(summary.encode()).hexdigest()[:12]
    assert db.get_snooze_state(_today_marker(key))


# ---------------------------------------------------------------------------
# condition rows close themselves
# ---------------------------------------------------------------------------


def test_a_red_run_unpark_resolves_the_parked_item():
    _park("evidence-gate")
    assert len(_bell("canary.parked")) == 1
    db.add_canary_run(task="evidence-gate", trigger="scheduled", session_id=None, gate_results_json="[]", passed=False)
    stats = run_maintenance()
    assert stats["unparked"] == ["evidence-gate"]
    assert _bell("canary.parked") == []
    assert len(_rows("canary.parked")) == 1  # the log keeps the history


async def test_a_manual_unpark_resolves_the_parked_item():
    from api.routers import canary as canary_router

    _park("evidence-gate")
    app = FastAPI()
    app.include_router(canary_router.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.patch("/api/canary/evidence-gate", json={"parked": False})
    assert resp.status_code == 200 and resp.json()["changed"] is True
    assert _bell("canary.parked") == []


def test_a_healthy_suite_resolves_the_unhealthy_item():
    _report_suite_health({"chronic": ["a", "b"], "noop": ["a", "b"], "blackout": True})
    assert len(_bell("canary.suite_unhealthy")) == 1
    _report_suite_health({"chronic": [], "noop": [], "blackout": False})
    assert _bell("canary.suite_unhealthy") == []


def test_a_merely_chronic_suite_also_closes_the_unhealthy_item():
    """Down from 'agent not running' to one honest failure: the harness-level
    alarm is over even though a task-level log line remains."""
    _report_suite_health({"chronic": ["a", "b"], "noop": ["a", "b"], "blackout": True})
    _report_suite_health({"chronic": ["a"], "noop": [], "blackout": False})
    assert _bell("canary.suite_unhealthy") == []
    assert len(_rows("canary.suite_chronic")) == 1
