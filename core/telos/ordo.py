"""TELOS Ordo Pass — *ordo amoris* as a scheduled job (spec §5.1).

The correction is a RE-RANKING, not a purge: finite goods stay in the
hierarchy, re-subordinated to the question they serve. Daily:

1. Walk the goal DAG from g_root; verify every parent chain. Orphans are
   the operational definition of "losing our way": suspended and listed for
   operator review — never silently deleted.
2. Re-rank siblings by parent_advancement x discharge_history x claim
   support, applying vapor discounts (§5.3).
3. Emit the full diff to the trace.

Also here: the monthly Dream Register review (§4.2) — dreams must fail the
capability test; one that looks reachable was a milestone promoted by
enthusiasm and gets flagged for operator demotion (flagged, not demoted:
reclassification is operator work).

Mechanical throughout — no LLM calls.
"""

from __future__ import annotations

import logging

from core.telos.store import TelosObject, TelosStore

logger = logging.getLogger("pernix.telos.ordo")


def _chain_to_root(goal: TelosObject, by_id: dict) -> bool:
    """True iff the parent chain reaches g_root without cycles."""
    seen = set()
    cur = goal
    while cur is not None:
        if cur.id == "g_root":
            return True
        if cur.id in seen:
            return False
        seen.add(cur.id)
        cur = by_id.get(str(cur.get("parent") or ""))
    return False


def _rank_score(store: TelosStore, goal: TelosObject, vapor_classes: set[str]) -> float:
    """parent_advancement x discharge_history x claim support, vapor-discounted."""
    events = store.trace_events(days=28, types={"hypothesis_resolved", "question_minted", "hevel_discharge"})
    goal_questions = {q.id for q in store.list_questions() if q.get("parent_goal") == goal.id}

    resolved = sum(1 for e in events if e.get("type") == "hypothesis_resolved" and e.get("question") in goal_questions)
    advancement = min(1.0, resolved / 4.0) if goal_questions else 0.1

    discharges = [
        float(e.get("discharge", 0.0))
        for e in events
        if e.get("type") == "hevel_discharge" and e.get("class") == _goal_class(goal)
    ]
    discharge_history = max(0.1, sum(discharges) / len(discharges)) if discharges else 0.5

    supported = sum(
        1
        for c in store.list("claim")
        if any(str(d).startswith("q_") and d in goal_questions for d in (c.get("derived_from") or []))
    )
    claim_support = min(1.0, 0.3 + supported / 5.0)

    score = advancement * discharge_history * claim_support
    if _goal_class(goal) in vapor_classes:
        score *= 0.5  # vapor discount — discounted, never banned (§5.3)
    return round(score, 4)


def _goal_class(goal: TelosObject) -> str:
    """Discharge classes group completions for the Hevel Audit: kind plus
    the first tag, so 'milestone:deploy' completions pool together."""
    tags = goal.get("tags") or []
    first = str(tags[0]) if isinstance(tags, list) and tags else ""
    return f"{goal.get('kind', 'task')}:{first}" if first else str(goal.get("kind", "task"))


def run_ordo_pass(store: TelosStore) -> dict:
    goals = store.list_goals()
    by_id = {g.id: g for g in goals}
    state = store.get_state()
    vapor_classes = set(state.get("vapor_classes") or [])

    diff: dict = {"orphaned": [], "reranked": [], "unsuspended": []}

    for g in goals:
        if g.id == "g_root" or g.get("state") in ("completed",):
            continue
        rooted = _chain_to_root(g, by_id)
        if not rooted and g.get("state") == "active":
            store.update(g, state="suspended", suspended_reason="orphan: no chain to g_root (ordo)")
            diff["orphaned"].append(g.id)
        elif rooted and g.get("state") == "suspended" and str(g.get("suspended_reason", "")).startswith("orphan"):
            store.update(g, state="active", suspended_reason=None)
            diff["unsuspended"].append(g.id)

    # Re-rank active siblings under each parent.
    for parent_id in {str(g.get("parent")) for g in goals if g.get("parent")}:
        siblings = [g for g in goals if str(g.get("parent")) == parent_id and g.get("state") == "active"]
        if len(siblings) < 1:
            continue
        scored = sorted(((g, _rank_score(store, g, vapor_classes)) for g in siblings), key=lambda t: t[1], reverse=True)
        for rank, (g, score) in enumerate(scored, start=1):
            if g.get("ordo_rank") != rank or abs(float(g.get("ordo_score", -1)) - score) > 1e-6:
                store.update(g, ordo_rank=rank, ordo_score=score)
                diff["reranked"].append({"id": g.id, "rank": rank, "score": score})

    if diff["orphaned"]:
        from db import models as db

        db.add_notification(
            title="TELOS ordo: orphaned goals suspended",
            body=f"Goals without a chain to g_root, awaiting review or re-attachment: {', '.join(diff['orphaned'])}",
            urgency="normal",
        )
    store.trace_append("ordo_pass", diff)
    counts = {k: len(v) for k, v in diff.items()}
    logger.info("telos: ordo pass done: %s", counts)
    return counts


def review_dream_register(store: TelosStore) -> dict:
    """Monthly (§4.2): every dream must have capability_gap=true and be
    non-completable. Violations are flagged for the operator, not demoted."""
    flagged = []
    for g in store.list_goals(kind="dream"):
        problems = []
        if not g.get("capability_gap", False):
            problems.append("capability_gap is not true — a reachable dream is a promoted milestone; demote it")
        if g.get("completable", False):
            problems.append("dreams are not completable; the horizon must recede")
        milestones = [m for m in store.list_goals(kind="milestone") if str(m.get("parent")) == g.id]
        done = [m for m in milestones if m.get("state") == "completed"]
        if milestones and len(done) == len(milestones):
            problems.append("all milestones complete — mint a successor beyond the new capability frontier")
        if problems:
            store.update(g, register_flags=problems)
            flagged.append({"id": g.id, "problems": problems})
    if flagged:
        from db import models as db

        db.add_notification(
            title="TELOS dream register review",
            body="; ".join(f"{f['id']}: {f['problems'][0]}" for f in flagged[:3]),
            urgency="normal",
        )
    store.trace_append("dream_register_review", {"flagged": [f["id"] for f in flagged]})
    return {"flagged": len(flagged)}
