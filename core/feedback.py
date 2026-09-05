"""Pernix — User feedback as ground truth (trust-loop hardening, 2026-09-04).

A thumb on an assistant message is the only outcome in the whole loop that
nothing in the system authored. It outranks the next-message reading, which
outranks the reflect verdict — so when it lands it becomes the turn's
`outcome_source`, and when it CONTRADICTS the verdict it also corrects the
per-entry usefulness counters that verdict fed.

That correction is the point. `scout_signals` is what retirement and ranking
divide by; a policy credited with a success on a turn the user hated is a lie
the loop keeps compounding. Here the credit goes back.

Attribution's forward path (post-mortem → signals) belongs to
`core/synthesis.attribute()` and is not touched from here: this module only
reverses or adds the delta a thumb disagrees with, records exactly what it
applied on the post-mortem, and undoes that record on a flip or a removal.

Nothing here raises: it is called from an HTTP handler whose job is to store
the user's click, and a signal-bookkeeping problem must never turn that into
a 500.
"""

from __future__ import annotations

import json
import logging

from db import models as db

logger = logging.getLogger("pernix.feedback")

# Entry ids the turn used or cited live under these signal rows.
_SIGNAL_TYPE = "adaptive_entry"

# Where the applied correction is remembered, so a second write of the same
# thumb changes nothing and a flip reverses exactly what the first one did.
_APPLIED_KEY = "user_signal_applied"


def _entry_ids(payload: dict) -> list[str]:
    """The adaptive entries this turn's outcome was attributed to.

    Both halves of the same credit: hints scout actually used, and policies
    reflect cited as having shaped the turn.
    """
    ids: list[str] = []
    scout_summary = payload.get("scout_summary") or {}
    for source in (scout_summary.get("used_hints") or [], payload.get("cited_policies") or []):
        for entry_id in source:
            if isinstance(entry_id, str) and entry_id and entry_id not in ids:
                ids.append(entry_id)
    return ids


def _corrective_deltas(entry_id: str, verdict: str, signal: str) -> dict:
    """(successes, failures) to add for one entry when the user disagrees.

    Agreement is silent — the forward attribution already recorded it. Only a
    contradiction moves anything:

    * thumbs-up on a non-pass: the entry was blamed for a turn the user was
      happy with. Credit the success, and take back the failure if one was
      actually recorded against it.
    * thumbs-down on a pass: the entry was credited for a turn the user was
      not happy with. Record the failure, and take back the success.

    A take-back is skipped when the counter is already zero: `scout_signals`
    counters are cumulative evidence, and driving one negative would corrupt
    every ratio computed from it.
    """
    is_pass = verdict == "pass"
    if signal == "up" and not is_pass:
        wanted = {"successes": 1, "failures": -1}
    elif signal == "down" and is_pass:
        wanted = {"successes": -1, "failures": 1}
    else:
        return {}

    row = db.get_signal(_SIGNAL_TYPE, entry_id) or {}
    if wanted["failures"] < 0 and int(row.get("failures") or 0) <= 0:
        wanted["failures"] = 0
    if wanted["successes"] < 0 and int(row.get("successes") or 0) <= 0:
        wanted["successes"] = 0
    return {k: v for k, v in wanted.items() if v}


def _write_signal(entry_id: str, successes: int, failures: int, rationale: str) -> None:
    """Apply one correction. delta_reinforcements=0 — this is not a new use.

    The entry's usage was counted once already, at scout submit time. A thumb
    is a second reading of the SAME observation, not another observation, and
    inflating the denominator here would make every entry look less used than
    it is at exactly the moment the user told us something about it.
    """
    db.upsert_signal(
        _SIGNAL_TYPE,
        entry_id,
        delta_successes=successes,
        delta_failures=failures,
        payload_json=json.dumps({"last_rationale": rationale}),
        delta_reinforcements=0,
    )


def apply_user_signal(session_id: str, message_id, signal: str | None) -> dict:
    """Land a thumb on the turn `message_id` belongs to and correct its credit.

    `signal` is "up", "down", or None to withdraw. Returns a small report of
    what happened — the post-mortem it found, the entries it touched, and the
    per-entry deltas — for tests and for the endpoint's logs. Never raises.
    """
    report: dict = {"post_mortem_id": None, "applied": {}, "reversed": {}, "entries": []}
    try:
        pm = db.set_post_mortem_user_signal(session_id, message_id, signal)
    except Exception as e:
        logger.warning("Could not stamp the user signal for %s/%s: %s", session_id, message_id, e)
        return report
    if not pm:
        # An ungraded turn (or a message that belongs to none). The feedback
        # row itself is already stored; there is simply no verdict to argue
        # with, and that is not an error.
        return report

    report["post_mortem_id"] = pm["id"]
    try:
        payload = json.loads(pm.get("payload_json") or "{}")
        if not isinstance(payload, dict):
            payload = {}
    except (ValueError, TypeError):
        payload = {}

    prior = payload.get(_APPLIED_KEY) or {}
    prior_signal = prior.get("signal")
    if prior_signal == signal:
        # Same thumb written twice. The counters already say what this click
        # says; re-applying would double it.
        report["entries"] = list((prior.get("entries") or {}).keys())
        report["applied"] = dict(prior.get("entries") or {})
        return report

    verdict = str(pm.get("verdict") or "")
    try:
        # 1. Undo whatever the previous thumb applied, exactly as recorded.
        for entry_id, deltas in (prior.get("entries") or {}).items():
            successes = -int(deltas.get("successes") or 0)
            failures = -int(deltas.get("failures") or 0)
            if successes or failures:
                _write_signal(entry_id, successes, failures, f"user signal withdrawn (was {prior_signal})")
                report["reversed"][entry_id] = {"successes": successes, "failures": failures}

        # 2. Apply the new one, if it contradicts the verdict.
        applied: dict = {}
        if signal:
            for entry_id in _entry_ids(payload):
                deltas = _corrective_deltas(entry_id, verdict, signal)
                if not deltas:
                    continue
                _write_signal(
                    entry_id,
                    int(deltas.get("successes") or 0),
                    int(deltas.get("failures") or 0),
                    f"user thumbs-{signal} contradicted verdict={verdict}",
                )
                applied[entry_id] = deltas

        # 3. Remember it, so the next click can reverse exactly this much.
        if applied:
            payload[_APPLIED_KEY] = {"signal": signal, "entries": applied}
        else:
            payload.pop(_APPLIED_KEY, None)
        db.update_post_mortem_payload(pm["id"], payload)

        report["applied"] = applied
        report["entries"] = list(applied.keys())
    except Exception as e:
        logger.warning("User-signal correction failed for post-mortem %s: %s", pm.get("id"), e)
    return report
