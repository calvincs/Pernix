"""Pernix — Canary tools: read-only suite status for the agent.

Registered only when canary_enabled. The agent can read the suite and its
recent results; it cannot start a run. The `canary_run` tool was retired in
3.2: canaries run after a deploy or a model swap, or when the user presses
Run in the Canary tab (POST /api/canary/run).
"""

from __future__ import annotations

import json
import logging

from config import settings

logger = logging.getLogger("pernix.tools.canary")


def canary_status(task: str = "", limit: int = 10, _context: dict | None = None) -> str:
    """Suite overview + recent run results."""
    from core.canary import scan_canaries
    from db import models as db

    defs = scan_canaries()
    lines = [f"Canary suite: {len(defs)} task(s) in {settings.canaries_dir}"]
    for d in defs:
        flags = " [flaky]" if d.flaky else ""
        lines.append(f"  - {d.name}{flags}: {len(d.gates)} gate(s), tags={','.join(d.tags) or '-'}")

    runs = db.list_canary_runs(task=task or None, limit=max(1, min(int(limit), 50)))
    if not runs:
        lines.append("No recorded runs yet.")
        return "\n".join(lines)

    lines.append(f"\nRecent runs (newest first{f', task={task}' if task else ''}):")
    for r in runs:
        gates = []
        try:
            gates = json.loads(r.get("gate_results_json") or "[]")
        except (TypeError, ValueError):
            pass
        failed = [g["name"] for g in gates if not g.get("passed")]
        verdict = "PASS" if r.get("passed") else f"FAIL({','.join(failed) or 'no-gates'})"
        lines.append(
            f"  {str(r.get('created_at', ''))[:16]} {r['task']}: {verdict} "
            f"trigger={r.get('trigger')} retries={r.get('retries', 0)} "
            f"tokens={r.get('tokens', 0)} {float(r.get('duration_s') or 0):.0f}s"
        )
    return "\n".join(lines)


def register(reg) -> None:
    if not settings.canary_enabled:
        return
    reg.register(
        name="canary_status",
        func=canary_status,
        description=(
            "List the canary suite and recent run results (pass/fail per gate, "
            "retries, tokens, duration). Read-only."
        ),
        parameters={
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Filter runs to one canary name"},
                "limit": {"type": "integer", "description": "Max runs to show (default 10)"},
            },
        },
        category="evaluation",
        tags=["canary", "status", "results", "regression", "history"],
        timeout=15,
        parallel_safe=True,
        safety_level="safe",
        denied_session_types={"canary"},
    )
