"""Tests for the fallback-burn watch + cost_estimate pricing plumbing.

The watch encodes the 2026-08-19 silent-reroute signature (primary provider
wedged → every call billed to the fallback tier) as a standing snooze check.
Watch-only: it mints a notification, never touches routing.
"""

from __future__ import annotations

from core.llm.burnwatch import evaluate_fallback_burn
from core.llm.stream_ladder import estimate_cost


def _rows(primary_tokens: int, fallback_tokens: int) -> list[dict]:
    return [
        {"model": "local-27b", "provider": "openai", "total": primary_tokens, "calls": 10},
        {
            "model": "paid-remote",
            "provider": "openrouter",
            "routing_reason": "automatic_failover",
            "total": fallback_tokens,
            "calls": 4,
        },
    ]


def test_burn_fires_on_dominant_fallback_share():
    finding = evaluate_fallback_burn(_rows(10_000, 90_000), "paid-remote", 0.25, 50_000)
    assert finding is not None
    assert finding["model"] == "paid-remote"
    assert finding["share"] == 0.9
    assert finding["tokens"] == 90_000 and finding["total_tokens"] == 100_000
    assert finding["calls"] == 4


def test_burn_quiet_below_share_threshold():
    assert evaluate_fallback_burn(_rows(90_000, 10_000), "paid-remote", 0.25, 50_000) is None


def test_burn_quiet_below_volume_floor():
    """A quiet day where the only traffic happened to fail over is noise,
    not the incident signature."""
    assert evaluate_fallback_burn(_rows(1_000, 9_000), "paid-remote", 0.25, 50_000) is None


def test_burn_disabled_by_config():
    rows = _rows(0, 100_000)
    assert evaluate_fallback_burn(rows, "", 0.25, 0) is None  # no fallback model
    assert evaluate_fallback_burn(rows, "paid-remote", 0, 0) is None  # share=0 disables


def test_burn_zero_fallback_usage_is_quiet():
    assert evaluate_fallback_burn(_rows(100_000, 0), "paid-remote", 0.25, 50_000) is None


# ---------------------------------------------------------------------------
# cost_estimate pricing
# ---------------------------------------------------------------------------


def test_estimate_cost_priced_model(monkeypatch):
    monkeypatch.setattr("config.settings.model_prices", {"paid-remote": {"in": 3.0, "out": 15.0}})
    # 1M prompt at $3 + 100k completion at $15 = 3.0 + 1.5
    assert estimate_cost("paid-remote", 1_000_000, 100_000) == 4.5


def test_estimate_cost_unpriced_model_stays_null(monkeypatch):
    monkeypatch.setattr("config.settings.model_prices", {"paid-remote": {"in": 3.0, "out": 15.0}})
    assert estimate_cost("local-27b", 1_000_000, 100_000) is None
    monkeypatch.setattr("config.settings.model_prices", {})
    assert estimate_cost("paid-remote", 1_000_000, 100_000) is None


def test_estimate_cost_tolerates_junk_config(monkeypatch):
    monkeypatch.setattr("config.settings.model_prices", {"m": "not-a-dict", "n": {"in": "abc"}})
    assert estimate_cost("m", 1000, 1000) is None
    assert estimate_cost("n", 1000, 1000) is None


def test_explicit_and_unknown_model_usage_never_prove_failover():
    for reason in ("selected", "unknown", None):
        rows = [{"model": "paid-remote", "total": 917448, "calls": 10, "routing_reason": reason}]
        assert evaluate_fallback_burn(rows, "paid-remote", 0.25, 0) is None


def test_historical_fallback_still_counted_after_setting_changes():
    rows = _rows(10000, 90000)
    assert evaluate_fallback_burn(rows, "new-backup", 0.25, 0)["tokens"] == 90000


def test_window_excludes_outside_rows_and_preserves_unknown_routes():
    from db import models as db
    from db.database import connect_sessions

    sid = db.create_session()
    for timestamp, reason in [
        ("2026-10-25 16:00:00", "selected"),
        ("2026-10-26 15:59:59", "automatic_failover"),
        ("2026-10-26 16:00:01", "automatic_failover"),
    ]:
        db.add_token_usage(
            sid,
            model="paid",
            total_tokens=100,
            routing_reason=reason,
            requested_model="local",
            failure_reason="timeout" if reason != "selected" else None,
        )
        with connect_sessions() as conn:
            conn.execute("UPDATE token_usage SET created_at=? WHERE id=(SELECT max(id) FROM token_usage)", (timestamp,))
    rows = db.token_usage_by_model_since("2026-10-25T16:00:01+00:00", "2026-10-26T16:00:00+00:00")
    assert len(rows) == 1 and rows[0]["total"] == 100
    assert rows[0]["requested_model"] == "local" and rows[0]["failure_reason"] == "timeout"


def test_migration_keeps_history_unknown_and_resolves_legacy_alert():
    from db import models as db
    from db.database import MIGRATIONS, _run_migrations, connect_sessions

    sid = db.create_session()
    db.add_token_usage(sid, model="paid", total_tokens=917448)
    nid = db.add_notification("", "Fallback model is carrying the load", category="system.fallback_burn")
    # Recreate the v44 boundary, then apply v45 exactly as an existing install does.
    with connect_sessions() as conn:
        for col in ("requested_model", "routing_reason", "failure_reason"):
            conn.execute(f"ALTER TABLE token_usage DROP COLUMN {col}")
        conn.execute("DROP TABLE model_selection_events")
        conn.execute("UPDATE schema_meta SET value='44' WHERE key='schema_version'")
        _run_migrations(conn)
        row = conn.execute("SELECT * FROM token_usage").fetchone()
        assert row["routing_reason"] == "unknown" and row["requested_model"] is None
        assert conn.execute("SELECT resolved_at FROM notifications WHERE id=?", (nid,)).fetchone()[0]
        _run_migrations(conn)  # idempotent startup
