"""Item #1: post-mortem TTL cleanup helper + snooze activity."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from core.snooze import SnoozeRunner
from db import models as db
from db.database import connect_sessions


def _seed_pm(session_id: str, *, created_at: str, synthesized_at: str | None) -> str:
    pm_id = db.add_post_mortem(
        session_id=session_id,
        attempt=1,
        verdict="pass",
        failure_cause="none",
        confidence=0.5,
        reflect_model="test",
        reflect_latency_ms=1,
        scout_viability="verified",
        execution_mode="inline",
        payload_json=json.dumps({}),
    )
    # Backdate / set synthesized flag directly via SQL.
    with connect_sessions() as conn:
        conn.execute(
            "UPDATE post_mortems SET created_at = ?, synthesized_at = ? WHERE id = ?",
            (created_at, synthesized_at, pm_id),
        )
    return pm_id


def test_deletes_synthesized_and_old_rows_only():
    sid = db.create_session(title="pm-ttl")
    now = datetime.now(timezone.utc)
    old_iso = (now - timedelta(days=120)).isoformat()
    new_iso = now.isoformat()

    # Old + synthesized → should be deleted.
    pm_old_synth = _seed_pm(sid, created_at=old_iso, synthesized_at=old_iso)
    # Old but NOT synthesized → preserved (may still need attribution).
    pm_old_unsynth = _seed_pm(sid, created_at=old_iso, synthesized_at=None)
    # New + synthesized → preserved (within retention window).
    pm_new_synth = _seed_pm(sid, created_at=new_iso, synthesized_at=new_iso)

    cutoff = (now - timedelta(days=90)).isoformat()
    deleted = db.delete_old_post_mortems(cutoff)
    assert deleted == 1

    assert db.get_post_mortem(pm_old_synth) is None
    assert db.get_post_mortem(pm_old_unsynth) is not None
    assert db.get_post_mortem(pm_new_synth) is not None


def test_returns_zero_when_nothing_to_delete():
    cutoff = "1900-01-01T00:00:00+00:00"
    assert db.delete_old_post_mortems(cutoff) == 0


@pytest.mark.asyncio
async def test_snooze_activity_runs_without_error(monkeypatch):
    monkeypatch.setattr("config.settings.post_mortem_retention_days", 90)
    sid = db.create_session(title="pm-ttl-snooze")
    old_iso = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    pm_old = _seed_pm(sid, created_at=old_iso, synthesized_at=old_iso)

    runner = SnoozeRunner()
    await runner._cleanup_post_mortems()

    # Old synthesized row should be gone.
    assert db.get_post_mortem(pm_old) is None
    assert runner._stats.get("post_mortems_pruned", 0) >= 1
