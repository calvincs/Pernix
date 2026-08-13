"""TELOS adaptive-hint retirement — the half of the loop Candor already had.

`evaluate.py` mints a `routing_hint` for every supported, evidence-backed,
judge-confident claim. Nothing ever took one back, so telos consumed slots
against `adaptive_max_entries_per_kind` permanently and, once the kind
filled, every later supported claim was rejected at apply time.

Retirement criteria, in the order they are checked, all mechanical:

1. **The evidence object is gone.** The hint's creating event cites the
   claim, the hypothesis, and the question (`evaluate.py`). If the
   hypothesis file no longer exists, nothing in the store backs the hint.
2. **The hypothesis no longer reads `supported`.** A hypothesis returned to
   the speculation pool or flipped to `refuted` by a later pass has had its
   support withdrawn; the hint outlived its premise.
3. **The parent question was abandoned.** `soup.py` abandons a question
   after `telos_question_max_attempts` dry generations — the line of inquiry
   the hint serves is closed.
4. **TTL.** Past `_HINT_TTL_DAYS` the hint is retired regardless. This is
   the honest criterion: 1–3 rarely fire, because a telos verdict is
   terminal by construction (evaluation only ever picks `gated` hypotheses,
   so a `supported` one is never revisited). Without a TTL the retirement
   pass would be decorative in exactly the way the mint-only version was.
   A still-true claim re-mints cheaply the next time its hypothesis is
   re-generated and re-supported; a slot held forever cannot.

Mechanical — no LLM.
"""

from __future__ import annotations

import logging

from config import settings
from core.telos.store import TelosStore

logger = logging.getLogger("pernix.telos.retire")

_KINDS = ("routing_hint",)
_HINT_TTL_DAYS = 90
# Bounded like Candor's pass: a sweep is maintenance, not a purge.
_MAX_PER_PASS = 3


def _retire_reason(store: TelosStore, refs: list[str]) -> str | None:
    """Why this hint's evidence no longer holds, or None when it still does."""
    hypothesis_ids = [r for r in refs if r.startswith("h_")]
    question_ids = [r for r in refs if r.startswith("q_")]

    for hid in hypothesis_ids:
        h = store.read("hypothesis", hid)
        if h is None:
            return f"hypothesis {hid} no longer in the store"
        if h.get("status") != "supported":
            return f"hypothesis {hid} is now '{h.get('status')}', not supported"
    for qid in question_ids:
        q = store.read("question", qid)
        if q is not None and q.get("state") == "abandoned":
            return f"question {qid} was abandoned"
    return None


def retire_stale_hints(store: TelosStore) -> dict:
    """One retirement sweep over telos-authored adaptive entries."""
    result = {"retired": 0, "reasons": []}
    if not (settings.adaptive_enabled and settings.telos_enabled):
        return result

    from core.adaptive.contract import queue_producer_edits
    from core.adaptive.retire import creating_evidence, entry_age_days, producer_entries, retire_edit

    edits = []
    for row in producer_entries("telos", _KINDS):
        if len(edits) >= _MAX_PER_PASS:
            break
        refs = creating_evidence(row["id"])
        reason = _retire_reason(store, refs)
        if reason is None:
            age = entry_age_days(row)
            if age is not None and age >= _HINT_TTL_DAYS:
                reason = f"older than the {_HINT_TTL_DAYS}d hint TTL ({age:.0f}d)"
        if reason is None:
            continue
        edits.append(retire_edit(row, reason))
        result["reasons"].append({"entry_id": row["id"], "reason": reason})

    if edits:
        q = queue_producer_edits(edits, "telos", rationale="telos routing-hint retirement (evidence no longer holds)")
        result["retired"] = q["queued"] + q["gated"]
        store.trace_append("adaptive_retire", {"count": result["retired"], "reasons": result["reasons"]})
        logger.info("telos: queued %d routing-hint retirement(s)", result["retired"])
    return result


def prune_speculation_pool(store: TelosStore) -> dict:
    """Delete pooled hypotheses past `telos_soup_retention_days`.

    The pool is a retained record, not a feedstock: nothing reads
    `status == "soup"` (the spec's recombination pass is unimplemented, and
    `core/telos/soup.py` says so). Left unbounded it grows by roughly the
    generation rate forever — 388 of 402 files on one seven-day-old install —
    and every `list_hypotheses()` call in the generate/evaluate loop re-reads
    all of them, so the cost lands on the hot path rather than on disk.

    Only `soup` rows are eligible: `gated` is queued work, and `supported` /
    `refuted` are the falsification record that hint retirement and the
    calibration fallback both read. Deletion is safe only because ids are
    minted against a persisted high-water mark (`TelosStore.mint_id`) — with
    the old disk-derived scheme this would have recycled ids out from under
    live references.
    """
    result: dict = {"pruned": 0}
    days = int(settings.telos_soup_retention_days or 0)
    if days <= 0:
        return result

    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    for h in store.list_hypotheses(status="soup"):
        created = str(h.get("created_at") or "")
        if not created:
            continue
        try:
            when = datetime.fromisoformat(created)
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when >= cutoff:
            continue
        try:
            if h.path is not None:
                h.path.unlink()
                result["pruned"] += 1
        except OSError as e:
            logger.debug("telos: soup prune failed for %s: %s", h.id, e)

    if result["pruned"]:
        store.trace_append("soup_pruned", {"count": result["pruned"], "older_than_days": days})
        logger.info("telos: pruned %d pooled hypotheses older than %dd", result["pruned"], days)
    return result
