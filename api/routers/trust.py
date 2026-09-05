"""Pernix — /api/trust: one surface for "is the learning loop honest?"

Every other dashboard in the app reports activity: how many turns ran, how
many entries exist, how many canaries passed. None of them answer the only
question that matters about a loop that changes its own behaviour — whether
the signal it is learning from is real.

So this endpoint reports the five things that can falsify it:

* `grader`   — how often the reflect verdict and the user's own thumb agree,
               plus the nightly hold-out score when one has been recorded.
* `outcomes` — the outcome-source mix (llm < next_turn < user) and how much of
               the week's traffic got graded at all.
* `entries`  — adaptive entries by status, and how many are `unfounded`:
               created from evidence that resolves to no recorded outcome.
* `canaries` — runs, failures, and how many were contaminated by the memory
               they exist to test.
* `trials`   — per-entry treated/control results once trial arms ship.

Inputs that do not exist yet answer with zeros. Several of them are written by
sibling workstreams (the hold-out score, the receipts module, the
contamination scan, trial arms), and a dashboard that 500s until the last of
them lands is a dashboard nobody can use to watch them land.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter

from db import models as db

router = APIRouter(tags=["trust"])

logger = logging.getLogger("pernix.api.trust")

# The outcome mix is a "what is happening now" number; the canary counts are a
# "has anything leaked" number, and leaks are rarer than turns.
_OUTCOME_WINDOW_DAYS = 7
_CANARY_WINDOW_DAYS = 14

# Written by the nightly grader hold-out run.
_HOLDOUT_KEY = "trust.grader_holdout"


def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _holdout() -> dict | None:
    """The last grader hold-out result, or None when none has been recorded."""
    try:
        raw = db.get_snooze_state(_HOLDOUT_KEY)
    except Exception as e:
        logger.debug("Grader hold-out state unavailable: %s", e)
        return None
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        logger.debug("Grader hold-out state is not JSON; reporting none")
        return None
    return parsed if isinstance(parsed, dict) else None


def _unfounded_entries() -> int:
    """Adaptive entries whose creating evidence resolves to no recorded outcome.

    The resolver lives in core/adaptive/receipts.py, which another workstream
    owns; until it exists the honest answer is zero rather than an error.
    """
    try:
        from core.adaptive import receipts
    except ImportError:
        return 0
    counter = getattr(receipts, "count_unfounded", None)
    if not callable(counter):
        return 0
    try:
        return int(counter())
    except Exception as e:
        logger.debug("Unfounded-entry count failed: %s", e)
        return 0


def _snapshot() -> dict:
    """Assemble the whole surface. Runs off-loop; every part fails to zeros."""
    from core.metrics import grader_agreement

    outcome_since = _since(_OUTCOME_WINDOW_DAYS)
    canary_since = _since(_CANARY_WINDOW_DAYS)

    try:
        grader = grader_agreement()
    except Exception as e:
        logger.debug("Grader agreement unavailable: %s", e)
        grader = {"agreement": 0.0, "n": 0}

    outcomes = db.post_mortem_outcome_counts(outcome_since)
    canaries = db.canary_outcome_counts(canary_since)

    return {
        "grader": {
            "agreement": grader.get("agreement", 0.0),
            "n": grader.get("n", 0),
            "holdout": _holdout(),
        },
        "outcomes": {
            "by_source": outcomes["by_source"],
            "graded_7d": outcomes["graded"],
            "user_turns_7d": db.count_user_turns_since(outcome_since),
        },
        "entries": {
            "by_status": db.adaptive_entry_status_counts(),
            "unfounded": _unfounded_entries(),
        },
        "canaries": {
            "contaminated_14d": canaries["contaminated"],
            "runs_14d": canaries["runs"],
            "fails_14d": canaries["fails"],
        },
        # Filled once trial arms land (batch 2). An empty list is the correct
        # answer for "no entry is currently under trial", not a placeholder.
        "trials": [],
    }


@router.get("/api/trust")
async def get_trust():
    """Grader agreement, outcome-source mix, entry provenance, contamination."""
    return await asyncio.to_thread(_snapshot)
