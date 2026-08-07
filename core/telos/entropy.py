"""TELOS Entropy Control — the acedia detector (spec §5.5).

Binding is the drive misbound; acedia is the drive extinguished. Dual
failure, dual monitor. Weekly: if novelty entropy over executed hypotheses
falls below the floor (0.2), or the far band's realized share falls below
0.10, raise soup temperature (shift the band mix toward far) and bump the
serendipity budget one notch until the floor recovers. When healthy, decay
both back toward config defaults. The restlessness is load-bearing; this
loop keeps it lit.

Mechanical — no LLM.
"""

from __future__ import annotations

import logging
import math

from config import settings
from core.telos.store import TelosObject, TelosStore

logger = logging.getLogger("pernix.telos.entropy")

_NOVELTY_FLOOR = 0.20
_FAR_BAND_MIN = 0.10
_FAR_STEP = 0.05
_SERENDIPITY_STEP = 0.05


def realized_band_shares(store: TelosStore, days: int = 7) -> dict:
    events = store.trace_events(days=days, types={"hypothesis"})
    counts = {"near": 0, "mid": 0, "far": 0}
    for e in events:
        band = str(e.get("band", ""))
        if band in counts:
            counts[band] += 1
    total = sum(counts.values())
    if total == 0:
        return {"near": 0.0, "mid": 0.0, "far": 0.0, "total": 0}
    return {**{k: round(v / total, 3) for k, v in counts.items()}, "total": total}


def novelty_entropy(store: TelosStore, days: int = 7) -> float:
    """Normalized Shannon entropy over (band, source_domain) of executed
    hypotheses — collapse to one band/domain reads as 0, an even spread as 1."""
    executed = [h for h in store.list_hypotheses() if h.get("status") in ("running", "supported", "refuted", "gated")]
    if len(executed) < 2:
        return 1.0  # too little signal to call the drive extinguished
    buckets: dict[str, int] = {}
    for h in executed:
        mapping = h.get("mapping") or {}
        key = f"{h.get('band')}:{str(mapping.get('source_domain', ''))[:40].lower()}"
        buckets[key] = buckets.get(key, 0) + 1
    total = sum(buckets.values())
    h_val = -sum((n / total) * math.log2(n / total) for n in buckets.values())
    h_max = math.log2(len(buckets)) if len(buckets) > 1 else 1.0
    return round(h_val / h_max, 3) if h_max else 0.0


def run_entropy_control(store: TelosStore) -> dict:
    shares = realized_band_shares(store)
    entropy = novelty_entropy(store)
    state = store.get_state()
    mix = store.band_mix()
    serendipity = store.serendipity_budget()

    starving = (shares["total"] > 0 and shares["far"] < _FAR_BAND_MIN) or entropy < _NOVELTY_FLOOR
    changed = False
    if starving:
        # Raise the temperature: shift mix toward far, bump serendipity.
        far = min(0.5, mix["far"] + _FAR_STEP)
        near = max(0.2, mix["near"] - _FAR_STEP)
        mid = max(0.0, 1.0 - near - far)
        store.set_state(
            soup_bands={"near": round(near, 2), "mid": round(mid, 2), "far": round(far, 2)},
            serendipity_budget=round(min(0.5, serendipity + _SERENDIPITY_STEP), 2),
        )
        changed = True
        alarm = TelosObject(
            id=store.mint_id("alarm"),
            kind="alarm",
            meta={
                "type": "acedia",
                "target": "soup",
                "level": 1,
                "state": "open",
                "evidence": {"novelty_entropy": entropy, "far_share": shares["far"], "executed": shares["total"]},
            },
        )
        store.write(alarm)
    else:
        # Healthy: decay any override back toward config defaults, and clear
        # open acedia alarms — the restlessness recovered.
        default_far = 0.20
        if abs(mix["far"] - default_far) > 0.01 or abs(serendipity - settings.telos_serendipity_budget) > 0.01:
            far = max(default_far, mix["far"] - _FAR_STEP)
            near = min(0.50, mix["near"] + _FAR_STEP)
            mid = max(0.0, 1.0 - near - far)
            store.set_state(
                soup_bands={"near": round(near, 2), "mid": round(mid, 2), "far": round(far, 2)},
                serendipity_budget=round(max(settings.telos_serendipity_budget, serendipity - _SERENDIPITY_STEP), 2),
            )
            changed = True
        for a in store.list_alarms(open_only=True):
            if a.get("type") == "acedia":
                store.update(a, state="cleared", cleared_reason="entropy floor recovered")

    result = {"novelty_entropy": entropy, "far_share": shares["far"], "adjusted": changed, "starving": starving}
    store.trace_append("entropy_control", result)
    logger.info("telos: entropy control: %s", result)
    return result
