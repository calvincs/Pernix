"""Pernix — Retention sweeps: age out the transient records the system emits.

Cron runs, post-mortems, canary runs and RLM run
directories all accumulate as a side effect of normal operation. Nothing here
decides *when* to sweep — snooze's activity ladder (Activities 7, 11, 12, 12a,
12c) and maintenance's 24h fallback own the cadence and the watermarks. These
are plain functions with explicit budgets so both callers share one
implementation.

Every function swallows its own errors and reports what it managed to delete;
a retention sweep must never take down the cycle that called it.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import settings
from core.pools import run_background
from db import models as db

logger = logging.getLogger("pernix.retention")


# ---------------------------------------------------------------------------
# Cron runs, cron-created sessions, state log
# ---------------------------------------------------------------------------


def prune_cron(
    *,
    max_age_days: int = 30,
    keep_per_job: int = 100,
    session_max_age_days: int = 7,
    state_log_max_age_days: int = 30,
    state_log_keep_per_session: int = 500,
) -> dict[str, int]:
    """Prune cron run rows, cron-created sessions, and the state log.

    State-log retention keeps the N most recent rows per session regardless of
    age (so the last turn of every session stays inspectable) and drops
    anything older than the age cutoff beyond that floor.

    Returns {"runs", "sessions", "state_log"} counts.
    """
    return {
        "runs": db.prune_cron_runs(max_age_days=max_age_days, keep_per_job=keep_per_job),
        "sessions": db.prune_cron_sessions(max_age_days=session_max_age_days),
        "state_log": db.prune_state_log(
            max_age_days=state_log_max_age_days, keep_per_session=state_log_keep_per_session
        ),
    }


# ---------------------------------------------------------------------------
# Post-mortems
# ---------------------------------------------------------------------------


def prune_post_mortems(retention_days: int | None = None) -> int:
    """Delete synthesized post-mortems older than the retention window.

    Only sweeps rows already processed by synthesis — never touches the
    unsynthesized backlog, so a backlogged run won't lose data. Cheap no-op
    once caught up.
    """
    days = max(int((retention_days if retention_days is not None else settings.post_mortem_retention_days) or 0), 1)
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        deleted = db.delete_old_post_mortems(cutoff_iso)
    except Exception as e:
        logger.warning("Snooze post-mortem cleanup failed: %s", e)
        return 0
    if deleted:
        logger.info("Snooze post-mortem cleanup: deleted %d rows older than %d days", deleted, days)
    return deleted


def prune_notifications(retention_days: int | None = None) -> int:
    """Delete notification rows past the retention window (0 = keep forever).

    The bell is a recent-events surface, not an archive: until v3.1 the
    table had no pruner at all, and idle-loop producers refill it on a
    fixed cadence while it only ever shrank by manual dismiss clicks.
    """
    days = int((retention_days if retention_days is not None else settings.notification_retention_days) or 0)
    if days <= 0:
        return 0
    try:
        deleted = db.prune_notifications(days)
    except Exception as e:
        logger.warning("Notification cleanup failed: %s", e)
        return 0
    if deleted:
        logger.info("Notification cleanup: deleted %d rows older than %d days", deleted, days)
    return deleted


# ---------------------------------------------------------------------------
# Canary runs
# ---------------------------------------------------------------------------


async def prune_canary_runs(retention_days: int | None = None) -> tuple[int, int]:
    """Prune canary_runs rows and stale canary sessions past retention.

    Session prune mirrors the dream-journal pattern (core/dream/journal.py):
    list, filter by type + age, delete. Scoring rows outlive their session only
    inside the retention window — the tripwire's baseline math reads
    canary_runs, never the sessions.

    Returns (rows_deleted, sessions_deleted).
    """
    days = max(int((retention_days if retention_days is not None else settings.canary_retention_days) or 0), 1)
    try:
        deleted = await asyncio.to_thread(db.prune_canary_runs, days)
        pruned_sessions = await prune_sessions_of_type("canary", days)
    except Exception as e:
        logger.warning("Snooze canary cleanup failed: %s", e)
        return 0, 0
    if deleted or pruned_sessions:
        logger.info(
            "Snooze canary cleanup: %d run row(s), %d session(s) older than %dd",
            deleted,
            pruned_sessions,
            days,
        )
    return deleted, pruned_sessions


async def prune_sessions_of_type(session_type: str, retention_days: int, *, keep: set[str] | None = None) -> int:
    """Delete sessions of one type not updated within the retention window.

    A direct query by type and age — never a window over the newest rows.
    The old loop over list_sessions(500) could not see the oldest sessions
    once the table passed 500 rows (161 outside the window on the live box,
    one journal already past its retention with no way to be pruned).
    """
    days = max(int(retention_days or 0), 1)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    ids = await asyncio.to_thread(db.list_session_ids_by_type_before, session_type, cutoff)
    pruned = 0
    for sid in ids:
        if keep and sid in keep:
            continue
        try:
            await asyncio.to_thread(db.delete_session, sid)
            pruned += 1
        except Exception as e:
            logger.warning("Retention: could not delete %s session %s: %s", session_type, sid, e)
    return pruned


async def prune_worker_sessions(retention_days: int | None = None) -> int:
    """Worker sessions past worker_session_retention_days, except any a
    parent is still waiting on. The worker's result already lives in the
    parent's transcript; the worker transcript is debugging residue."""
    days = retention_days if retention_days is not None else settings.worker_session_retention_days
    try:
        keep = await asyncio.to_thread(db.watched_worker_ids)
        pruned = await prune_sessions_of_type("worker", days, keep=keep)
    except Exception as e:
        logger.warning("Snooze worker-session cleanup failed: %s", e)
        return 0
    if pruned:
        logger.info("Snooze worker cleanup: %d worker session(s) older than %dd", pruned, max(int(days or 0), 1))
    return pruned


