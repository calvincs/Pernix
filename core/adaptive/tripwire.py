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
  secondary (passive) — organic post-mortem OUTCOME drift across the apply,
                      measured with a two-proportion z-test: up to
                      adaptive_tripwire_window_turns graded turns before the
                      apply vs. the graded turns after it, at least 30 per
                      side, canary-stamped rows excluded (§5 landed that
                      stamp for exactly this). Per-turn outcome is the user's
                      own signal when one exists (post_mortems.user_signal,
                      migration v36) and reflect's verdict otherwise.
                      p<0.05 and worse flags; p<0.01 and worse may roll back.
                      (The old form compared retry RATES over 20-turn
                      windows. n=20 is noise: on the live box it flagged
                      seven batches with lines like "retry rate 50% vs 30%"
                      and rolled back none of them, because a 3-turn swing
                      moves that ratio 15 points.)

Either signal → status='suspect' + a notification. A later clean comparison
clears the flag (status back to 'applied', cleared_at stamped) — the other
clear path is human dismiss via the API. cleared_at is TERMINAL either way:
a cleared batch drops out of the sweep, so a dismiss is not re-litigated on
the next cycle against the very evidence it dismissed. adaptive_auto_rollback
promotes a CONFIRMED primary hit to automatic rollback; an unconfirmed hit
(the rerun itself died) flags but never rolls back. The passive signal rolls
back only when it is significant at p<0.01 AND both adaptive_auto_rollback
and adaptive_pm_drift_rollback are on — one flag for "the tripwire may
rollback", one for "this channel has earned it".
"""

from __future__ import annotations

import json
import logging
import math

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

    outcome='contaminated' rows are dropped from BOTH windows (W5): a run that
    broke canary isolation measured something other than the pipeline, so it
    can neither testify against a batch nor stand in a green precondition.
    """
    post = [
        r
        for r in db.list_canary_runs(batch_id=batch["batch_id"], limit=500)
        if r["task"] not in flaky and r.get("outcome") != "contaminated"
    ]
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
        history = [
            r
            for r in db.list_canary_runs(task=task, limit=200)
            if (r.get("created_at") or "") < applied_at and r.get("outcome") != "contaminated"
        ]
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


# Post-mortem drift test. Both windows must reach MIN_N before the batch is
# measured at all: the destructive action at the end of this chain is a
# rollback, and a test that can be moved by three turns is not evidence.
PM_DRIFT_MIN_N = 30
PM_DRIFT_ALPHA_FLAG = 0.05
PM_DRIFT_ALPHA_ROLLBACK = 0.01
_PM_DRIFT_MAX_N_CEILING = 200


def two_proportion_z_test(successes_a: int, n_a: int, successes_b: int, n_b: int) -> tuple[float, float]:
    """Pooled two-proportion z-test. Returns (z, two-sided p).

    H0: the two samples share one underlying success rate. `z` is signed
    from A's perspective — negative means A did worse than B — and `p` is
    the two-sided normal tail, computed with erfc so nothing here needs
    scipy. Degenerate inputs (an empty sample, or a pooled rate of exactly
    0 or 1, which makes the variance zero) return (0.0, 1.0): no evidence,
    rather than an infinity that would read as certainty.
    """
    if n_a <= 0 or n_b <= 0:
        return (0.0, 1.0)
    p_a = successes_a / n_a
    p_b = successes_b / n_b
    pooled = (successes_a + successes_b) / (n_a + n_b)
    var = pooled * (1.0 - pooled) * (1.0 / n_a + 1.0 / n_b)
    if var <= 0.0:
        return (0.0, 1.0)
    z = (p_a - p_b) / math.sqrt(var)
    p = math.erfc(abs(z) / math.sqrt(2.0))
    return (z, p)


def _organic(rows: list[dict]) -> list[dict]:
    """Drop canary-stamped post-mortems — a canary turn measures the suite."""
    out = []
    for r in rows:
        try:
            if json.loads(r.get("payload_json") or "{}").get("session_type") == "canary":
                continue
        except (TypeError, ValueError):
            pass
        out.append(r)
    return out


def _turn_succeeded(row: dict) -> bool:
    """Ground truth for one graded turn: the user's signal outranks the LLM.

    `post_mortems.user_signal` arrives with migration v36 and is NULL for
    every turn nobody reacted to, so `.get` covers both the pre-migration
    schema (key absent from the SELECT * row) and the common NULL — either
    way the fallback is reflect's own verdict.
    """
    signal = (row.get("user_signal") or "").strip().lower()
    if signal in ("up", "down"):
        return signal == "up"
    return row.get("verdict") == "pass"


