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

# Kinds with no validation path. They are generated, reported, and never
# resolved, so they must be kept out of the validator's queue window and
# aged out of `pending` — see archive_stale_dream_hypotheses.
_NON_VALIDATED_KINDS = ("open_question",)


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

    # Genuinely oldest-first pending queue (ordered at the query, so a
    # backlog larger than the window cannot starve its oldest rows); open
    # questions are report material, not validation candidates.
    #
    # The kind exclusion belongs in the QUERY, not in a comprehension over
    # its result. Filtering after the LIMIT windows over the wrong
    # population: open_question rows are never validated and never expire,
    # so once 200 of them accumulated they filled the window completely and
    # this list came back empty — which reads exactly like "nothing to
    # validate". Dream then generated on every cycle instead, for two days,
    # while 58 real candidates sat unreachable behind them.
    # The fetch window must exceed the backpressure cap, or the comparison
    # below can never be true: a hard limit=200 against a cap of 200 made
    # `len(pending) > cap` unreachable, backpressure never engaged, and the
    # queue grew unbounded (observed at 310 pending). The 200 floor keeps
    # the batch big enough to do real work when the cap is small.
    _cap = max(1, settings.dream_max_pending)
    pending = db.list_dream_hypotheses(
        status="pending", limit=max(200, _cap + 1), oldest_first=True, exclude_kinds=_NON_VALIDATED_KINDS
    )
    last_action = db.get_snooze_state("dream_last_action") or ""
    # Backpressure: generation adds up to dream_hypotheses_per_cycle rows per
    # step while validation resolves ~one, so an unbounded queue only grows.
    # Past the cap, every step validates until the backlog drains.
    backlogged = len(pending) > _cap

    did = None
    if pending and (last_action != "validate" or backlogged):
        from core.dream.validate import validate_one

        outcome, expired_count = await validate_one(store, pending, is_cancelled)
        if outcome == "validated":
            stats["dream_validated"] = 1
        elif outcome == "refuted":
            stats["dream_refuted"] = 1
        stats["dream_expired"] = expired_count
        if outcome is not None:
            did = "validate"
    if did is None and not backlogged and not is_cancelled():
        from core.dream.hypothesize import generate

        stats["dream_hypotheses"] = await generate(store, is_cancelled)
        did = "generate"

    if did:
        db.set_snooze_state("dream_last_action", did)

    # Promotion (plan 4d): validated hypotheses climb into the adaptive
    # layer — gated by adaptive_enabled, bounded per step, stamped
    # status='promoted' so each promotes exactly once.
    if settings.adaptive_enabled and not is_cancelled():
        from core.dream.promote import promote_validated

        try:
            stats["dream_promoted"] = await promote_validated()
        except Exception as e:
            logger.warning("dream: promotion failed: %s", e)

        # Symmetric with promotion, and for the same reason Candor's pass is
        # symmetric: minting without retiring wedges the per-kind cap, after
        # which every promotion is silently rejected (core/dream/retire.py).
        try:
            from core.dream.retire import retire_stale_hints

            stats["dream_retired"] = await retire_stale_hints()
        except Exception as e:
            logger.warning("dream: adaptive retirement failed: %s", e)

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
    # The same daily slot ages out the kinds that have no validation path, so
    # `pending` converges instead of growing without bound.
    if not is_cancelled():
        import asyncio
        from datetime import datetime, timezone

        from core.dream.journal import prune_old_journals_sync

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if db.get_snooze_state("dream_journal_prune") != today:
            await asyncio.to_thread(prune_old_journals_sync)
            # Twice the report interval, so every open question is carried by
            # at least one report before it is archived.
            ttl_days = max(2, settings.dream_report_interval_days * 2)
            for kind in _NON_VALIDATED_KINDS:
                try:
                    n = await asyncio.to_thread(db.archive_stale_dream_hypotheses, kind, ttl_days)
                    if n:
                        logger.info("dream: archived %d stale %s rows (>%dd)", n, kind, ttl_days)
                except Exception as e:
                    logger.warning("dream: %s archival failed: %s", kind, e)
            _check_queue_health(pending)
            _check_promotion_health()
            db.set_snooze_state("dream_journal_prune", today)

    return stats


# A validation candidate older than this has not been looked at in a week of
# cycles. Dream runs many times a day, so this is far past "busy" — it means
# the queue has stopped draining.
_STALL_DAYS = 7


def _check_queue_health(pending: list[dict]) -> None:
    """Absolute-health check: is the validator actually draining its queue?

    The tripwire-style signals in this codebase all measure *relative*
    change. None of them can see a loop that has stopped entirely, which is
    why the open_question starvation ran unnoticed for two days. This asks
    the absolute question instead — is the oldest thing in the queue older
    than any healthy cycle would leave it — and says so out loud.
    """
    from datetime import datetime, timezone

    if not pending:
        return
    oldest = min((r.get("created_at") or "") for r in pending)
    if not oldest:
        return
    try:
        age_days = (datetime.now(timezone.utc) - datetime.fromisoformat(oldest)).days
    except ValueError:
        return
    if age_days < _STALL_DAYS:
        return
    try:
        db.add_notification(
            title="Dream: validation queue is not draining",
            body=(
                f"The oldest pending hypothesis is {age_days} days old and {len(pending)} "
                "candidates are waiting. Dream validates roughly one per idle cycle, so a "
                "backlog this old means validation is failing or starved rather than busy. "
                "Check the dream log for validator errors."
            ),
            urgency="normal",
        )
    except Exception as e:
        logger.warning("dream: queue health notification failed: %s", e)


def _check_promotion_health() -> None:
    """The other half of the absolute-health question: do VALIDATED rows
    reach promotion?

    The pending-queue check above cannot see this stall — on 2026-08-19 the
    validation loop was perfectly healthy while 55 validated rows sat parked
    for three days, because inflow (~18/day) had outrun the veto-window
    drain (10/day) and the per-producer proposal cap. That state logged one
    INFO line per cycle and alarmed nowhere. Waiting on review is normal;
    waiting longer than the stall threshold means inflow and drain have
    diverged and the queue will only grow — say so out loud, once a day,
    from the same daily slot as the pending check.
    """
    from datetime import datetime, timezone

    validated = db.list_dream_hypotheses(status="validated", limit=100, oldest_first=True)
    if not validated:
        return
    oldest = min((r.get("created_at") or "") for r in validated)
    if not oldest:
        return
    try:
        age_days = (datetime.now(timezone.utc) - datetime.fromisoformat(oldest)).days
    except ValueError:
        return
    if age_days < _STALL_DAYS:
        return
    try:
        db.add_notification(
            title="Dream: validated findings are not reaching promotion",
            body=(
                f"The oldest validated hypothesis is {age_days} days old and {len(validated)} "
                "are waiting. Promotion is backpressured by the adaptive proposal queue "
                "(adaptive_max_pending_per_producer) and the veto-window drain "
                "(adaptive_max_auto_approvals_per_day) — a backlog this old means findings "
                "are being minted faster than they are applied. Drain the review queue, or "
                "reduce inflow, or raise the drain caps."
            ),
            urgency="normal",
        )
    except Exception as e:
        logger.warning("dream: promotion health notification failed: %s", e)
