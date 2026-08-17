"""Pernix — TELOS: a teleological operational layer over the task loop.

Design: docs/dev/telos-spec.md (derived from the "Intelligence, Knowledge,
Purpose, and Personhood" source material). TELOS wraps the Pernix loop with
a non-convergent drive and its correction machinery:

  fast loop  — anomaly → Question → SOUP hypotheses → testability gate →
               evidence → claim (snooze Activity 16, one bounded unit/cycle)
  slow loops — Ordo Pass (daily re-ranking), Binding Monitor (Goodhart
               detector), Hevel Audit (discharge measurement), dual-ledger
               reconciliation, Entropy Control (acedia detector) — all run
               from the daily telos cron with weekly watermarks.

All state is markdown with YAML frontmatter under data/telos/ (greppable,
diffable, Provenas-style ids), plus an append-only JSONL trace ledger that
is authoritative over the agent's first-person autobiography — always.

Write-permission rule (enforced by construction): this package writes only
data/telos/** and snooze_state telos_* keys. The trace ledger is append-only
even to this package; nothing rewrites a trace line after it is written.
Fully inert when telos_enabled is off: no directories created, no reads.
"""

from __future__ import annotations

import logging

from config import settings

logger = logging.getLogger("pernix.telos")


async def run_step(is_cancelled) -> dict:
    """One fast-loop unit. Called from snooze Activity 16. Round-robins
    between evaluating a gated hypothesis and generating hypotheses for the
    next scheduled question (85% goal-linked / 15% serendipity), mirroring
    Dream's validate/generate fairness. Never raises upward."""
    stats = {
        "telos_questions": 0,
        "telos_hypotheses": 0,
        "telos_gated": 0,
        "telos_souped": 0,
        "telos_evaluated": 0,
        "telos_claims": 0,
    }
    if not settings.telos_enabled:
        return stats

    from core.telos.store import TelosStore
    from db import models as db

    store = TelosStore.open()

    # Seed the root and config provenance record on first enable.
    store.ensure_root()

    gated = store.list_hypotheses(status="gated")
    last_action = db.get_snooze_state("telos_last_action") or ""
    backlogged = len(gated) > max(1, settings.telos_max_gated_backlog)

    did = None
    if gated and (last_action != "evaluate" or backlogged):
        from core.telos.evaluate import evaluate_one

        outcome = await evaluate_one(store, gated, is_cancelled)
        if outcome:
            stats["telos_evaluated"] = 1
            if outcome in ("supported", "refuted"):
                stats["telos_claims"] = 1
            did = "evaluate"
    if did is None and not backlogged and not is_cancelled():
        from core.telos.soup import generate_for_next_question

        result = await generate_for_next_question(store, is_cancelled)
        stats["telos_hypotheses"] = result.get("generated", 0)
        stats["telos_gated"] = result.get("gated", 0)
        stats["telos_souped"] = result.get("souped", 0)
        if result.get("ran"):
            did = "generate"

    if did:
        db.set_snooze_state("telos_last_action", did)

    return stats


async def run_slow_loops(force_weekly: bool = False) -> dict:
    """Daily slow-loop pass (telos cron): Ordo Pass + Binding Monitor every
    run; Hevel rollup, ledger reconciliation, and Entropy Control weekly
    (watermarked); dream-register review monthly. Never raises upward."""
    stats: dict = {}
    if not settings.telos_enabled:
        return stats

    from datetime import datetime, timezone

    from core.telos.store import TelosStore
    from db import models as db

    store = TelosStore.open()
    store.ensure_root()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Daily: Ordo Pass — re-ranking, never a purge.
    try:
        from core.telos.ordo import run_ordo_pass

        stats["ordo"] = run_ordo_pass(store)
    except Exception as e:
        logger.warning("telos: ordo pass failed: %s", e)

    # Daily: Binding Monitor — the idolatry/Goodhart detector.
    try:
        from core.telos.binding import run_binding_monitor

        stats["binding"] = run_binding_monitor(store)
    except Exception as e:
        logger.warning("telos: binding monitor failed: %s", e)

    # Daily: release adaptive slots this layer no longer has evidence for.
    # Minting without retiring wedges the per-kind cap, at which point every
    # further supported claim is rejected and the loop looks like a loop with
    # nothing to say (see core/telos/retire.py).
    try:
        from core.telos.retire import retire_stale_hints

        stats["adaptive_retire"] = retire_stale_hints(store)
    except Exception as e:
        logger.warning("telos: adaptive hint retirement failed: %s", e)

    # Daily: take terminal hypotheses out of the pool. A gate_reason that
    # says the falsifier cannot be checked at all is a verdict, not a
    # holding pattern — those entries move to soup/archive/ and stop being
    # re-read by every generate/evaluate pass (see core/telos/retire.py).
    try:
        from core.telos.retire import archive_untestable_pool

        stats["soup_untestable"] = archive_untestable_pool(store)
    except Exception as e:
        logger.warning("telos: untestable-pool sweep failed: %s", e)

    # Daily: bound the speculation pool on the age axis. Nothing reads
    # status='soup', but the generate/evaluate loop re-reads every file on
    # disk each pass, so an unbounded pool is a growing tax on the hot path,
    # not just on storage. Aged entries are archived 'expired', never deleted.
    try:
        from core.telos.retire import prune_speculation_pool

        stats["soup_prune"] = prune_speculation_pool(store)
    except Exception as e:
        logger.warning("telos: speculation-pool prune failed: %s", e)

    # Daily: the archive's own horizon — the only place a hypothesis file is
    # unlinked, and long by default (the archive is the calibration record).
    try:
        from core.telos.retire import prune_soup_archive

        stats["soup_archive_prune"] = prune_soup_archive(store)
    except Exception as e:
        logger.warning("telos: soup-archive prune failed: %s", e)

    # Weekly block, watermarked so cron cadence changes can't double-run it.
    week = datetime.now(timezone.utc).strftime("%G-W%V")
    if force_weekly or db.get_snooze_state("telos_weekly") != week:
        try:
            from core.telos.hevel import run_hevel_rollup

            stats["hevel"] = run_hevel_rollup(store)
        except Exception as e:
            logger.warning("telos: hevel rollup failed: %s", e)
        try:
            from core.telos.reconcile import run_reconciliation

            stats["reconcile"] = await run_reconciliation(store)
        except Exception as e:
            logger.warning("telos: reconciliation failed: %s", e)
        try:
            from core.telos.entropy import run_entropy_control

            stats["entropy"] = run_entropy_control(store)
        except Exception as e:
            logger.warning("telos: entropy control failed: %s", e)
        db.set_snooze_state("telos_weekly", week)

    # Monthly: dream register review (capability-gap test, successor minting
    # is operator work — this pass only flags, never demotes silently).
    month = today[:7]
    if db.get_snooze_state("telos_dream_review") != month:
        try:
            from core.telos.ordo import review_dream_register

            stats["dream_register"] = review_dream_register(store)
        except Exception as e:
            logger.warning("telos: dream register review failed: %s", e)
        db.set_snooze_state("telos_dream_review", month)

    store.trace_append("slow_loops", {"date": today, "stats": {k: v for k, v in stats.items() if v}})
    return stats
