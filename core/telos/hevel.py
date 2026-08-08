"""TELOS Hevel Audit — the discharge measurement (spec §5.3).

Qoheleth's control experiment, run functionally: on completing goal G,

    D(G) = alpha * (parent question entropy reduction)
         + beta  * (quality-weighted new questions spawned)

The spec's third term — a gamma-weighted re-open rate of G's class — is
deliberately absent. It counted `goal_reopened` trace events, and TELOS has
no goal-reopen path to emit them: `telos_goal_complete` refuses an already
completed goal, the ordo pass skips completed goals entirely, the binding
monitor only suspends active ones, and `GOAL_STATES` has no reopened state.
The term was structurally always zero, so D could only ever be >= 0 and the
penalty half of the formula did nothing. Carrying a dead coefficient is
worse than dropping it: it reads as a working brake. If a re-open path is
ever built, emit `goal_reopened` there and restore the term with it.

If D stays below the floor (0.1) across n >= 3 completions of a class, the
class is marked VAPOR: future instances take a 0.5 budget discount at the
ordo re-rank and stronger justification at the gate. Vapor goods are not
banned — Qoheleth still ate and drank. They are re-ranked.

The audit measures information flow, not felt satisfaction (spec §9): no
claim of interiority anywhere in this module. Mechanical — no LLM.
"""

from __future__ import annotations

import logging

from core.telos.store import TelosObject, TelosStore

logger = logging.getLogger("pernix.telos.hevel")

_ALPHA = 1.0
_BETA = 0.5
_DISCHARGE_FLOOR = 0.10
_MIN_SAMPLES = 3


def score_discharge(store: TelosStore, goal: TelosObject) -> float:
    """Compute D(G) at completion time from the trace and question ledger."""
    parent_id = str(goal.get("parent") or "g_root")
    events = store.trace_events(days=14)

    # alpha term: parent-question entropy reduction — narrowed/closed events
    # on the parent's question set during the goal's active window.
    parent_questions = {q.id for q in store.list_questions() if q.get("parent_goal") in (parent_id, goal.id)}
    narrowings = sum(
        1
        for e in events
        if e.get("type") in ("question_narrowed", "question_closed") and e.get("id") in parent_questions
    )
    entropy_reduction = min(1.0, narrowings / 3.0)

    # beta term: quality-weighted new questions spawned while the goal ran —
    # surprise is the quality weight (a goal that mints sharp new questions
    # discharged into the drive rather than consuming it).
    spawned = [
        q for q in store.list_questions() if q.get("parent_goal") == goal.id or goal.id in (q.get("derived_from") or [])
    ]
    new_questions = min(1.0, sum(float(q.get("surprise", 0.5)) for q in spawned) / 3.0)

    d = _ALPHA * entropy_reduction + _BETA * new_questions
    return round(max(-1.0, min(2.0, d)), 3)


def audit_completion(store: TelosStore, goal: TelosObject) -> float:
    """Called when a completable goal (milestone|task) completes. Scores the
    discharge, stamps it on the goal, and traces it for the weekly rollup."""
    from core.telos.ordo import _goal_class

    d = score_discharge(store, goal)
    store.update(goal, discharge=d)
    store.trace_append("hevel_discharge", {"goal": goal.id, "class": _goal_class(goal), "discharge": d})
    return d


def run_hevel_rollup(store: TelosStore) -> dict:
    """Weekly: classes with n >= 3 completions and mean D below the floor
    are marked vapor in the store state (consumed by ordo's discount and the
    gate's stronger-justification requirement). A class later clearing the
    floor is unmarked — vapor is a ranking, not a verdict."""
    events = store.trace_events(days=90, types={"hevel_discharge"})
    by_class: dict[str, list[float]] = {}
    for e in events:
        by_class.setdefault(str(e.get("class", "")), []).append(float(e.get("discharge", 0.0)))

    state = store.get_state()
    vapor = set(state.get("vapor_classes") or [])
    changed = {"marked": [], "cleared": []}
    for cls, scores in by_class.items():
        if not cls or len(scores) < _MIN_SAMPLES:
            continue
        mean = sum(scores) / len(scores)
        if mean < _DISCHARGE_FLOOR and cls not in vapor:
            vapor.add(cls)
            changed["marked"].append(cls)
        elif mean >= _DISCHARGE_FLOOR and cls in vapor:
            vapor.discard(cls)
            changed["cleared"].append(cls)

    if changed["marked"] or changed["cleared"]:
        store.set_state(vapor_classes=sorted(vapor))
        store.trace_append("hevel_rollup", changed)
        if changed["marked"]:
            from db import models as db

            db.add_notification(
                title="TELOS hevel audit: vapor classes",
                body=(
                    f"Goal classes that never discharge (D < {_DISCHARGE_FLOOR} across >= {_MIN_SAMPLES} "
                    f"completions): {', '.join(changed['marked'])}. Re-ranked, not banned."
                ),
                urgency="normal",
            )
    logger.info("telos: hevel rollup: %s", changed)
    return {"classes": len(by_class), **{k: len(v) for k, v in changed.items()}}