_TERMINAL_HYPOTHESIS_STATUSES = ("refuted", "expired", "archived", "promoted")


def prune_dream_hypotheses(retention_days: int | None = None) -> int:
    """Dream hypotheses in a terminal status older than
    dream_hypothesis_retention_days. Pending and validated rows are never
    touched — they are work, not residue."""
    days = max(int(retention_days if retention_days is not None else settings.dream_hypothesis_retention_days), 1)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        deleted = db.delete_old_dream_hypotheses(cutoff, _TERMINAL_HYPOTHESIS_STATUSES)
    except Exception as e:
        logger.warning("Snooze dream-hypothesis cleanup failed: %s", e)
        return 0
    if deleted:
        logger.info("Snooze dream cleanup: %d terminal hypothesis row(s) older than %dd", deleted, days)
    return deleted


async def nudge_stale_canaries(max_age_days: int = 90) -> int:
    """Notify once per canary whose last_reviewed is over max_age_days old.

    Staleness nudge (plan §10.8 / §12.2): the watermark is keyed on the
    reviewed date, so the nudge self-rearms when a human bumps it. No LLM, no
    writes to the suite. Returns the number of notifications raised.
    """
    raised = 0
    try:
        from core.canary import scan_canaries

        for c in await run_background(scan_canaries):
            if not c.last_reviewed:
                continue
            try:
                reviewed = datetime.fromisoformat(str(c.last_reviewed))
                if reviewed.tzinfo is None:
                    reviewed = reviewed.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if (datetime.now(timezone.utc) - reviewed).days < max_age_days:
                continue
            key = f"canary_stale_notified:{c.name}:{c.last_reviewed}"
            if db.get_snooze_state(key):
                continue
            db.add_notification(
                title=f"Canary '{c.name}' is stale",
                body=(
                    f"last_reviewed {c.last_reviewed} is over {max_age_days} days old. Re-verify its "
                    f"gates still reflect a daily-driver task, then bump last_reviewed."
                ),
                urgency="normal",
            )
            db.set_snooze_state(key, "1")
            raised += 1
    except Exception as e:
        logger.warning("Canary staleness nudge failed: %s", e)
    return raised


# ---------------------------------------------------------------------------
# RLM run directories
# ---------------------------------------------------------------------------


async def prune_rlm_runs(retention_days: int | None = None) -> int:
    """Delete RLM run directories + rows older than the retention window.

    The durable output of a run is the tool result already in the session
    transcript; the run dir (staged context copies, trace.jsonl, child.log) is
    debugging residue. Root runs only — nested runs live inside their parent's
    dir and their rows cascade via delete_rlm_run. Running runs are never
    touched (list_rlm_runs_before excludes them).
    """
    days = max(int(retention_days if retention_days is not None else settings.rlm_run_retention_days), 1)
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        stale = db.list_rlm_runs_before(cutoff_iso)
    except Exception as e:
        logger.warning("Snooze RLM cleanup: could not list runs: %s", e)
        return 0
    if not stale:
        return 0

    workspace_dir = Path(settings.workspace_dir)
    deleted = 0
    for run in stale:
        run_id = run["run_id"]
        run_dir = workspace_dir / run["run_dir"]
        try:
            # rmtree is blocking filesystem recursion; keep the loop responsive.
            if run_dir.exists():
                await run_background(shutil.rmtree, run_dir)
            db.delete_rlm_run(run_id)
            # The run's sidebar view session goes with it — it is pure
            # navigation chrome over the (now deleted) trace. Mirror of
            # manager._purge_rlm_artifacts, which handles the other direction
            # (session deleted first).
            if run.get("ui_session_id"):
                db.delete_session(run["ui_session_id"])
            deleted += 1
        except Exception as e:
            logger.warning("Snooze RLM cleanup: error deleting run %s: %s", run_id, e)

    if deleted:
        logger.info("Snooze RLM cleanup: deleted %d old run(s)", deleted)
    return deleted
