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


def test_retirement_moves_to_quarantine():
    _mk(vetting=False)
    for _ in range(5):
        _run("pin", True)
    stats = run_maintenance()
    assert stats["retired"] == ["pin"]
    assert load_canary("pin", base=_base()) is None  # out of the live suite
    marker = retired_dir(_base()) / "pin" / "retired.json"
    assert marker.is_file()
    assert "consecutive" in json.loads(marker.read_text())["reason"]
    # Quarantined canaries are invisible to scan (one level deep only).
    from core.canary.parser import scan_canaries

    assert scan_canaries(_base()) == []


def test_purge_after_retention_window(monkeypatch):
    monkeypatch.setattr("config.settings.canary_purge_after_days", 30)
    _mk(vetting=False)
    for _ in range(5):
        _run("pin", True)
    run_maintenance()
    marker = retired_dir(_base()) / "pin" / "retired.json"

    # Fresh quarantine: not purged.
    stats = run_maintenance()
    assert stats["purged"] == []
    assert marker.is_file()

    # Backdate past the window: purged.
    old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    marker.write_text(json.dumps({"retired_at": old, "reason": "r"}))
    stats = run_maintenance()
    assert stats["purged"] == ["pin"]
    assert not (retired_dir(_base()) / "pin").exists()


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
