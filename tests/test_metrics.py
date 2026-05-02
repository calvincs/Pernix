"""Phase 4b: metrics reporter — counts, percentiles, and report formatting."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from core import metrics
from core.metrics import _percentile, compute, format_report
from db import models as db

# ---------------------------------------------------------------------------
# _percentile helper
# ---------------------------------------------------------------------------


def test_percentile_empty_returns_zero():
    assert _percentile([], 50) == 0.0


def test_percentile_median():
    assert _percentile([10, 20, 30, 40, 50], 50) == 30.0


def test_percentile_p95_interpolates():
    # 100 items, p95 ~ 95-96 range
    data = list(range(1, 101))
    result = _percentile(data, 95)
    assert 94 <= result <= 96


# ---------------------------------------------------------------------------
# compute() — windowing and aggregation
# ---------------------------------------------------------------------------


def _seed_pm(
    session_id: str,
    verdict="pass",
    failure_cause="none",
    confidence=0.9,
    exec_mode="inline",
    viability="verified",
    latency=50,
    deliverables=None,
):
    payload = {"deliverables": deliverables or []}
    return db.add_post_mortem(
        session_id=session_id,
        attempt=1,
        verdict=verdict,
        failure_cause=failure_cause,
        confidence=confidence,
        reflect_model="m",
        reflect_latency_ms=latency,
        scout_viability=viability,
        execution_mode=exec_mode,
        payload_json=json.dumps(payload),
    )


def test_compute_counts_verdicts_in_window():
    sid = db.create_session(title="metrics-window")
    _seed_pm(sid, verdict="pass")
    _seed_pm(sid, verdict="pass")
    _seed_pm(sid, verdict="retry", failure_cause="scout", confidence=0.8)

    r = compute(days=1)
    # May include other tests' sessions — just assert totals have at least these
    assert r.verdicts.get("pass", 0) >= 2
    assert r.verdicts.get("retry", 0) >= 1
    assert r.failure_causes.get("scout", 0) >= 1


def test_compute_excludes_post_mortems_outside_window():
    sid = db.create_session(title="outside-window")
    _seed_pm(sid, verdict="pass")

    # Very narrow future window — this PM was created "now" so should fall OUT
    # of a 1-second window starting in the future.
    future = datetime.now(timezone.utc) + timedelta(days=1)
    result = compute(
        since_iso=future.isoformat(),
        until_iso=(future + timedelta(hours=1)).isoformat(),
    )
    assert result.post_mortems_total == 0


def test_compute_reports_viability_and_execution_mode_distributions():
    sid = db.create_session(title="metrics-distributions")
    _seed_pm(sid, verdict="pass", viability="verified", exec_mode="inline")
    _seed_pm(sid, verdict="pass", viability="unverified", exec_mode="workers")
    _seed_pm(sid, verdict="pass", viability="verified", exec_mode="tasks")

    r = compute(days=1)
    assert r.viability.get("verified", 0) >= 2
    assert r.viability.get("unverified", 0) >= 1
    assert r.execution_modes.get("workers", 0) >= 1


def test_compute_aggregates_deliverables_from_payload():
    sid = db.create_session(title="metrics-deliv")
    _seed_pm(
        sid,
        verdict="pass",
        deliverables=[
            {"description": "write x", "status": "met"},
            {"description": "write y", "status": "unmet"},
        ],
    )
    r = compute(days=1)
    assert r.deliverables_total >= 2
    assert r.deliverables_status.get("met", 0) >= 1
    assert r.deliverables_status.get("unmet", 0) >= 1


def test_compute_reflect_latency_percentiles():
    sid = db.create_session(title="metrics-latency")
    # Make latencies large enough and distinct to compute meaningful percentiles.
    for lat in [100, 200, 300, 400, 500, 600, 700, 800, 900, 1000]:
        _seed_pm(sid, verdict="pass", latency=lat)
    r = compute(days=1)
    assert r.reflect_latency_p50 > 0
    assert r.reflect_latency_p95 >= r.reflect_latency_p50


def test_compute_confidence_mean_by_verdict():
    sid = db.create_session(title="metrics-conf")
    _seed_pm(sid, verdict="pass", confidence=0.9)
    _seed_pm(sid, verdict="pass", confidence=0.7)
    _seed_pm(sid, verdict="retry", failure_cause="scout", confidence=0.5)
    r = compute(days=1)
    # Not guaranteed to isolate just our seeded rows from other tests,
    # but confidence_mean_by_verdict should exist for pass + retry.
    assert "pass" in r.confidence_mean_by_verdict
    assert 0 <= r.confidence_mean_by_verdict["pass"] <= 1


def test_compute_captures_signal_snapshot():
    # Seed a fresh signal and confirm it appears in the (non-windowed) snapshot.
    db.delete_signal("skill", "metrics-sig-fresh")
    db.upsert_signal("skill", "metrics-sig-fresh", delta_successes=5)
    r = compute(days=1)
    assert r.signals_total >= 1
    assert "skill" in r.signals_by_type


# ---------------------------------------------------------------------------
# format_report() — plaintext shape
# ---------------------------------------------------------------------------


def test_format_report_returns_plaintext_with_sections():
    sid = db.create_session(title="metrics-fmt")
    _seed_pm(sid, verdict="pass")
    r = compute(days=1)
    text = format_report(r)
    assert "POST-MORTEMS:" in text
    assert "DELIVERABLES:" in text
    assert "SIGNALS" in text


def test_format_report_handles_empty_window():
    # Window far in the past should produce a report with no errors.
    old = datetime.now(timezone.utc) - timedelta(days=3650)
    r = compute(since_iso=old.isoformat(), until_iso=(old + timedelta(hours=1)).isoformat())
    text = format_report(r)
    assert "POST-MORTEMS: 0" in text


def test_to_dict_returns_serializable_shape():
    r = compute(days=1)
    d = r.to_dict()
    # Round-trip through json to confirm serializability
    assert json.loads(json.dumps(d))
