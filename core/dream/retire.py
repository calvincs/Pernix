"""Pernix — Dream adaptive-hint retirement.

`promote.py` mints adaptive entries from validated hypotheses and nothing
ever took one back, so dream consumed slots against
`adaptive_max_entries_per_kind` permanently. Once `routing_hint` filled,
every later promotion was rejected at apply time with a logged-only error —
"the loop runs and produces nothing", indistinguishable from "nothing to
say". Candor has had the symmetric mint/retire pass since it shipped; this
is dream's.

Retirement criteria, all mechanical (no LLM), checked in this order:

1. **The originating hypothesis is gone or unpromoted.** The creating event
   cites `dream_hypothesis:<id>`; if that row has vanished or no longer
   reads `promoted`, the entry has no author.
2. **The Candor evidence recovered.** For `tool_pattern` promotions this is
   the real criterion and it is the exact test `validate.py` uses to refute:
   re-query each cited `(pred, args)` and, when every checkable fact has
   come back above the degradation line with enough observations to mean it,
   the hint is advice about a problem that no longer exists. This mirrors
   Candor's own retirement rule so the two producers age out on the same
   evidence.
3. **TTL.** Past `_HINT_TTL_DAYS`, retire regardless. Dream evidence is
   content-hash-pinned to a corpus snapshot; past the TTL the hint is
   unverifiable rather than false, and an unverifiable hint should not hold
   a capped slot against a checkable one. Dream re-mints from fresh evidence
   if the pattern is still real.

Kinds: `routing_hint` and `policy` — everything `promote._promote_edit` can
create. Memory-review proposals have no entry to retire.
"""

from __future__ import annotations

import logging

from config import settings
from db import models as db

logger = logging.getLogger("pernix.dream.retire")

_KINDS = ("routing_hint", "policy")
_HINT_TTL_DAYS = 90
_MAX_PER_PASS = 2
_HYPOTHESIS_REF_PREFIX = "dream_hypothesis:"


def _hypothesis_id(refs: list[str]) -> str:
    for r in refs:
        if r.startswith(_HYPOTHESIS_REF_PREFIX):
            return r[len(_HYPOTHESIS_REF_PREFIX) :]
    return ""


async def _candor_recovered(row: dict) -> str | None:
    """Reason string when every cited Candor fact is healthy again, else None."""
    if not settings.candor_enabled or row.get("kind") != "tool_pattern":
        return None
    from core.dream.validate import _DEGRADED_P, _MIN_OBSERVATIONS, _evidence
    from core.extensions.candor.bridge import get_candor_bridge

    refs = [e for e in _evidence(row) if e.get("type") == "candor" and e.get("pred")]
    if not refs:
        return None
    bridge = get_candor_bridge()
    recovered: list[str] = []
    for ref in refs:
        result = await bridge.predict(ref["pred"], ref.get("args") or [])
        if result is None or "p" not in result:
            return None  # bridge inert or fact gone — cannot claim recovery
        p = float(result["p"])
        n = int(result.get("observations") or 0)
        if n < _MIN_OBSERVATIONS or p < _DEGRADED_P:
            return None  # still degraded, or too thin to call either way
        recovered.append(f"{ref['pred']}({','.join(str(a) for a in ref.get('args') or [])}): p={p:.2f} n={n}")
    return "candor evidence recovered — " + "; ".join(recovered[:3])


async def retire_stale_hints() -> int:
    """One retirement sweep over dream-authored adaptive entries. Count queued."""
    if not (settings.adaptive_enabled and settings.dream_enabled):
        return 0

    from core.adaptive.contract import queue_producer_edits
    from core.adaptive.retire import creating_evidence, entry_age_days, producer_entries, retire_edit

    by_id = {r["id"]: r for r in db.list_dream_hypotheses(limit=1000)}
    edits = []
    for entry in producer_entries("dream", _KINDS):
        if len(edits) >= _MAX_PER_PASS:
            break
        hid = _hypothesis_id(creating_evidence(entry["id"]))
        hypothesis = by_id.get(hid) if hid else None

        reason: str | None = None
        if hid and hypothesis is None:
            reason = f"originating hypothesis {hid[:8]} no longer exists"
        elif hypothesis is not None and hypothesis.get("status") != "promoted":
            reason = f"originating hypothesis {hid[:8]} is now '{hypothesis.get('status')}'"
        elif hypothesis is not None:
            reason = await _candor_recovered(hypothesis)
        if reason is None:
            age = entry_age_days(entry)
            if age is not None and age >= _HINT_TTL_DAYS:
                reason = f"older than the {_HINT_TTL_DAYS}d hint TTL ({age:.0f}d)"
        if reason is None:
            continue
        edits.append(retire_edit(entry, reason))
        logger.info("dream: retiring adaptive entry %s — %s", entry["id"], reason)

    if not edits:
        return 0
    q = queue_producer_edits(edits, "dream", rationale="dream adaptive-entry retirement (evidence no longer holds)")
    return q["queued"] + q["gated"]
