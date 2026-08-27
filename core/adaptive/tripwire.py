"""Pernix — Adaptive tripwire (plan 4f): did a batch make the agent worse?

Two signals per applied batch:
  primary (active)  — PER-TASK verdicts from the post-batch canary sweep
                      (canary_runs joined on batch_id). A non-flaky canary
                      whose trailing canary_baseline_runs runs before the
                      apply were all green, now recording gate_fail with no
                      pass, has regressed; the sweep's confirm-rerun means a
                      CONFIRMED regression shows two gate_fail rows for the
                      (batch, task) pair. Only outcome='gate_fail' counts —
                      timeouts, harness errors and noop runs are suite-health
                      concerns and can neither trip nor certify a batch.
                      (The old aggregate pass-rate-delta form had a dead
                      zone: one failure among 8 canaries was a 12.5% drop,
                      under the 15% delta, so the signal could never fire.)
  secondary (passive) — organic post-mortem retry drift over
                      adaptive_tripwire_window_turns turns after the apply
                      vs. the same window before (canary-stamped rows
                      excluded — §5 landed that stamp for exactly this);
                      drift >= canary_regression_delta flags.

Either signal → status='suspect' + a notification. A later clean comparison
clears the flag (status back to 'applied', cleared_at stamped) — the other
clear path is human dismiss via the API. cleared_at is TERMINAL either way:
a cleared batch drops out of the sweep, so a dismiss is not re-litigated on
the next cycle against the very evidence it dismissed. adaptive_auto_rollback
promotes a CONFIRMED primary hit to automatic rollback; an unconfirmed hit
(the rerun itself died) flags but never rolls back, and the passive signal
never auto-rolls-back (too noisy by construction).
"""

from __future__ import annotations

import json
import logging

from config import settings
from db import models as db

logger = logging.getLogger("pernix.adaptive")


def _flaky_tasks() -> set[str]:
    try:
        from core.canary import scan_canaries

        return {c.name for c in scan_canaries() if c.flaky}
    except Exception:
        return set()


def _applied_at(batch: dict) -> str:
    """Wall-clock of the APPLY, not the queue.

    adaptive_batches.created_at is stamped when the batch is QUEUED; a batch
    can sit pending for hours before the idle window drains it. The batch's
    adaptive_events are written during apply, so the earliest non-rollback
    one is the real boundary. No events (nothing landed) → created_at.
    """
    try:
        events = db.adaptive_events_for_batch(batch["batch_id"])  # ascending id
    except Exception:
        events = []
    for ev in events:
        if ev.get("action") != "rollback" and ev.get("created_at"):
            return ev["created_at"]
    return batch.get("created_at") or ""


def _canary_signal(batch: dict, flaky: set[str], applied_at: str) -> tuple[bool, bool, str] | None:
    """(regressed, confirmed, detail) — per-task verdicts from the post-batch
    sweep — or None when no usable sweep data exists yet (batch stays as-is;
    the sweep may still be queued).

    A task testifies against the batch only when it has earned the right to:
    its trailing runs before the apply must all be green (the precondition).
    Only outcome='gate_fail' rows count as failures — timeouts, errors and
    noop runs are suite-health trouble, and legacy pre-v30 rows (outcome
    NULL) can only ever count as passes, never as evidence of regression.
    `confirmed` means the sweep's confirm-rerun also gate-failed (>= 2 rows);
    a single unconfirmed gate_fail flags but must never auto-roll-back.
    """
    post = [r for r in db.list_canary_runs(batch_id=batch["batch_id"], limit=500) if r["task"] not in flaky]
    if not post:
        return None

    per_task: dict[str, list[dict]] = {}
    for r in post:
        per_task.setdefault(r["task"], []).append(r)

    window = max(1, settings.canary_baseline_runs)
    judged = 0
    regressed_tasks: list[str] = []
    confirmed_tasks: list[str] = []
    for task, rows in sorted(per_task.items()):
        usable = [
            r for r in rows if r.get("outcome") in ("pass", "gate_fail") or (not r.get("outcome") and r.get("passed"))
        ]
        if not usable:
            continue  # nothing but timeouts/errors/noops — measures the harness, not the batch
        history = [r for r in db.list_canary_runs(task=task, limit=200) if (r.get("created_at") or "") < applied_at]
        history = history[:window]  # newest-first
        if len(history) < min(3, window) or not all(r.get("passed") for r in history):
            continue  # no green precondition — this task cannot testify
        judged += 1
        fails = sum(1 for r in usable if r.get("outcome") == "gate_fail")
        if fails == 0 or any(r.get("passed") for r in usable):
            continue
        regressed_tasks.append(task)
        if fails >= 2:
            confirmed_tasks.append(task)

    if judged == 0:
        # Every post-batch row was either unusable or belonged to a task with
        # no green history. That is a statement about the suite, not the
        # batch — report "no usable signal" instead of a false all-clear;
        # core/canary/maintain.py raises suite outages separately.
        logger.warning(
            "Tripwire: no canary task could testify for batch %s (%d post-batch rows) — "
            "treating the canary signal as unavailable.",
            batch.get("batch_id"),
            len(post),
        )
        return None

    if not regressed_tasks:
        return (False, True, f"canary verdicts clean ({judged} task(s) judged, {len(post)} post-batch runs)")
    parts = []
    for task in regressed_tasks:
        parts.append(
            f"{task} ({'confirmed, 2 gate_fails' if task in confirmed_tasks else 'unconfirmed, rerun missing'})"
        )
    return (True, bool(confirmed_tasks), "canary regression: " + ", ".join(parts))


