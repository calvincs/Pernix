"""Regression: the TELOS testability gate had no calibration, so a constant
optimistic EIG cleared it forever.

Shipped defect (2026-08-07 introspective-stack review, §3/§5.2): `soup.gate`
admitted a hypothesis on `eig >= telos_eig_floor` where `eig` is a number the
generating model made up about its own output. The spec (§7/§8) designed an
EIG-calibration metric "specifically to detect when the gate is being gamed
by optimistic estimates" and it was never built — grep for `brier`,
`eig_calib`, `realized_eig` returned nothing. A model that always emitted
`eig: 0.4` passed a 0.15 floor indefinitely and, by construction, nothing
could notice.

The fix scores predicted eig against realized resolution over the trace and
discounts the model's estimate at the gate when it systematically over-claims.

Kept as a regression pin because the failure is silent by nature: without
these assertions a future refactor could drop the discount and every test
that only checks "gate admits a well-formed hypothesis" would still pass.
"""

from __future__ import annotations

import pytest

from config import settings
from core.telos.calibration import eig_calibration
from core.telos.soup import gate
from core.telos.store import TelosStore


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setattr(settings, "telos_enabled", True)
    s = TelosStore.open()
    s.ensure_root()
    return s


def _pair(store, hid: str, eig: float, resolved: bool) -> None:
    """One generation event plus its outcome, as the live loop writes them."""
    store.trace_append("hypothesis", {"id": hid, "question": "q_x", "band": "near", "status": "gated", "eig": eig})
    store.trace_append(
        "hypothesis_resolved" if resolved else "hypothesis_pooled",
        {"id": hid, "verdict": "supported" if resolved else "inconclusive"},
    )


def test_well_calibrated_generator_is_not_discounted(store):
    # Claims 0.8, resolves 80% of the time: the estimate tracks reality.
    for i in range(10):
        _pair(store, f"h_{i:04d}", 0.8, resolved=i < 8)
    calib = eig_calibration(store)
    assert calib["n"] == 10
    assert calib["discount"] == 1.0
    assert calib["brier"] is not None


def test_constant_optimistic_estimate_is_discounted(store):
    # The gaming signature: always 0.4, never actually resolves anything.
    for i in range(10):
        _pair(store, f"h_{i:04d}", 0.4, resolved=False)
    calib = eig_calibration(store)
    assert calib["n"] == 10
    assert calib["resolve_rate"] == 0.0
    assert calib["overclaim"] == 0.4
    assert calib["discount"] < 1.0

    # The Brier TOTAL does not see this — a constant 0.4 against all-zero
    # outcomes scores better than an honest 0.5. This assertion is the point:
    # the discount must key on the reliability component, not the total.
    assert calib["brier"] < 0.25


def test_the_gate_actually_closes_on_a_gamed_estimate(store, monkeypatch):
    monkeypatch.setattr(settings, "telos_eig_floor", 0.15)
    monkeypatch.setattr(settings, "telos_max_eval_tokens", 100000)
    hypothesis = {"falsifier": {"observable": "x", "rule": "reject if y"}, "eig": 0.4, "cost_est_tokens": 100}

    # Undiscounted, the constant estimate clears the floor — the old behaviour.
    admitted, _ = gate(hypothesis)
    assert admitted

    for i in range(10):
        _pair(store, f"h_{i:04d}", 0.4, resolved=False)
    discount = eig_calibration(store)["discount"]
    admitted, reason = gate(hypothesis, eig_discount=discount)
    assert not admitted
    assert "calibration" in reason  # the trace says WHY it closed


def test_small_samples_never_discount(store):
    """Below the minimum sample the metric is noise; the gate must not act
    on it, or a cold start would wedge the layer shut."""
    for i in range(3):
        _pair(store, f"h_{i:04d}", 0.9, resolved=False)
    assert eig_calibration(store)["discount"] == 1.0


def test_no_evaluations_reports_absent_not_zero(store):
    calib = eig_calibration(store)
    assert calib["n"] == 0
    assert calib["brier"] is None  # an unmeasured metric is not a good score
    assert calib["discount"] == 1.0


def test_underclaiming_is_never_inflated(store):
    """The gate is a floor. Boosting a pessimistic estimate would admit
    hypotheses the model itself expects nothing from."""
    for i in range(10):
        _pair(store, f"h_{i:04d}", 0.2, resolved=True)
    assert eig_calibration(store)["discount"] == 1.0
