"""Tests for core/canary/maintain.py — the suite file helpers that outlived
auto-maintenance (retired in 3.2): frontmatter rewrites, retirement into the
.retired/ quarantine, and the quarantine purge snooze retention runs."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.canary import maintain
from core.canary.maintain import _rewrite_frontmatter, purge_quarantine, retire_canary, retired_dir
from core.canary.parser import load_canary
from core.canary.propose import materialize_canary

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


def _base() -> Path:
    from config import settings

    return Path(settings.canaries_dir)


def _mk(name: str = "pin") -> None:
    got, err = materialize_canary(dict(_SPEC, name=name))
    assert got == name, err


def test_auto_maintenance_is_gone():
    for gone in ("run_maintenance", "check_suite_health", "_maintain_one", "_retire_exhausted_probes"):
        assert not hasattr(maintain, gone), gone
    from config import Settings

    for key in ("canary_auto_maintain", "canary_vetting_runs", "canary_park_after_passes"):
        assert key not in Settings.__dataclass_fields__, key


def test_retire_moves_to_quarantine_with_a_marker():
    _mk()
    c = load_canary("pin", base=_base())
    assert retire_canary(c, _base(), reason="r", by="user")
    assert load_canary("pin", base=_base()) is None
    marker = retired_dir(_base()) / "pin" / "retired.json"
    assert json.loads(marker.read_text())["by"] == "user"


def test_purge_after_retention_window(monkeypatch):
    monkeypatch.setattr("config.settings.canary_purge_after_days", 30)
    quarantine = retired_dir(_base()) / "pin"
    quarantine.mkdir(parents=True)
    marker = quarantine / "retired.json"
    marker.write_text(json.dumps({"retired_at": datetime.now(timezone.utc).isoformat(), "reason": "r"}))

    # Fresh quarantine: not purged.
    assert purge_quarantine(_base()) == []
    assert marker.is_file()

    # Backdate past the window: purged.
    old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    marker.write_text(json.dumps({"retired_at": old, "reason": "r"}))
    assert purge_quarantine(_base()) == ["pin"]
    assert not quarantine.exists()


def test_purge_leaves_an_unreadable_marker_for_a_human():
    quarantine = retired_dir(_base()) / "odd"
    quarantine.mkdir(parents=True)
    (quarantine / "retired.json").write_text("not json")
    assert purge_quarantine(_base()) == []
    assert quarantine.exists()


async def test_snooze_retention_rung_drains_the_quarantine(monkeypatch):
    """12c owns the purge now that 12d (auto-maintenance) is gone."""
    from core.snooze import SnoozeRunner

    monkeypatch.setattr("config.settings.canary_purge_after_days", 1)
    quarantine = retired_dir(_base()) / "gone"
    quarantine.mkdir(parents=True)
    old = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    (quarantine / "retired.json").write_text(json.dumps({"retired_at": old}))

    runner = SnoozeRunner.__new__(SnoozeRunner)
    runner._stats = {}
    await SnoozeRunner._cleanup_canary_runs(runner)
    assert not quarantine.exists()
    assert runner._stats.get("canaries_purged") == 1


def test_frontmatter_rewrite_preserves_body_and_unknown_keys():
    _mk()
    md = _base() / "pin" / "CANARY.md"
    md.write_text(md.read_text().replace("timeout: 600", "timeout: 600\ncustom_note: keep-me", 1))
    assert _rewrite_frontmatter(md, {"last_reviewed": "2026-01-01"})
    after = md.read_text()
    assert "custom_note: keep-me" in after
    assert "test canary" in after  # body preserved
    assert load_canary("pin", base=_base()).last_reviewed == "2026-01-01"


def test_frontmatter_rewrite_refuses_a_broken_result():
    _mk()
    md = _base() / "pin" / "CANARY.md"
    before = md.read_text()
    assert _rewrite_frontmatter(md, {"gates": []}) is False
    assert md.read_text() == before
