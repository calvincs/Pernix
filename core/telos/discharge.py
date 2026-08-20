"""Pernix — TELOS alarm discharge pass (E3): evidence closes what evidence opened.

Binding alarms are cleared by their own live monitor and acedia alarms by
entropy control, but a divergence alarm had no owner: raised when a weekly
reconciliation measured the autobiography off the trace, it stayed live
forever even after later weeks came in clean. This pass re-checks such
alarms against current ledger state and closes them only on repeated,
time-separated clean measurements — the Hevel discharge shape: an alarm
leaves the books because its condition measurably stopped holding, not
because someone decided to stop looking. Acknowledgement is therefore not
required for discharge; it silences the notification, it is not the exit.

Only alarm types registered in `_CHECKERS` are touched. A type whose clear
path already lives in its own monitor must not be second-guessed here — two
writers with different bars on one alarm would make the ladder unauditable.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from config import settings
from core.telos.store import TelosObject, TelosStore

logger = logging.getLogger("pernix.telos")

# A forced re-run of the daily cron is the same check, not a second one —
# the same time-anchoring rule as the binding ladder's window advance.
_CHECK_SPACING_HOURS = 20


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hours_between(a: str, b: str) -> float:
    try:
        ta = datetime.fromisoformat(a)
        tb = datetime.fromisoformat(b)
    except (TypeError, ValueError):
        return 0.0
    return abs((tb - ta).total_seconds()) / 3600.0


def _check_divergence(store: TelosStore, alarm: TelosObject) -> tuple[str, bool] | None:
    """(evidence_key, clean) from the newest reconciliation, or None when
    there is nothing new to measure. Reconciliation is weekly, so each week
    is one piece of evidence — re-reading the same measurement tomorrow is
    not a second clean check, and the week that raised the alarm cannot
    also be the week that clears it.
    """
    series = list(store.get_state().get("coherence_series") or [])
    if not series:
        return None
    latest = series[-1]
    week = str(latest.get("week") or "")
    if not week:
        return None
    target_week = str(alarm.get("target") or "").removeprefix("AUTO-")
    if week == target_week or week in (alarm.get("checked_evidence") or []):
        return None
    try:
        divergence = float(latest.get("divergence"))
    except (TypeError, ValueError):
        return None
    return week, divergence <= settings.telos_divergence_max


_CHECKERS = {"divergence": _check_divergence}


def run_alarm_discharge(store: TelosStore) -> dict:
    result: dict = {"checked": 0, "discharged": 0}
    if not settings.telos_alarm_autoclose:
        return result
    n_required = max(1, settings.telos_alarm_autoclose_checks)

    for alarm in store.list_alarms(open_only=True):
        checker = _CHECKERS.get(str(alarm.get("type") or ""))
        if checker is None:
            continue
        verdict = checker(store, alarm)
        if verdict is None:
            continue
        evidence_key, clean = verdict
        result["checked"] += 1
        seen = list(alarm.get("checked_evidence") or []) + [evidence_key]

        checks = list(alarm.get("clean_checks") or [])
        if not clean:
            # The condition held again: the streak restarts. The dirty
            # evidence is still recorded so it is never counted twice either.
            store.update(alarm, clean_checks=[], checked_evidence=seen)
            continue

        now = _now_iso()
        if checks and _hours_between(checks[-1], now) < _CHECK_SPACING_HOURS:
            continue
        checks.append(now)
        span_h = _hours_between(checks[0], checks[-1])

        if len(checks) >= n_required and span_h >= settings.telos_alarm_autoclose_window_hours:
            reason = (
                f"closed-by-discharge: condition not holding in " f"{len(checks)} consecutive checks over {span_h:.0f}h"
            )
            was_acknowledged = alarm.get("state") == "acknowledged"
            store.update(alarm, state="cleared", cleared_reason=reason, clean_checks=checks, checked_evidence=seen)
            store.trace_append(
                "alarm_discharge",
                {
                    "id": alarm.id,
                    "class": str(alarm.get("type") or ""),
                    "target": alarm.get("target"),
                    "clean_checks": len(checks),
                    "span_hours": round(span_h, 1),
                    "first_clean_at": checks[0],
                    "last_clean_at": checks[-1],
                    "was_acknowledged": was_acknowledged,
                    "reason": reason,
                },
            )
            result["discharged"] += 1
            logger.info("telos: alarm %s discharged (%s)", alarm.id, reason)
        else:
            store.update(alarm, clean_checks=checks, checked_evidence=seen)

    if result["checked"]:
        logger.info("telos: alarm discharge pass: %s", result)
    return result
