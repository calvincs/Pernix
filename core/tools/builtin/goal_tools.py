"""Pernix — Persistent cross-turn goals (adaptation plan 3b).

A goal is an explicit contract: objective + optional budgets, closed only by
goal_complete (refused while goal-scoped gates fail). Goals come from
explicit user intent — the agent must never infer one from conversation.
"""

from __future__ import annotations

import logging

from config import settings

logger = logging.getLogger("pernix.tools.goals")


def goal_create(
    objective: str,
    token_budget: int = 0,
    time_budget_s: int = 0,
    continuation_budget: int = 0,
    _context: dict | None = None,
) -> str:
    """Create the session's goal. Only when the USER explicitly asked for one."""
    from db import models as db

    session_id = (_context or {}).get("session_id", "")
    if not session_id:
        return "Error: goal_create requires a session context."
    if not objective or len(objective.strip()) < 10:
        return "Error: objective must be a substantive description (>= 10 chars)."
    goal_id = db.create_goal(
        session_id,
        objective.strip(),
        token_budget=token_budget or None,
        time_budget_s=time_budget_s or None,
        continuation_budget=continuation_budget,
    )
    if goal_id is None:
        return "Error: this session already has a live goal. Complete or update it first (goal_status / goal_update / goal_complete)."
    # Stamp immediately so this turn's remaining spend bills to the goal.
    session = _session(_context)
    if session is not None:
        session.active_goal_id = goal_id
    parts = [f"Goal #{goal_id} created."]
    if token_budget:
        parts.append(f"Token budget: {token_budget:,}.")
    if time_budget_s:
        parts.append(f"Time budget: {time_budget_s}s.")
    parts.append(
        f"Auto-continuations: {continuation_budget} (0 = the goal waits for the user between turns)."
        " Only goal_complete finishes it."
    )
    return " ".join(parts)


def goal_status(_context: dict | None = None) -> str:
    from db import models as db

    session_id = (_context or {}).get("session_id", "")
    goal = db.get_active_goal(session_id) if session_id else None
    if not goal:
        return "No live goal in this session."
    lines = [
        f"Goal #{goal['id']} [{goal['status']}]: {goal['objective']}",
        f"Started: {goal.get('started_at', '?')}",
    ]
    used = db.goal_token_usage(goal["id"])
    if goal.get("token_budget"):
        lines.append(f"Tokens: {used:,}/{int(goal['token_budget']):,} (includes worker spend)")
    else:
        lines.append(f"Tokens: {used:,} (no budget set)")
    lines.append(f"Auto-continuations used: {goal.get('continuations_used', 0)}/{goal.get('continuation_budget', 0)}")
    gates = [g for g in db.get_gates(session_id) if g.get("scope") in ("goal", "session")]
    if gates:
        lines.append("Completion gates: " + ", ".join(g["name"] for g in gates))
    return "\n".join(lines)


def goal_update(
    objective: str = "",
    status: str = "",
    token_budget: int = -1,
    time_budget_s: int = -1,
    continuation_budget: int = -1,
    _context: dict | None = None,
) -> str:
    """Adjust the live goal (pause/resume, budgets, objective wording)."""
    from db import models as db

    session_id = (_context or {}).get("session_id", "")
    goal = db.get_active_goal(session_id) if session_id else None
    if not goal:
        return "Error: no live goal to update."
    fields: dict = {}
    if objective.strip():
        fields["objective"] = objective.strip()
    if status:
        if status not in ("active", "paused"):
            return "Error: goal_update only sets status active|paused. Use goal_complete to finish."
        fields["status"] = status
    if token_budget >= 0:
        fields["token_budget"] = token_budget or None
    if time_budget_s >= 0:
        fields["time_budget_s"] = time_budget_s or None
    if continuation_budget >= 0:
        fields["continuation_budget"] = continuation_budget
    if not fields:
        return "Error: nothing to update."
    db.update_goal(goal["id"], **fields)
    return f"Goal #{goal['id']} updated: {', '.join(fields)}."


