"""Pernix — TELOS extension: agent-facing tools for the teleological layer.

The core engine lives in core/telos (fast loop via snooze Activity 16, slow
loops via the daily cron). These tools give the agent read access to its own
drive state and bounded write access to questions and goals:

- telos_status     — layer overview: questions, hypotheses, goals, alarms
- telos_ask        — mint a Question (first-class, with provenance)
- telos_goal_add   — add a dream/milestone/task under the goal DAG
- telos_goal_complete — complete a completable goal (runs the Hevel audit)

Deliberately absent: any trace-ledger write path (authority ordering, spec
§5.4 — the agent's self-model cannot outvote its record), any root
re-expression path (operator co-sign only), and any alarm-clearing path
(alarms clear when their signature stops holding, not when asked to).

Master switch: settings.telos_enabled. Tools register only when enabled at
startup (the Candor pattern — toggling registration needs a restart).
"""

from __future__ import annotations

import logging

from config import settings
from core.tools.registry import ToolRegistry

logger = logging.getLogger("pernix.ext.telos")


def telos_status(_context: dict | None = None) -> str:
    """Layer overview: question/hypothesis/goal/alarm counts and health."""
    if not settings.telos_enabled:
        return "TELOS is disabled (settings.telos_enabled)."
    from core.telos.store import TelosStore

    store = TelosStore.open()
    store.ensure_root()
    questions = store.list_questions()
    hyps = store.list_hypotheses()
    goals = store.list_goals()
    alarms = store.list_alarms(open_only=True)
    state = store.get_state()
    mix = store.band_mix()

    def count(items, key, value):
        return sum(1 for i in items if i.get(key) == value)

    lines = [
        f"Questions: {count(questions, 'state', 'open')} open, "
        f"{count(questions, 'state', 'narrowed')} narrowed, "
        f"{count(questions, 'state', 'abandoned')} abandoned "
        f"({sum(1 for q in questions if q.get('origin') == 'serendipity')} serendipity)",
        f"Hypotheses: {count(hyps, 'status', 'gated')} gated, {count(hyps, 'status', 'soup')} in the "
        f"speculation pool, {count(hyps, 'status', 'supported')} supported, "
        f"{count(hyps, 'status', 'refuted')} refuted",
        f"Goals: {len(goals)} total — "
        f"{count(goals, 'kind', 'dream')} dreams, {count(goals, 'kind', 'milestone')} milestones, "
        f"{count(goals, 'kind', 'task')} tasks; {count(goals, 'state', 'suspended')} suspended",
        f"Claims: {len(store.list('claim'))} committed",
        f"Band mix: near {mix['near']:.2f} / mid {mix['mid']:.2f} / far {mix['far']:.2f}; "
        f"serendipity budget {store.serendipity_budget():.2f}",
        f"Vapor classes: {', '.join(state.get('vapor_classes') or []) or 'none'}",
    ]
    if alarms:
        lines.append(
            "OPEN ALARMS: " + "; ".join(f"[{a.get('type')}] L{a.get('level')} on {a.get('target')}" for a in alarms[:5])
        )
    root = store.read("goal", "g_root")
    if root:
        lines.append(f"Root question: {root.get('text')}")
    return "\n".join(lines)


def telos_ask(
    question: str = "",
    surprise: float = 0.6,
    parent_goal: str = "g_root",
    _context: dict | None = None,
) -> str:
    """Mint a first-class Question into the fast loop."""
    if not settings.telos_enabled:
        return "TELOS is disabled (settings.telos_enabled)."
    question = (question or "").strip()
    if len(question) < 15:
        return "Question too short — a TELOS question needs enough substance to hypothesize against (>=15 chars)."
    from core.telos.store import TelosStore

    store = TelosStore.open()
    store.ensure_root()
    if store.question_is_duplicate(question):
        return "A near-duplicate of this question already exists — not minting another."
    if parent_goal != "g_root" and store.read("goal", parent_goal) is None:
        return f"Unknown parent goal '{parent_goal}' — use g_root or an existing goal id."
    q = store.add_question(question, surprise=surprise, parent_goal=parent_goal, origin="operator")
    return f"Minted {q.id} (parent {parent_goal}). The SOUP will pick it up at the next idle cycle."


