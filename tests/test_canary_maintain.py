"""Tests for core/canary/maintain.py — suite auto-maintenance."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.canary.maintain import retired_dir, run_maintenance
from core.canary.parser import load_canary
from core.canary.propose import materialize_canary
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
    monkeypatch.setattr("config.settings.canary_enabled", True)
    monkeypatch.setattr("config.settings.canary_auto_maintain", True)
    monkeypatch.setattr("config.settings.canary_vetting_runs", 3)
    monkeypatch.setattr("config.settings.canary_retire_after_passes", 5)


def _mk(name: str = "pin", vetting: bool = True) -> None:
    got, err = materialize_canary(dict(_SPEC, name=name), vetting=vetting)
    assert got == name, err


def _run(name: str, passed: bool) -> None:
    db.add_canary_run(task=name, trigger="scheduled", session_id=None, gate_results_json="[]", passed=passed)


def _base() -> Path:
    from config import settings

    return Path(settings.canaries_dir)


def test_promotion_after_consistent_passes():
    _mk()
    for _ in range(3):
        _run("pin", True)
    stats = run_maintenance()
    assert stats["promoted"] == ["pin"]
    c = load_canary("pin", base=_base())
    assert c.flaky is False and "vetting" not in c.tags
    assert "auto-admitted" in c.tags  # provenance survives promotion


def test_vetting_needs_enough_runs():
    _mk()
    _run("pin", True)
    stats = run_maintenance()
    assert stats["promoted"] == []
    assert load_canary("pin", base=_base()).flaky is True


def test_mixed_vetting_settles_flaky():
    _mk()
    for outcome in (False, True, True):  # oldest first: fail, then passes
        _run("pin", outcome)
    stats = run_maintenance()
    assert stats["settled_flaky"] == ["pin"]
    c = load_canary("pin", base=_base())
    assert c.flaky is True and "vetting" not in c.tags


def test_goodhart_lock_failing_canary_untouchable():
    """Latest run failed → no mutation of any kind, even a fully-vetted one."""
    _mk()
    for _ in range(4):
        _run("pin", True)
    _run("pin", False)  # newest
    stats = run_maintenance()
    assert all(not v for v in stats.values())
    c = load_canary("pin", base=_base())
    assert c.flaky is True and "vetting" in c.tags  # untouched


def test_flap_detection_tags_established_canary():
    _mk(vetting=False)
    for outcome in (True, False, True, False, True, False, True, True):  # newest last
        _run("pin", outcome)
    stats = run_maintenance()
    assert stats["flaky_tagged"] == ["pin"]
    assert load_canary("pin", base=_base()).flaky is True


def test_long_green_demotes_cadence_and_stays_in_the_suite():
    """A long-green canary is the tripwire's baseline, not dead weight: it
    must stay scannable and keep producing scheduled runs, just fewer."""
    _mk(vetting=False)
    for _ in range(5):
        _run("pin", True)
    stats = run_maintenance()
    assert stats["demoted"] == [{"name": "pin", "cadence": 2}]
    c = load_canary("pin", base=_base())
    assert c is not None and c.cadence == 2
    assert not (retired_dir(_base()) / "pin").exists()

    # Still green after more runs: cadence backs off further, still present.
    for _ in range(5):
        _run("pin", True)
    stats = run_maintenance()
    assert stats["demoted"] == [{"name": "pin", "cadence": 4}]
    assert load_canary("pin", base=_base()).cadence == 4


def test_demotion_capped_so_a_canary_never_leaves_the_baseline():
    from core.canary.parser import MAX_CADENCE

    _mk(vetting=False)
    md = _base() / "pin" / "CANARY.md"
    md.write_text(md.read_text().replace("flaky: false", f"flaky: false\ncadence: {MAX_CADENCE}", 1))
    for _ in range(5):
        _run("pin", True)
    stats = run_maintenance()
    assert stats["demoted"] == []  # already at the ceiling — no further backoff
    assert load_canary("pin", base=_base()).cadence == MAX_CADENCE


def test_purge_after_retention_window(monkeypatch):
    """Auto-maintenance no longer quarantines; the purge still drains what
    earlier versions (or a human) left in .retired/."""
    monkeypatch.setattr("config.settings.canary_purge_after_days", 30)
    quarantine = retired_dir(_base()) / "pin"
    quarantine.mkdir(parents=True)
    marker = quarantine / "retired.json"
    marker.write_text(json.dumps({"retired_at": datetime.now(timezone.utc).isoformat(), "reason": "r"}))

    # Fresh quarantine: not purged.
    stats = run_maintenance()
    assert stats["purged"] == []
    assert marker.is_file()

    # Backdate past the window: purged.
    old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    marker.write_text(json.dumps({"retired_at": old, "reason": "r"}))
    stats = run_maintenance()
    assert stats["purged"] == ["pin"]
    assert not quarantine.exists()


def test_review_bump_on_stale_healthy_canary():
    _mk(vetting=False)
    _run("pin", True)
    md = _base() / "pin" / "CANARY.md"
    old = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%d")
    md.write_text(md.read_text().replace(datetime.now(timezone.utc).strftime("%Y-%m-%d"), old))
    stats = run_maintenance()
    assert stats["reviewed"] == ["pin"]
    c = load_canary("pin", base=_base())
    assert c.last_reviewed == datetime.now(timezone.utc).strftime("%Y-%m-%d")


def test_disabled_is_inert(monkeypatch):
    monkeypatch.setattr("config.settings.canary_auto_maintain", False)
    _mk()
    for _ in range(3):
        _run("pin", True)
    stats = run_maintenance()
    assert all(not v for v in stats.values())


# ---------------------------------------------------------------------------
# Suite health — the absolute check the relative signals cannot make
# ---------------------------------------------------------------------------


def _noop_run(name: str) -> None:
    """A run where the agent never executed: no tokens, sub-second, failed."""
    db.add_canary_run(
        task=name,
        trigger="scheduled",
        session_id=None,
        gate_results_json="[]",
        passed=False,
        tokens=0,
        duration_s=0.03,
    )


def _real_fail(name: str) -> None:
    """An honest failure: the agent ran, spent tokens, and missed the gate."""
    db.add_canary_run(
        task=name,
        trigger="scheduled",
        session_id=None,
        gate_results_json="[]",
        passed=False,
        tokens=54000,
        duration_s=120.0,
    )


def test_health_flags_chronically_failing_canary():
    _mk("solo", vetting=False)
    for _ in range(3):
        _real_fail("solo")
    stats = run_maintenance()
    assert stats["unhealthy"] == ["solo"]
    # The Goodhart lock still holds: reporting must not mutate the canary.
    assert load_canary("solo", base=_base()).flaky is False


def test_health_ignores_a_canary_that_still_passes_sometimes():
    _mk("flappy", vetting=False)
    _real_fail("flappy")
    _run("flappy", True)
    _real_fail("flappy")
    stats = run_maintenance()
    assert stats["unhealthy"] == []


def test_health_needs_enough_scheduled_history():
    _mk("young", vetting=False)
    for _ in range(2):  # below the 3-run health window
        _real_fail("young")
    assert run_maintenance()["unhealthy"] == []


def test_health_separates_harness_break_from_quality_regression():
    """Zero tokens + sub-second + failing == the agent never ran.

    This is the 2026-08 blackout signature: gates scored against the seeded
    fixtures. It must raise at high urgency and say so, because the remedy
    is nothing like the remedy for a genuine regression.
    """
    _mk("broken", vetting=False)
    for _ in range(3):
        _noop_run("broken")
    run_maintenance()
    notes = db.get_notifications()
    hit = [n for n in notes if "not running" in n["title"]]
    assert hit and hit[0]["urgency"] == "high"
    assert "harness failure" in hit[0]["body"]


def test_health_alert_does_not_renotify_on_every_sweep():
    _mk("solo", vetting=False)
    for _ in range(3):
        _real_fail("solo")
    run_maintenance()
    first = len(db.get_notifications())
    run_maintenance()
    assert len(db.get_notifications()) == first  # deduped by day + signature


def test_health_recovery_clears_the_alert_state():
    _mk("solo", vetting=False)
    for _ in range(3):
        _real_fail("solo")
    run_maintenance()
    for _ in range(3):
        _run("solo", True)
    assert run_maintenance()["unhealthy"] == []


def test_frontmatter_rewrite_preserves_body_and_unknown_keys():
    _mk()
    md = _base() / "pin" / "CANARY.md"
    # Inject a key the maintenance code knows nothing about.
    md.write_text(md.read_text().replace("flaky: true", "flaky: true\ncustom_note: keep-me", 1))
    assert "keep-me" in md.read_text()
    for _ in range(3):
        _run("pin", True)
    stats = run_maintenance()
    assert stats["promoted"] == ["pin"]
    after = md.read_text()
    assert "custom_note: keep-me" in after
    assert "test canary" in after  # body preserved