def _post_mortem_signal(batch: dict, applied_at: str) -> tuple[bool, bool, str] | None:
    """(flagged, rollback_worthy, detail) from organic outcome drift, or None.

    None means "not measurable yet" — fewer than PM_DRIFT_MIN_N graded
    organic turns on one side of the apply. The caller leaves the batch
    exactly as it found it in that case.
    """
    max_n = max(PM_DRIFT_MIN_N, min(_PM_DRIFT_MAX_N_CEILING, int(settings.adaptive_tripwire_window_turns or 0)))

    # ASCENDING feed: the window we want is the turns IMMEDIATELY after the
    # apply. list_post_mortems is newest-first, so slicing it would compare
    # the newest turns overall — a moving target that drifts away from the
    # batch as the system keeps running.
    after = _organic(db.list_post_mortems_since(applied_at, limit=max_n * 3))[:max_n]
    if len(after) < PM_DRIFT_MIN_N:
        return None  # not enough organic turns yet — keep waiting
    before = _organic([r for r in db.list_post_mortems(limit=max_n * 6) if (r.get("created_at") or "") < applied_at])[
        :max_n
    ]
    if len(before) < PM_DRIFT_MIN_N:
        return None

    n_after, n_before = len(after), len(before)
    ok_after = sum(1 for r in after if _turn_succeeded(r))
    ok_before = sum(1 for r in before if _turn_succeeded(r))
    z, p = two_proportion_z_test(ok_after, n_after, ok_before, n_before)
    worse = (ok_after / n_after) < (ok_before / n_before)
    detail = (
        f"post-mortem outcome drift: {ok_after}/{n_after} ({ok_after / n_after:.0%}) succeeded after the apply "
        f"vs {ok_before}/{n_before} ({ok_before / n_before:.0%}) before "
        f"(two-proportion z={z:.2f}, p={p:.4f})"
    )
    return (worse and p < PM_DRIFT_ALPHA_FLAG, worse and p < PM_DRIFT_ALPHA_ROLLBACK, detail)


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _expire_stale_suspect(batch: dict, actions: list[dict]) -> bool:
    """Auto-clear a passive-only suspect past adaptive_suspect_ttl_days.

    Returns True when the batch was cleared (caller skips re-evaluation).
    Canary-confirmed flags are exempt: their reason names the regression and
    a human (or rollback) should settle them.
    """
    from datetime import datetime, timezone

    ttl = int(settings.adaptive_suspect_ttl_days or 0)
    if ttl <= 0:
        return False
    reason = str(batch.get("flagged_reason") or "")
    if "canary regression" in reason:
        return False
    bid = batch["batch_id"]
    key = f"adaptive_suspect_since:{bid}"
    raw = db.get_snooze_state(key)
    if not raw:
        # Legacy suspect flagged before the marker existed — start its clock.
        db.set_snooze_state(key, _now_iso())
        return False
    try:
        since = datetime.fromisoformat(str(raw))
    except ValueError:
        return False
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    if (datetime.now(timezone.utc) - since).days < ttl:
        return False
    from db.models import _now as _now_fn

    db.adaptive_update_batch(
        bid,
        status="applied",
        cleared_at=_now_fn(),
        flagged_reason=reason + f" [auto-cleared: passive-signal flag aged past {ttl}d with no confirmation]",
    )
    actions.append({"batch_id": bid, "action": "suspect_expired", "detail": reason})
    return True


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
            # A suspect flagged by the PASSIVE signal alone can never
            # self-clear: its comparison windows are frozen at the apply, so
            # every sweep re-derives the same verdict and only a human
            # dismiss ended it (4 batches sat suspect for 12 days on the
            # live box). Passive-only flags now expire after
            # adaptive_suspect_ttl_days — the notification already fired, a
            # passive flag rolls back only under its own extra flag, and a
            # canary-confirmed regression (reason starts "canary regression:")
            # is exempt. The marker is stamped at flag time; a legacy suspect
            # with no marker gets one on first sight and expires from then.
            if batch["status"] == "suspect" and _expire_stale_suspect(batch, actions):
                continue

            applied_at = _applied_at(batch)
            canary = _canary_signal(batch, flaky, applied_at)
            pm = _post_mortem_signal(batch, applied_at)
            canary_regressed = canary is not None and canary[0]
            canary_confirmed = canary_regressed and canary[1]
            pm_regressed = pm is not None and pm[0]
            # The passive channel's own permission: significant at the
            # stricter alpha AND both flags on. adaptive_auto_rollback has
            # only ever meant "a CONFIRMED canary regression may undo a
            # batch"; widening it silently to a statistical signal that has
            # never rolled anything back would be a different promise.
            pm_rollback = (
                pm is not None and pm[1] and settings.adaptive_auto_rollback and settings.adaptive_pm_drift_rollback
            )
            regressed = canary_regressed or pm_regressed
            details = "; ".join(d for d in ((canary[2] if canary else ""), (pm[2] if pm else "")) if d)

            if regressed and batch["status"] == "applied":
                db.adaptive_update_batch(bid, status="suspect", flagged_reason=details)
                db.set_snooze_state(f"adaptive_suspect_since:{bid}", _now_iso())
                actions.append({"batch_id": bid, "action": "flagged", "detail": details})
                db.add_notification(
                    title="Adaptive tripwire: batch flagged suspect",
                    body=f"Batch {bid}: {details}. Review in the Adaptive panel.",
                    urgency="high",
                )
                why = ""
                if settings.adaptive_auto_rollback and canary_confirmed:
                    why = "canary regression"
                elif pm_rollback:
                    why = "post-mortem outcome drift"
                if why:
                    from core.adaptive.engine import rollback

                    rollback(batch_id=bid, actor="tripwire")
                    actions.append({"batch_id": bid, "action": "auto_rolled_back", "detail": details})
                    db.add_notification(
                        title="Adaptive tripwire: batch auto-rolled-back",
                        body=f"Batch {bid} rolled back on {why}: {details}",
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