def telos_goal_add(
    kind: str = "task",
    title: str = "",
    justification: str = "",
    parent: str = "g_root",
    _context: dict | None = None,
) -> str:
    """Add a goal to the DAG. Dreams must fail the capability test."""
    if not settings.telos_enabled:
        return "TELOS is disabled (settings.telos_enabled)."
    from core.telos.store import TelosObject, TelosStore

    kind = (kind or "").strip().lower()
    if kind not in ("dream", "milestone", "task"):
        return "kind must be dream | milestone | task (the root is operator-configured, never added here)."
    title = (title or "").strip()
    justification = (justification or "").strip()
    if len(title) < 5:
        return "Goal title too short."
    if len(justification) < 15:
        return (
            "Every goal needs a justification linking it to its parent question "
            "(>=15 chars) — an unjustified goal is the Ordo Pass's first orphan."
        )
    store = TelosStore.open()
    store.ensure_root()
    if store.read("goal", parent) is None:
        return f"Unknown parent '{parent}'."
    gid = store.mint_id("goal", hint=title)
    if store.read("goal", gid) is not None:
        return f"A goal with id {gid} already exists."
    meta = {
        "kind": kind,
        "title": title,
        "text": title,
        "parent": parent,
        "justification": justification,
        "state": "active",
        "completable": kind in ("milestone", "task"),
        "tags": [],
    }
    if kind == "dream":
        # Capability test (§4.2): a dream the current toolchain can reach is
        # a milestone promoted by enthusiasm. The flag is an assertion the
        # monthly register review re-checks.
        meta["capability_gap"] = True
        meta["completable"] = False
    obj = TelosObject(id=gid, kind="goal", meta=meta)
    store.write(obj)
    store.trace_append("goal_added", {"id": gid, "kind": kind, "parent": parent})
    return f"Added {kind} {gid} under {parent}."


def telos_goal_complete(goal_id: str = "", _context: dict | None = None) -> str:
    """Complete a completable goal and run the Hevel discharge audit on it."""
    if not settings.telos_enabled:
        return "TELOS is disabled (settings.telos_enabled)."
    from core.telos.hevel import audit_completion
    from core.telos.store import TelosStore

    store = TelosStore.open()
    g = store.read("goal", (goal_id or "").strip())
    if g is None:
        return f"Unknown goal '{goal_id}'."
    if not g.get("completable", False):
        return f"{g.id} is a {g.get('kind')} — not completable. Dreams recede; the root never closes."
    if g.get("state") == "completed":
        return f"{g.id} is already completed (discharge {g.get('discharge')})."
    store.update(g, state="completed")
    store.trace_append("goal_completed", {"id": g.id, "kind": g.get("kind")})
    d = audit_completion(store, g)
    return (
        f"Completed {g.id}. Hevel discharge D = {d} "
        f"({'discharged into new questions' if d >= 0.1 else 'low discharge — if this class keeps scoring ~0, it will be marked vapor'})."
    )


def register(reg: ToolRegistry) -> None:
    """Register agent-facing tools. Hard off-switch: nothing when disabled."""
    if not settings.telos_enabled:
        logger.debug("TELOS extension inactive (telos_enabled=false)")
        return

    reg.register(
        name="telos_status",
        func=telos_status,
        description=(
            "Overview of the TELOS teleological layer: open questions, hypothesis pipeline "
            "(gated/speculation-pool/resolved), goal DAG health, committed claims, open alarms "
            "(binding/hevel/divergence/acedia), and the current exploration temperature."
        ),
        parameters={"type": "object", "properties": {}},
    )
    reg.register(
        name="telos_ask",
        func=telos_ask,
        description=(
            "Mint a first-class TELOS Question into the fast loop. Use for genuine open questions "
            "about the system's behavior or knowledge worth hypothesizing on during idle time — "
            "not for tasks. The SOUP generates falsifiable hypotheses for it at idle."
        ),
        parameters={
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "The question text (>=15 chars)"},
                "surprise": {"type": "number", "description": "0-1: how strongly a prior was violated (default 0.6)"},
                "parent_goal": {"type": "string", "description": "Goal id this serves (default g_root)"},
            },
            "required": ["question"],
        },
    )
    reg.register(
        name="telos_goal_add",
        func=telos_goal_add,
        description=(
            "Add a goal to the TELOS DAG: dream (far-horizon, must exceed current capability, never "
            "completable), milestone, or task. Every goal needs a justification linking it to its "
            "parent — orphans get suspended by the daily Ordo Pass."
        ),
        parameters={
            "type": "object",
            "properties": {
                "kind": {"type": "string", "description": "dream | milestone | task"},
                "title": {"type": "string", "description": "Short goal title"},
                "justification": {"type": "string", "description": "How this advances the parent (>=15 chars)"},
                "parent": {"type": "string", "description": "Parent goal id (default g_root)"},
            },
            "required": ["kind", "title", "justification"],
        },
    )
    reg.register(
        name="telos_goal_complete",
        func=telos_goal_complete,
        description=(
            "Complete a completable TELOS goal (milestone or task). Runs the Hevel discharge audit: "
            "did completing it reduce its parent question's entropy and spawn new questions, or did "
            "it discharge nothing (vapor)?"
        ),
        parameters={
            "type": "object",
            "properties": {"goal_id": {"type": "string", "description": "The goal id (g_...)"}},
            "required": ["goal_id"],
        },
    )
    logger.info("TELOS extension registered (4 tools)")
