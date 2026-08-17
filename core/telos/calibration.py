"""TELOS EIG calibration — the metric spec §7 names and §3.4 depends on.

The testability gate admits a hypothesis when ``eig >= telos_eig_floor``, and
``eig`` is a number the generating model made up about its own output. The
spec (§8) calls this the weakest link and says the EIG-calibration metric
"exists specifically to detect when the gate is being gamed by optimistic
estimates". This module is that metric plus the actuation that makes it bite.

Ground truth is already in the trace, both halves of it: the predicted eig
rides the ``hypothesis`` generation event, and whether evaluation actually
moved anything rides the resolution event — ``hypothesis_resolved`` (the
falsifier discriminated: a verdict was reached and a claim committed) or
``hypothesis_pooled`` (two inconclusive passes: the check named an observable
the records do not contain, so nothing was learned).

Reading eig as "probability this hypothesis resolves when evaluated" is the
interpretation that makes it scorable at all. It is stated here rather than
buried, because the score below is only as meaningful as that reading:

    brier = mean((eig - realized)^2),  realized in {0.0, 1.0}

**Why the Brier total is reported but does not trigger the discount.** The
gaming signature is a constant optimistic estimate — always emit eig 0.4,
clear a 0.15 floor forever. Against all-inconclusive outcomes a constant 0.4
scores brier 0.16, *better* than an honest 0.5, so the total is blind to
exactly the failure it was commissioned to catch. What sees it is the
reliability component — calibration-in-the-large, ``mean_eig -
resolve_rate``. The Brier is kept as the spec's trend-watched headline; the
overclaim term is what actuates.

Actuation: when the sample is large enough and predictions systematically
exceed the realized resolve rate, the gate multiplies every model-supplied
eig by the mean-recalibration factor ``resolve_rate / mean_eig``.
Over-claiming only — a pessimistic estimator is never inflated, because the
gate is a floor and inflating it would admit hypotheses the model itself
expects to learn nothing from.

The window is the release valve: the discount is computed over a rolling
trace window, so it lifts on its own as the events that justified it age out.
The gate cannot latch shut permanently on one bad patch.

Mechanical — no LLM.
"""

from __future__ import annotations

import logging

from core.telos.store import TelosStore

logger = logging.getLogger("pernix.telos.calibration")

_WINDOW_DAYS = 90
# Below this many scored evaluations the estimate is noise; no discount.
_MIN_SAMPLES = 8
# Mean predicted eig must exceed the realized resolve rate by at least this
# much before the correction engages — small gaps are sampling, not gaming.
_MIN_OVERCLAIM = 0.10
# Floor on the correction so a bad patch degrades the gate rather than
# closing it: at 0.25 a claimed eig of 0.6 still clears a 0.15 floor.
_MIN_DISCOUNT = 0.25


def _as_eig(value) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return min(max(f, 0.0), 1.0)


def eig_calibration(store: TelosStore, days: int = _WINDOW_DAYS) -> dict:
    """Score predicted eig against realized resolution over the trace window.

    Returns ``{n, brier, mean_eig, resolve_rate, overclaim, discount}``.
    ``brier`` is None when nothing has been evaluated yet — an absent metric
    is reported absent, never as a flattering zero.
    """
    events = store.trace_events(days=days, types={"hypothesis", "hypothesis_resolved", "hypothesis_pooled"})

    predicted: dict[str, float] = {}
    outcomes: dict[str, float] = {}
    for e in events:
        hid = str(e.get("id") or "")
        if not hid:
            continue
        etype = e.get("type")
        if etype == "hypothesis":
            eig = _as_eig(e.get("eig"))
            if eig is not None:
                predicted[hid] = eig
        elif etype == "hypothesis_resolved":
            outcomes[hid] = 1.0
        elif etype == "hypothesis_pooled":
            outcomes[hid] = 0.0

    # Trace lines written before the eig field existed carry no prediction,
    # and a generation event can fall out of the window before its
    # resolution lands. The hypothesis object still holds the number, so
    # fall back to it rather than dropping the sample.
    missing = [hid for hid in outcomes if hid not in predicted]
    if missing:
        by_id = {h.id: h for h in store.list_hypotheses()}
        for hid in missing:
            obj = by_id.get(hid)
            if obj is None:
                # A dead-end hypothesis is archived at the moment it is
                # pooled, so the fallback must look there too — otherwise the
                # only samples it can ever recover are the ones that resolved
                # and the discount is computed against a resolve rate this
                # module exists to distrust.
                obj = store.read_archived("hypothesis", hid)
            eig = _as_eig(obj.get("eig")) if obj is not None else None
            if eig is not None:
                predicted[hid] = eig

    pairs = [(predicted[hid], realized) for hid, realized in outcomes.items() if hid in predicted]
    n = len(pairs)
    if n == 0:
        return {"n": 0, "brier": None, "mean_eig": None, "resolve_rate": None, "overclaim": None, "discount": 1.0}

    brier = sum((eig - realized) ** 2 for eig, realized in pairs) / n
    mean_eig = sum(eig for eig, _ in pairs) / n
    resolve_rate = sum(realized for _, realized in pairs) / n
    overclaim = mean_eig - resolve_rate

    discount = 1.0
    if n >= _MIN_SAMPLES and mean_eig > 0 and overclaim >= _MIN_OVERCLAIM:
        discount = max(_MIN_DISCOUNT, round(resolve_rate / mean_eig, 3))

    return {
        "n": n,
        "brier": round(brier, 4),
        "mean_eig": round(mean_eig, 3),
        "resolve_rate": round(resolve_rate, 3),
        "overclaim": round(overclaim, 3),
        "discount": discount,
    }


def eig_discount(store: TelosStore, days: int = _WINDOW_DAYS) -> tuple[float, dict]:
    """(multiplier, calibration) for the soup gate. 1.0 = trust the model."""
    calib = eig_calibration(store, days=days)
    return float(calib["discount"]), calib


def describe(calib: dict) -> str:
    """One status line. Says "not yet measurable" rather than implying zero."""
    if not calib.get("n"):
        return "EIG calibration: no evaluated hypotheses yet — the gate trusts the model's estimate"
    line = (
        f"EIG calibration: Brier {calib['brier']} over {calib['n']} evaluated "
        f"(mean claimed {calib['mean_eig']} vs realized resolve rate {calib['resolve_rate']})"
    )
    if calib["discount"] < 1.0:
        line += f" — GATE DISCOUNT {calib['discount']}x active (systematic over-claim {calib['overclaim']})"
    return line