def goal_complete(summary: str = "", _context: dict | None = None) -> str:
    """Finish the goal. Refused while goal-scoped gates fail — the gates ARE
    the completion criteria."""
    from db import models as db

    session_id = (_context or {}).get("session_id", "")
    goal = db.get_active_goal(session_id) if session_id else None
    if not goal:
        return "Error: no live goal to complete."

    if settings.gates_enabled:
        from core.gates import _run_one, failing
        from core.tools.paths import workspace

        rows = [g for g in db.get_gates(session_id) if g.get("scope") in ("goal", "session")]
        results = [_run_one(row, workspace(), "") for row in rows]
        bad = failing(results)
        if bad:
            details = "; ".join(f"{r.name} (exit {r.exit_code}): {r.output_tail[-200:] or r.error}" for r in bad)
            return (
                f"Error: goal_complete refused — {len(bad)} gate(s) failing: {details}. "
                f"Make them pass first; the gates are the completion criteria."
            )

    db.update_goal(goal["id"], status="complete")
    session = _session(_context)
    if session is not None:
        session.active_goal_id = None
    note = f" Summary: {summary.strip()}" if summary.strip() else ""
    return f"Goal #{goal['id']} completed.{note}"


def _session(_context: dict | None):
    try:
        from sessions.manager import get_manager

        return get_manager().get((_context or {}).get("session_id", ""))
    except Exception:
        return None


def register(reg) -> None:
    if not settings.goals_enabled:
        return
    common = {"category": "core", "source": "builtin", "parallel_safe": False, "timeout": 60}
    tags = ["goal", "objective", "budget", "long-running", "autonomous", "persistent", "task"]

    reg.register(
        name="goal_create",
        func=goal_create,
        description=(
            "Create this session's persistent goal — ONLY when the user explicitly asked for "
            "one; never infer a goal from conversation. The goal carries across turns until "
            "goal_complete. Budgets are optional; continuation_budget > 0 lets the harness "
            "auto-continue the goal after a turn ends (round ceiling, budget cut, or clean "
            "finish) up to that many times — default 0 waits for the user between turns."
        ),
        parameters={
            "type": "object",
            "properties": {
                "objective": {"type": "string", "description": "What done means, concretely (<= 4000 chars)"},
                "token_budget": {"type": "integer", "description": "Optional total token ceiling (includes workers)"},
                "time_budget_s": {"type": "integer", "description": "Optional wall-clock ceiling in seconds"},
                "continuation_budget": {
                    "type": "integer",
                    "description": "Auto-continuations allowed (default 0 = user-driven)",
                },
            },
            "required": ["objective"],
        },
        tags=tags + ["create"],
        safety_level="caution",
        **common,
    )
    reg.register(
        name="goal_status",
        func=goal_status,
        description="Show the live goal: objective, status, budget burn (worker spend included), gates.",
        parameters={"type": "object", "properties": {}},
        tags=tags + ["status", "check"],
        safety_level="safe",
        **common,
    )
    reg.register(
        name="goal_update",
        func=goal_update,
        description="Adjust the live goal: pause/resume (status active|paused), budgets, or objective wording.",
        parameters={
            "type": "object",
            "properties": {
                "objective": {"type": "string", "description": "New objective wording (optional)"},
                "status": {"type": "string", "description": "active | paused"},
                "token_budget": {"type": "integer", "description": "New token ceiling (0 clears; omit to keep)"},
                "time_budget_s": {"type": "integer", "description": "New time ceiling (0 clears; omit to keep)"},
                "continuation_budget": {"type": "integer", "description": "New continuation allowance"},
            },
        },
        tags=tags + ["update", "pause"],
        safety_level="caution",
        **common,
    )
    reg.register(
        name="goal_complete",
        func=goal_complete,
        description=(
            "Finish the live goal — the ONLY way a goal completes. Refused while any "
            "session/goal-scoped gate fails: the gates are the completion criteria."
        ),
        parameters={
            "type": "object",
            "properties": {"summary": {"type": "string", "description": "Optional one-line completion summary"}},
        },
        tags=tags + ["complete", "finish", "done"],
        safety_level="caution",
        **common,
    )
