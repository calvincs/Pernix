"""Pernix — Adaptive tripwire (plan 4f): did a batch make the agent worse?

Two signals per applied batch:
  primary (active)  — the post-batch canary sweep (canary_runs joined on
                      batch_id) vs. the trailing scheduled sweeps' baseline;
                      a pass-rate drop >= canary_regression_delta flags the
                      batch. Flaky canaries inform, never trip.
  secondary (passive) — organic post-mortem retry drift over
                      adaptive_tripwire_window_turns turns after the apply
                      vs. the same window before (canary-stamped rows
                      excluded — §5 landed that stamp for exactly this).

Either signal → status='suspect' + a notification. A later clean comparison
clears the flag (status back to 'applied', cleared_at stamped) — the other
clear path is human dismiss via the API. adaptive_auto_rollback promotes a
PRIMARY hit to automatic rollback once that metric has earned trust; the
passive signal never auto-rolls-back (too noisy by construction).
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


def _pass_rate(rows: list[dict]) -> float | None:
    if not rows:
        return None
    return sum(1 for r in rows if r.get("passed")) / len(rows)


def _canary_signal(batch: dict, flaky: set[str]) -> tuple[bool, str] | None:
    """(regressed, detail) from the post-batch sweep, or None when no sweep
    data exists yet (batch stays as-is; the sweep may still be queued)."""
    post = [r for r in db.list_canary_runs(batch_id=batch["batch_id"], limit=500) if r["task"] not in flaky]
    if not post:
        return None
    tasks = {r["task"] for r in post}
    per_task = max(1, settings.canary_baseline_runs)
    baseline_rows: list[dict] = []
    for task in tasks:
        rows = [
            r
            for r in db.list_canary_runs(task=task, limit=200)
            if r.get("trigger") == "scheduled" and (r.get("created_at") or "") < (batch.get("created_at") or "")
        ]
        baseline_rows.extend(rows[:per_task])  # list is newest-first
    base = _pass_rate(baseline_rows)
    now = _pass_rate(post)
    if base is None or now is None:
        return None
    drop = base - now
    detail = f"canary pass rate {now:.0%} vs baseline {base:.0%} ({len(post)} post-batch, {len(baseline_rows)} baseline runs)"
    return (drop >= settings.canary_regression_delta, detail)


def _post_mortem_signal(batch: dict) -> tuple[bool, str] | None:
    """(drifted, detail) from organic reflect-retry rates around the apply."""
    window = max(1, settings.adaptive_tripwire_window_turns)
    applied_at = batch.get("created_at") or ""

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

    after = _organic(db.list_post_mortems(since_iso=applied_at, limit=window * 3))[:window]
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
        batches = [b for b in db.adaptive_list_batches(limit=200) if b.get("status") in ("applied", "suspect")]
    except Exception as e:
        logger.warning("Tripwire batch listing failed: %s", e)
        return actions

    for batch in batches:
        bid = batch["batch_id"]
        try:
            canary = _canary_signal(batch, flaky)
            pm = _post_mortem_signal(batch)
            regressed = (canary is not None and canary[0]) or (pm is not None and pm[0])
            details = "; ".join(d for s in (canary, pm) if s is not None for d in [s[1]])

            if regressed and batch["status"] == "applied":
                db.adaptive_update_batch(bid, status="suspect", flagged_reason=details)
                actions.append({"batch_id": bid, "action": "flagged", "detail": details})
                db.add_notification(
                    title="Adaptive tripwire: batch flagged suspect",
                    body=f"Batch {bid}: {details}. Review in the Adaptive panel.",
                    urgency="high",
                )
                if settings.adaptive_auto_rollback and canary is not None and canary[0]:
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
