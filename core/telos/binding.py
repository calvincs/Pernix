"""TELOS Binding Monitor — the Goodhart/idolatry detector (spec §5.2).

Alarm signature, all four conditions over a 7-day window:
  - subgoal budget share > telos_budget_share_max (default 0.35), and
  - proxy metric slope positive (the subgoal's own activity is climbing), and
  - parent question entropy slope >= 0 (its questions are NOT narrowing), and
  - new-claims rate below floor.

The metric consumed, the drive undischarged, the budget still flowing.
Escalation ladder: L1 log + immediate ordo re-rank -> L2 (persists two
windows) freeze the subgoal pending re-justification -> L3 operator
escalation. The 0.35 threshold will false-positive during legitimate deep
pushes; L1 is deliberately just "log + ordo" so a justified push survives
one re-ranking with its budget intact (spec §8).

Mechanical — no LLM.
"""

from __future__ import annotations

import logging

from config import settings
from core.telos.store import TelosObject, TelosStore

logger = logging.getLogger("pernix.telos.binding")


def _window_signals(store: TelosStore, goal_id: str) -> dict:
    """The four signature signals for one goal over the current and prior
    half-windows (slopes = second half vs first half of the 7 days)."""
    events = store.trace_events(days=7)
    half = _split_epoch(events)

    spend = [e for e in events if e.get("type") == "spend"]
    total = sum(int(e.get("tokens", 0)) for e in spend) or 1
    goal_tokens = sum(int(e.get("tokens", 0)) for e in spend if e.get("goal") == goal_id)
    share = goal_tokens / total

    goal_questions = {q.id for q in store.list_questions() if q.get("parent_goal") == goal_id}

    def in_half(e, late: bool) -> bool:
        return (int(e.get("epoch_ms", 0)) >= half) == late

    # Proxy slope: the subgoal's own executed-hypothesis activity.
    proxy_early = sum(
        1 for e in events if e.get("type") == "hypothesis" and e.get("question") in goal_questions and in_half(e, False)
    )
    proxy_late = sum(
        1 for e in events if e.get("type") == "hypothesis" and e.get("question") in goal_questions and in_half(e, True)
    )

    # Parent-question entropy: narrowing/closing events reduce it. Zero
    # narrowings in the window = entropy slope >= 0 (the question isn't moving).
    narrowings = sum(
        1 for e in events if e.get("type") in ("question_narrowed", "question_closed") and e.get("id") in goal_questions
    )

    claims = sum(
        1
        for e in events
        if e.get("type") == "claim_commit" and any(str(d) in goal_questions for d in (e.get("derived_from") or []))
    )
    return {
        "budget_share": round(share, 3),
        "proxy_slope_positive": proxy_late > proxy_early,
        "entropy_reduced": narrowings > 0,
        "claims_in_window": claims,
        "goal_tokens": goal_tokens,
    }


def _split_epoch(events: list[dict]) -> int:
    if not events:
        return 0
    epochs = [int(e.get("epoch_ms", 0)) for e in events]
    return (min(epochs) + max(epochs)) // 2


def run_binding_monitor(store: TelosStore) -> dict:
    result = {"checked": 0, "alarms": []}
    goals = [g for g in store.list_goals() if g.id != "g_root" and g.get("state") == "active"]
    for g in goals:
        result["checked"] += 1
        sig = _window_signals(store, g.id)
        bound = (
            sig["budget_share"] > settings.telos_budget_share_max
            and sig["proxy_slope_positive"]
            and not sig["entropy_reduced"]
            and sig["claims_in_window"] < settings.telos_claims_floor_per_window
        )
        prior = _open_binding_alarm(store, g.id)
        if not bound:
            if prior is not None:
                store.update(prior, state="cleared", cleared_reason="signature no longer holds")
                store.trace_append("alarm_cleared", {"id": prior.id, "target": g.id})
            continue

        if prior is None:
            level = 1
            alarm = TelosObject(
                id=store.mint_id("alarm"),
                kind="alarm",
                meta={
                    "type": "binding",
                    "target": g.id,
                    "level": level,
                    "state": "open",
                    "evidence": sig,
                    "windows": 1,
                },
            )
            store.write(alarm)
        else:
            windows = int(prior.get("windows", 1)) + 1
            level = 3 if windows > 2 else 2
            alarm = store.update(prior, level=level, windows=windows, evidence=sig)

        store.trace_append(
            "alarm", {"id": alarm.id, "type": "binding", "target": g.id, "level": level, "evidence": sig}
        )
        result["alarms"].append({"target": g.id, "level": level})

        if level == 1:
            # L1: log + immediate ordo pass — re-ranking, not punishment.
            from core.telos.ordo import run_ordo_pass

            run_ordo_pass(store)
        elif level == 2:
            # L2: freeze pending re-justification against its parent.
            store.update(
                g, state="suspended", suspended_reason=f"binding alarm {alarm.id}: frozen pending re-justification"
            )
        if level >= 2:
            from db import models as db

            db.add_notification(
                title=f"TELOS binding alarm L{level}: {g.id}",
                body=(
                    f"Budget share {sig['budget_share']:.0%} with no parent-question movement and "
                    f"{sig['claims_in_window']} new claims this window. "
                    + ("Subgoal frozen pending re-justification." if level == 2 else "Operator escalation.")
                ),
                urgency="high" if level >= 3 else "normal",
            )
    logger.info("telos: binding monitor: %d goals, %d alarms", result["checked"], len(result["alarms"]))
    return result


def _open_binding_alarm(store: TelosStore, goal_id: str):
    for a in store.list_alarms(open_only=True):
        if a.get("type") == "binding" and a.get("target") == goal_id:
            return a
    return None
