"""Per-band EIG calibration export (P5-c-reduced, build-pick 2026-08-18).

The scoring site is the only place band, claimed eig and realized outcome are
all known at once — soup files carry neither claimed nor realized, telos_status
printed one aggregate line, and the trace's eig_calibration events are
per-question. eig_calibration now slices the same scored pairs by band and
dumps the table to ledgers/telos_eig_perband.json for the curiosity deep-dive.
"""

import json

import pytest

from config import settings
from core.telos.calibration import describe, eig_calibration
from core.telos.store import TelosStore


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setattr(settings, "telos_enabled", True)
    return TelosStore.open()


def _score_events(store):
    # near: two scored, one resolves. mid: one scored, pooled. bandless: one.
    store.trace_append("hypothesis", {"id": "h_n1", "eig": 0.8, "band": "near"})
    store.trace_append("hypothesis", {"id": "h_n2", "eig": 0.6, "band": "near"})
    store.trace_append("hypothesis", {"id": "h_m1", "eig": 0.4, "band": "mid"})
    store.trace_append("hypothesis", {"id": "h_x1", "eig": 0.2})
    store.trace_append("hypothesis_resolved", {"id": "h_n1", "band": "near"})
    store.trace_append("hypothesis_pooled", {"id": "h_n2"})
    store.trace_append("hypothesis_pooled", {"id": "h_m1"})
    store.trace_append("hypothesis_pooled", {"id": "h_x1"})


def test_per_band_slices_the_scored_pairs(store):
    _score_events(store)
    calib = eig_calibration(store)

    assert calib["n"] == 4
    pb = calib["per_band"]
    assert pb["near"]["n"] == 2
    assert pb["near"]["mean_eig"] == 0.7
    assert pb["near"]["resolve_rate"] == 0.5
    assert pb["near"]["brier"] == round(((0.8 - 1.0) ** 2 + (0.6 - 0.0) ** 2) / 2, 4)
    assert pb["mid"]["n"] == 1 and pb["mid"]["resolve_rate"] == 0.0
    # A bandless trace line is reported as unknown, never dropped —
    # the slices must sum to the aggregate denominator.
    assert pb["unknown"]["n"] == 1
    assert sum(row["n"] for row in pb.values()) == calib["n"]


def test_export_file_lands_next_to_the_trace_ledger(store):
    _score_events(store)
    eig_calibration(store)

    path = store.root / "ledgers" / "telos_eig_perband.json"
    assert path.exists()
    payload = json.loads(path.read_text())
    assert payload["aggregate"]["n"] == 4
    assert payload["per_band"]["near"]["n"] == 2
    assert "generated_at" in payload and payload["window_days"] == 90


def test_describe_carries_the_per_band_clause(store):
    _score_events(store)
    line = describe(eig_calibration(store))
    assert "per-band:" in line
    assert "near 2@claimed 0.7/realized 0.5" in line
    assert "telos_eig_perband.json" in line


def test_resolved_and_pooled_counts_expose_the_probe_leak(store):
    """Every pooled sample passed the mint probe and still couldn't be judged
    (E7): the export carries the split so the calibration review reads the
    probe-leak per band instead of re-deriving it from resolve_rate."""
    _score_events(store)
    calib = eig_calibration(store)

    assert calib["n_resolved"] == 1 and calib["n_pooled"] == 3
    pb = calib["per_band"]
    assert pb["near"]["n_resolved"] == 1 and pb["near"]["n_pooled"] == 1
    assert pb["mid"]["n_resolved"] == 0 and pb["mid"]["n_pooled"] == 1
    assert all(row["n_resolved"] + row["n_pooled"] == row["n"] for row in pb.values())


def test_empty_store_unchanged(store):
    calib = eig_calibration(store)
    assert calib["n"] == 0 and "per_band" not in calib
    assert "not" in describe(calib) or "no evaluated" in describe(calib)
