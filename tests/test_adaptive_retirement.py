"""Tests for the v3.1 value-based retirement sweep + notification hygiene."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.adaptive.retire import _USAGE_EPOCH_KEY, retire_unused_entries
from db import models as db


@pytest.fixture(autouse=True)
def _adaptive_on(monkeypatch):
    monkeypatch.setattr("config.settings.adaptive_enabled", True)
    monkeypatch.setattr("config.settings.adaptive_usage_retire_days", 45)
    monkeypatch.setattr("config.settings.adaptive_prompt_note_ttl_days", 90)


def _entry(entry_id: str, kind: str = "routing_hint", source: str = "refine", age_days: int = 60) -> None:
    stamp = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
    db.adaptive_put_entry(
        {
            "id": entry_id,
            "kind": kind,
            "scope": "global",
            "title": entry_id,
            "content": "prefer x when y",
            "risk": "low",
            "version": 1,
            "status": "active",
            "source": source,
            "created_at": stamp,
            "updated_at": stamp,
        }
    )


def _backdate_epoch(days: int) -> None:
    db.set_snooze_state(_USAGE_EPOCH_KEY, (datetime.now(timezone.utc) - timedelta(days=days)).isoformat())


def test_epoch_grace_protects_pre_instrumentation_entries():
    """CRITICAL: the sweep's first run stamps the epoch and retires NOTHING —
    'never used' is only meaningful for time observed, and without this
    every pre-instrumentation entry would mass-retire on day one."""
    _entry("old-but-unobserved", age_days=200)
    out = retire_unused_entries()
    assert out["retired"] == []
    assert db.get_snooze_state(_USAGE_EPOCH_KEY)  # clock started
    # Still within grace on the next pass.
    assert retire_unused_entries()["retired"] == []


def test_unused_entry_retires_after_observed_window():
    _entry("dead-weight", age_days=60)
    _backdate_epoch(50)
    out = retire_unused_entries()
    assert out["retired"] == ["dead-weight"]
    assert "no recorded use" in out["reasons"]["dead-weight"]
    row = [e for e in db.adaptive_list_entries(kind="routing_hint", status=None) if e["id"] == "dead-weight"][0]
    assert row["status"] == "deleted"  # soft delete — journaled, rollbackable
    events = db.adaptive_list_events(entry_id="dead-weight")
    assert any(ev["action"] == "delete" for ev in events)


def test_used_entry_survives():
    _entry("earning-its-keep", age_days=60)
    _backdate_epoch(50)
    db.upsert_signal("adaptive_entry", "earning-its-keep")
    assert retire_unused_entries()["retired"] == []


def test_protected_sources_are_exempt():
    """Candor runs its own lifecycle; a human's entry is never second-guessed
    by a counter."""
    _entry("tool-x-degraded", source="candor", age_days=120)
    _entry("calvins-note", kind="prompt_note", source="user", age_days=200)
    _backdate_epoch(120)
    assert retire_unused_entries()["retired"] == []


def test_young_entries_are_exempt():
    _entry("fresh", age_days=10)
    _backdate_epoch(120)
    assert retire_unused_entries()["retired"] == []


def test_prompt_note_ttl_retires_even_a_used_note():
    """prompt_note has no producer-side retirement loop — the TTL is its
    backstop; a still-useful note re-mints cheaply."""
    _entry("stale-note", kind="prompt_note", age_days=100)
    _backdate_epoch(120)
    db.upsert_signal("adaptive_entry", "stale-note")  # used, but past TTL
    out = retire_unused_entries()
    assert out["retired"] == ["stale-note"]
    assert "TTL" in out["reasons"]["stale-note"]


def test_zero_settings_disable_the_sweep(monkeypatch):
    monkeypatch.setattr("config.settings.adaptive_usage_retire_days", 0)
    monkeypatch.setattr("config.settings.adaptive_prompt_note_ttl_days", 0)
    _entry("kept-by-config", age_days=300)
    _backdate_epoch(300)
    assert retire_unused_entries()["retired"] == []


# ---------------------------------------------------------------------------
# Notification hygiene (system-wide)
# ---------------------------------------------------------------------------


def test_notification_dedup_key_swallows_repeats_only():
    first = db.add_notification(title="cap reached", body="same text", dedup_key="cap:test")
    second = db.add_notification(title="cap reached", body="same text", dedup_key="cap:test")
    assert first and second == ""
    assert len([n for n in db.get_notifications() if n["title"] == "cap reached"]) == 1
    # No key = old behavior, always inserts.
    db.add_notification(title="cap reached", body="same text")
    assert len([n for n in db.get_notifications() if n["title"] == "cap reached"]) == 2


def test_prune_notifications_respects_retention():
    from core import retention

    nid = db.add_notification(title="old news", body="x")
    from db.database import connect_sessions

    with connect_sessions() as conn:
        conn.execute("UPDATE notifications SET created_at = '2020-01-01T00:00:00+00:00' WHERE id = ?", (nid,))
    db.add_notification(title="fresh news", body="y")
    assert retention.prune_notifications(30) == 1
    titles = [n["title"] for n in db.get_notifications()]
    assert "fresh news" in titles and "old news" not in titles
    # 0 disables.
    assert retention.prune_notifications(0) == 0


def test_get_notifications_is_bounded():
    for i in range(5):
        db.add_notification(title=f"n{i}", body="x")
    assert len(db.get_notifications(limit=3)) == 3
