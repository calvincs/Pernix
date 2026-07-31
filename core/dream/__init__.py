"""Pernix — Dream: idle-time introspection over memory and outcome evidence.

Design: docs/dev/dream-plan.md. One bounded unit per snooze cycle (Activity
14): either validate one pending hypothesis or generate new ones — never
both — then write the periodic report when due. Round-robin between the two
via snooze_state[dream_last_action] so neither starves.

Write-permission rule (enforced by construction): this package writes only
the dream_* tables, snooze_state dream_* keys, workspace/dreams/ files, and
memory entries with source="dream". It never mutates entries it did not
author.
"""

from __future__ import annotations

import logging

from config import settings
from db import models as db

logger = logging.getLogger("pernix.dream")


async def run_step(is_cancelled) -> dict:
    """One dream unit. Called from snooze Activity 14. Never raises upward
    with partial writes — each sub-step is internally guarded."""
    stats = {
        "dream_hypotheses": 0,
        "dream_validated": 0,
        "dream_refuted": 0,
        "dream_expired": 0,
        "dream_reports": 0,
    }
    if not settings.dream_enabled:
        return stats

    from core.memory.store import get_memory_store

    store = get_memory_store()
    if store is None:
        return stats

    # Oldest-first pending queue; open questions are report material, not
    # validation candidates.
    pending = [
        r
        for r in reversed(db.list_dream_hypotheses(status="pending", limit=50))
        if r.get("kind") != "open_question"
    ]
    last_action = db.get_snooze_state("dream_last_action") or ""

    did = None
    if pending and last_action != "validate":
        from core.dream.validate import validate_one

        outcome = await validate_one(store, pending, is_cancelled)
        if outcome == "validated":
            stats["dream_validated"] = 1
        elif outcome == "refuted":
            stats["dream_refuted"] = 1
        elif outcome == "expired":
            stats["dream_expired"] = 1
        if outcome is not None:
            did = "validate"
    if did is None and not is_cancelled():
        from core.dream.hypothesize import generate

        stats["dream_hypotheses"] = await generate(store, is_cancelled)
        did = "generate"

    if did:
        db.set_snooze_state("dream_last_action", did)

    if not is_cancelled():
        from core.dream.report import maybe_write_report

        try:
            if await maybe_write_report():
                stats["dream_reports"] = 1
        except Exception as e:
            logger.warning("dream: report write failed: %s", e)

    # Deep probe: fire-and-forget launch when due (runs outside the cycle
    # as a maintenance-tracked task; the cycle never waits on it).
    if not is_cancelled():
        from core.dream.probe import launch_if_due

        try:
            await launch_if_due(store)
        except Exception as e:
            logger.warning("dream: probe launch failed: %s", e)

    # Journal retention: once per day, drop journal sessions past the window.
    if not is_cancelled():
        import asyncio
        from datetime import datetime, timezone

        from core.dream.journal import prune_old_journals_sync

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if db.get_snooze_state("dream_journal_prune") != today:
            await asyncio.to_thread(prune_old_journals_sync)
            db.set_snooze_state("dream_journal_prune", today)

    return stats
