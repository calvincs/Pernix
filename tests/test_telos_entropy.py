"""TELOS entropy control — the goal-independent slow loop kept by the v3.1
carve (ordo/binding/hevel/reconcile/discharge tests left with their modules;
the carve rationale and the two deleted regression pins are recorded in the
changelog)."""

from __future__ import annotations

import re

import pytest

from config import settings
from core.telos.entropy import novelty_entropy, run_entropy_control
from core.telos.store import TelosObject, TelosStore


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setattr(settings, "telos_enabled", True)
    s = TelosStore.open()
    s.ensure_root()
    return s


def test_entropy_raises_temperature_when_cold(store):
    # All executed hypotheses in one near-band bucket -> entropy 0. 'gated'
    # is deliberately NOT executed (see novelty_entropy), so these run.
    for i in range(5):
        store.write(
            TelosObject(
                id=store.mint_id("hypothesis"),
                kind="hypothesis",
                meta={"band": "near", "status": "running", "mapping": {"source_domain": "same"}, "question": "q"},
            )
        )
        store.trace_append("hypothesis_resolved", {"band": "near", "question": "q"})
    assert novelty_entropy(store) == 0.0
    result = run_entropy_control(store)
    assert result["starving"] and result["adjusted"]
    assert store.band_mix()["far"] > 0.20
    assert store.serendipity_budget() > settings.telos_serendipity_budget
    alarms = [a for a in store.list_alarms() if a.get("type") == "acedia"]
    assert len(alarms) == 1


def test_entropy_decays_back_when_healthy(store):
    store.set_state(soup_bands={"near": 0.4, "mid": 0.25, "far": 0.35}, serendipity_budget=0.3)
    # Diverse executed hypotheses across bands/domains.
    for band, dom in [("near", "a"), ("mid", "b"), ("far", "c"), ("far", "d")]:
        store.write(
            TelosObject(
                id=store.mint_id("hypothesis"),
                kind="hypothesis",
                meta={"band": band, "status": "supported", "mapping": {"source_domain": dom}, "question": "q"},
            )
        )
        store.trace_append("hypothesis_resolved", {"band": band, "question": "q"})
    result = run_entropy_control(store)
    assert not result["starving"] and result["adjusted"]
    assert store.band_mix()["far"] < 0.35
    assert store.serendipity_budget() < 0.3


def _hypothesis(store, band, dom, status="supported", updated_at=None):
    obj = TelosObject(
        id=store.mint_id("hypothesis"),
        kind="hypothesis",
        meta={"band": band, "status": status, "mapping": {"source_domain": dom}, "question": "q"},
    )
    store.write(obj)
    if updated_at is not None:
        # write() always re-stamps updated_at, so backdate the file on disk.
        text = re.sub(r"^updated_at: .*$", f"updated_at: '{updated_at}'", obj.path.read_text(), flags=re.M)
        obj.path.write_text(text)
    return obj


def test_novelty_entropy_honours_its_window(store):
    """Old variety must not mask a drive that went flat this week — the days
    argument was accepted and ignored, desensitizing the acedia detector as
    history grew."""
    for band, dom in [("near", "a"), ("mid", "b"), ("far", "c"), ("far", "d")]:
        _hypothesis(store, band, dom, updated_at="2020-01-01T00:00:00Z")
    # All-time the spread is wide; the last 7 days are one collapsed bucket.
    assert novelty_entropy(store, days=4000) == 1.0
    for _ in range(4):
        _hypothesis(store, "near", "same")
    assert novelty_entropy(store, days=7) == 0.0
    assert novelty_entropy(store, days=4000) > 0.0


def test_realized_band_shares_counts_executed_not_generated(store):
    from core.telos.entropy import realized_band_shares

    # Generation events are candidates, not executions: they must not count.
    for _ in range(9):
        store.trace_append("hypothesis", {"band": "far", "question": "q"})
    assert realized_band_shares(store)["total"] == 0
    for band in ("near", "near", "far"):
        store.trace_append("hypothesis_resolved", {"band": band, "question": "q"})
    shares = realized_band_shares(store)
    assert shares["total"] == 3
    assert shares["far"] == round(1 / 3, 3)
