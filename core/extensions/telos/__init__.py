"""Pernix — TELOS extension: agent-facing tools for the question loop.

The core engine lives in core/telos (fast loop via snooze Activity 16, slow
loops via the daily cron). These tools give the agent read access to its own
drive state and a bounded write path for questions:

- telos_status — layer overview: questions, hypotheses, claims, alarms
- telos_ask    — mint a Question (first-class, with provenance)

The goal tools (telos_goal_add / telos_goal_complete) left with the v3.1
goal-DAG carve — the tree only ever held the root, and the machinery that
consumed goals (ordo/binding/hevel) is gone.

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
    """Layer overview: question/hypothesis/claim/alarm counts and health."""
    if not settings.telos_enabled:
        return "TELOS is disabled (settings.telos_enabled)."
    from core.telos.calibration import describe, eig_calibration
    from core.telos.store import TelosStore

    store = TelosStore.open()
    store.ensure_root()
    questions = store.list_questions()
    hyps = store.list_hypotheses()
    alarms = store.list_alarms(open_only=True)
    mix = store.band_mix()

    def count(items, key, value):
        return sum(1 for i in items if i.get(key) == value)

    lines = [
        f"Questions: {count(questions, 'state', 'open')} open, "
        f"{count(questions, 'state', 'narrowed')} narrowed, "
        f"{count(questions, 'state', 'abandoned')} abandoned "
        f"({sum(1 for q in questions if q.get('origin') == 'serendipity')} serendipity)",
        # The archived count is a file tally, not a scan: terminal hypotheses
        # live in soup/archive/ and are excluded from every list above by the
        # store's one-level glob. Reported anyway so the pool count reads as a
        # live queue rather than as everything the layer ever produced.
        f"Hypotheses: {count(hyps, 'status', 'gated')} gated, {count(hyps, 'status', 'soup')} in the "
        f"speculation pool, {count(hyps, 'status', 'supported')} supported, "
        f"{count(hyps, 'status', 'refuted')} refuted, "
        f"{store.count_archived('hypothesis')} archived (untestable/expired, soup/archive/)",
        f"Claims: {len(store.list('claim'))} committed",
        f"Band mix: near {mix['near']:.2f} / mid {mix['mid']:.2f} / far {mix['far']:.2f}; "
        f"serendipity budget {store.serendipity_budget():.2f}",
        describe(eig_calibration(store)),
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
    q = store.add_question(question, surprise=surprise, parent_goal="g_root", origin="operator")
    return f"Minted {q.id}. The SOUP will pick it up at the next idle cycle."


def register(reg: ToolRegistry) -> None:
    """Register agent-facing tools. Hard off-switch: nothing when disabled."""
    if not settings.telos_enabled:
        logger.debug("TELOS extension inactive (telos_enabled=false)")
        return

    reg.register(
        name="telos_status",
        func=telos_status,
        description=(
            "Overview of the TELOS question loop: open questions, hypothesis pipeline "
            "(gated/speculation-pool/resolved), committed claims, open alarms, and the "
            "current exploration temperature."
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
            },
            "required": ["question"],
        },
    )
    logger.info("TELOS extension registered (2 tools)")
