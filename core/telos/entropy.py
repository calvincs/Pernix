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


def _within_days(iso_ts, days: int) -> bool:
    """Window filter for object timestamps. A missing or unparseable stamp
    counts as in-window: a corrupt timestamp must not silently shrink the
    sample and manufacture an acedia alarm."""
    from datetime import datetime, timezone

    if not iso_ts:
        return True
    try:
        dt = datetime.strptime(str(iso_ts), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return True
    return (datetime.now(timezone.utc) - dt).total_seconds() <= max(1, int(days)) * 86400


def realized_band_shares(store: TelosStore, days: int = 7) -> dict:
    # 'hypothesis' is the generation event — every candidate the soup emits,
    # including those that never run. The band mix we actuate on is the mix
    # actually *executed*, so count resolutions instead (spec §5.5).
    events = store.trace_events(days=days, types={"hypothesis_resolved"})
    counts = {"near": 0, "mid": 0, "far": 0}
    for e in events:
        band = str(e.get("band", ""))
        if band in counts:
            counts[band] += 1
    total = sum(counts.values())
    if total == 0:
        return {"near": 0.0, "mid": 0.0, "far": 0.0, "total": 0}
    return {**{k: round(v / total, 3) for k, v in counts.items()}, "total": total}


def _bucket_key(h: TelosObject) -> str:
    """Novelty bucket for one hypothesis: band + the corpus region it drew on.

    `context_files` is recorded by the SOUP sampler and names the memory
    files the band actually returned — the model does not choose it and
    cannot rename it. When it is absent (hypotheses generated before the
    field existed, or a pass where memory returned nothing for that band)
    the key falls back to the model-authored `source_domain` label, and that
    fallback is a known weakness, stated plainly: a model that rotates
    synonyms scores high novelty with zero change in actual exploration.
    Bucketing on the sampled file is what closes that hole, so the fallback
    is a gap in the data, not a design choice.
    """
    files = h.get("context_files") or []
    if isinstance(files, list) and files:
        return f"{h.get('band')}:{'|'.join(sorted(str(f) for f in files))[:120]}"
    mapping = h.get("mapping") or {}
    return f"{h.get('band')}:domain:{str(mapping.get('source_domain', ''))[:40].lower()}"


def novelty_entropy(store: TelosStore, days: int = 7) -> float:
    """Normalized Shannon entropy over (band, sampled corpus region) of
    hypotheses executed in the last `days` — collapse to one bucket reads as
    0, an even spread as 1. The window is load-bearing: measured over all
    time the detector desensitizes as history accumulates, and a drive that
    went flat last week stays hidden behind years of past variety.

    "Executed" means the same thing here as in `realized_band_shares`:
    hypotheses that actually ran. `gated` is excluded — generation emits
    ~3 candidates per cycle and evaluation resolves ~1, so counting the
    gated pool let generation variety dominate a detector that is supposed
    to be measuring what the layer *does*.
    """
    executed = [
        h
        for h in store.list_hypotheses()
        if h.get("status") in ("running", "supported", "refuted")
        and _within_days(h.get("updated_at") or h.get("created_at"), days)
    ]
    if len(executed) < 2:
        return 1.0  # too little signal to call the drive extinguished
    buckets: dict[str, int] = {}
    for h in executed:
        key = _bucket_key(h)
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