def _post_mortem_signal(batch: dict, applied_at: str) -> tuple[bool, str] | None:
    """(drifted, detail) from organic reflect-retry rates around the apply."""
    window = max(1, settings.adaptive_tripwire_window_turns)

    def _organic(rows: list[dict]) -> list[dict]:
        out = []
        for r in rows:
            try:
                if json.loads(r.get("payload_json") or "{}").get("session_type") == "canary":
                    continue
            except (TypeError, ValueError):
                pass
            out.append(r)
        return out

    # ASCENDING feed: the window we want is the turns IMMEDIATELY after the
    # apply. list_post_mortems is newest-first, so slicing it would compare
    # the newest turns overall — a moving target that drifts away from the
    # batch as the system keeps running.
    after = _organic(db.list_post_mortems_since(applied_at, limit=window * 3))[:window]
    if len(after) < window:
        return None  # not enough organic turns yet — keep waiting
    before = _organic([r for r in db.list_post_mortems(limit=window * 6) if (r.get("created_at") or "") < applied_at])[
        :window
    ]
    if len(before) < window:
        return None

    def _retry_rate(rows: list[dict]) -> float:
        return sum(1 for r in rows if r.get("verdict") != "pass") / len(rows)

    rate_after, rate_before = _retry_rate(after), _retry_rate(before)
    drift = rate_after - rate_before
    detail = f"post-mortem retry rate {rate_after:.0%} vs {rate_before:.0%} over {window}-turn windows"
    return (drift >= settings.canary_regression_delta, detail)


def evaluate_tripwire() -> list[dict]:
    """Sweep applied + suspect batches. Returns actions taken. Never raises."""
    actions: list[dict] = []
    if not settings.adaptive_enabled:
        return actions
    flaky = _flaky_tasks()
    try:
        batches = [
            b
            for b in db.adaptive_list_batches(limit=200)
            # cleared_at is terminal. A human dismiss (or an earlier clean
            # comparison) settles the batch for good — without this the sweep
            # re-derives the same signal from the same rows and re-flags the
            # batch on every cycle, so "dismiss" never sticks.
            if b.get("status") in ("applied", "suspect") and not b.get("cleared_at")
        ]
    except Exception as e:
        logger.warning("Tripwire batch listing failed: %s", e)
        return actions

    for batch in batches:
        bid = batch["batch_id"]
        try:
            applied_at = _applied_at(batch)
            canary = _canary_signal(batch, flaky, applied_at)
            pm = _post_mortem_signal(batch, applied_at)
            canary_regressed = canary is not None and canary[0]
            canary_confirmed = canary_regressed and canary[1]
            regressed = canary_regressed or (pm is not None and pm[0])
            details = "; ".join(d for d in ((canary[2] if canary else ""), (pm[1] if pm else "")) if d)

            if regressed and batch["status"] == "applied":
                db.adaptive_update_batch(bid, status="suspect", flagged_reason=details)
                actions.append({"batch_id": bid, "action": "flagged", "detail": details})
                db.add_notification(
                    title="Adaptive tripwire: batch flagged suspect",
                    body=f"Batch {bid}: {details}. Review in the Adaptive panel.",
                    urgency="high",
                )
                if settings.adaptive_auto_rollback and canary_confirmed:
                    from core.adaptive.engine import rollback

                    rollback(batch_id=bid, actor="tripwire")
                    actions.append({"batch_id": bid, "action": "auto_rolled_back", "detail": details})
                    db.add_notification(
                        title="Adaptive tripwire: batch auto-rolled-back",
                        body=f"Batch {bid} rolled back on canary regression: {details}",
                        urgency="high",
                    )
            elif not regressed and batch["status"] == "suspect" and (canary is not None or pm is not None):
                # A subsequent clean comparison clears the flag.
                from db.models import _now as _now_fn

                db.adaptive_update_batch(bid, status="applied", cleared_at=_now_fn())
                actions.append({"batch_id": bid, "action": "cleared", "detail": details})
        except Exception as e:
            logger.warning("Tripwire evaluation failed for batch %s: %s", bid, e)
    return actions
