"""Pernix — Candor extension: calibrated operational memory (add-on, off by default).

Wires the Candor memory substrate (github: CANDOR — append-only ledger,
earned probabilities, per-source trust) into Pernix:

- turn end   → sessions/hooks._maybe_candor emits tool/turn/verdict outcomes
- snooze     → SnoozeRunner._candor_maintenance runs the admission gate,
               drains the pending buffer, and checkpoints
- scout      → _gather_candor_intel injects the [OPERATIONAL INTEL] brief
- agent      → the tools below answer reliability questions on demand

Master switch: settings.candor_enabled. The hook/snooze/scout paths gate hot
at each call site; the tools below register only when enabled at startup
(the web-extension pattern — toggling tool registration needs a restart).

Requires the `candor` package (zero-dep):  pip install -e /path/to/Candor
Absent package or a broken store degrade to inert no-ops, never to errors.
"""

from __future__ import annotations

import json
import logging

from config import settings
from core.tools.registry import ToolRegistry

logger = logging.getLogger("pernix.ext.candor")


def predict_reliability(pred: str = "tool_ok", target: str = "*", _context: dict | None = None) -> str:
    """Calibrated reliability estimate for a tracked statement."""
    if not settings.candor_enabled:
        return "Candor is disabled (settings.candor_enabled)."
    from core.extensions.candor.bridge import get_candor_bridge

    try:
        result = get_candor_bridge().predict_sync(pred, [target])
    except Exception as e:
        return f"Candor unavailable: {e}"
    if result is None:
        return (
            f"No admitted fact {pred}({target}) — Candor has no evidence for it yet. "
            "Facts are admitted after observations accumulate and the snooze gate runs."
        )
    return json.dumps(result, indent=1)


def why_reliability(pred: str, target: str, _context: dict | None = None) -> str:
    """Full audit chain for a belief: who reported what, and how it was derived."""
    if not settings.candor_enabled:
        return "Candor is disabled (settings.candor_enabled)."
    from core.extensions.candor.bridge import get_candor_bridge

    try:
        result = get_candor_bridge().why_sync(pred, [target])
    except Exception as e:
        return f"Candor unavailable: {e}"
    if result is None:
        return f"No admitted fact {pred}({target})."
    text = json.dumps(result, indent=1, default=str)
    return text[:8000]


def reliability_questions(_context: dict | None = None) -> str:
    """Open anomalies the store wants measured (each with a suggested measurement)."""
    if not settings.candor_enabled:
        return "Candor is disabled (settings.candor_enabled)."
    from core.extensions.candor.bridge import get_candor_bridge

    try:
        questions = get_candor_bridge().questions_sync()
    except Exception as e:
        return f"Candor unavailable: {e}"
    if not questions:
        return "No open questions — no unexplained instability in tracked facts."
    lines = []
    for q in questions[:10]:
        lines.append(f"- [{q.get('kind', '?')}] target={q.get('target_id', '?')}: {q.get('suggested_measurement', '')}")
    return "\n".join(lines)


def register(reg: ToolRegistry) -> None:
    """Register agent-facing tools. Hard off-switch: nothing when disabled."""
    if not settings.candor_enabled:
        logger.debug("Candor extension inactive (candor_enabled=false)")
        return

    reg.register(
        name="predict_reliability",
        func=predict_reliability,
        description=(
            "Get a calibrated probability that something works, from Candor operational memory. "
            "Tracked predicates: tool_ok (target=tool name or '*'), turn_ok ('*'), "
            "tool_failure_mode (target=tool name; returns a failure-mode distribution), "
            "reflect_verdict ('*'), user_fact (target=user-model area, e.g. 'profile' or "
            "'professional_background', from the user.* memory file names; p = share of attested "
            "facts in that area that have stood unrevised — the stability of that part of the user "
            "model, NOT the truth of any single fact). Returns p, credible interval, observation "
            "count, and caveats (e.g. 'unstable', 'under_specified' = a missing variable is suspected)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "pred": {
                    "type": "string",
                    "description": "Predicate: tool_ok | tool_failure_mode | turn_ok | reflect_verdict",
                },
                "target": {"type": "string", "description": "Tool name, or '*' for the aggregate fact"},
            },
            "required": ["pred", "target"],
        },
        category="memory",
        tags=["candor", "reliability", "memory", "prediction"],
        source="extension",
        safety_level="safe",
        parallel_safe=True,
        timeout=60,
    )

    reg.register(
        name="why_reliability",
        func=why_reliability,
        description=(
            "Audit why Candor believes a reliability fact: per-source counts, gate decision, "
            "and derivation. Use when a predict_reliability answer needs justification."
        ),
        parameters={
            "type": "object",
            "properties": {
                "pred": {"type": "string", "description": "Predicate (e.g. tool_ok)"},
                "target": {"type": "string", "description": "Tool name or '*'"},
            },
            "required": ["pred", "target"],
        },
        category="memory",
        tags=["candor", "reliability", "audit"],
        source="extension",
        safety_level="safe",
        parallel_safe=True,
        timeout=90,
    )

    reg.register(
        name="reliability_questions",
        func=reliability_questions,
        description=(
            "List Candor's open questions — facts whose instability is unexplained, each with a "
            "concrete suggested measurement that would resolve it."
        ),
        parameters={"type": "object", "properties": {}},
        category="memory",
        tags=["candor", "reliability", "curiosity"],
        source="extension",
        safety_level="safe",
        parallel_safe=True,
        timeout=60,
    )
