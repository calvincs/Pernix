"""Pernix — Canary tools (adaptation plan 3.5): manual trigger + status.

Registered only when canary_enabled. canary_run enqueues via the scheduler
and returns immediately — a canary is a full multi-minute pipeline run and
must never block the calling turn. Denied inside canary sessions (a canary
spawning canaries is a fork bomb) and workers.
"""

from __future__ import annotations

import json
import logging

from config import settings

logger = logging.getLogger("pernix.tools.canary")


def canary_run(name: str, _context: dict | None = None) -> str:
    """Queue one canary for immediate background execution."""
    from core.canary import load_canary
    from core.extensions.scheduling import enqueue_manual_canary

    name = (name or "").strip()
    if not name:
        return "Error: name is required."
    if load_canary(name) is None:
        from core.canary import scan_canaries

        known = ", ".join(sorted(c.name for c in scan_canaries())) or "(none)"
        return f"Error: no canary named '{name}'. Known canaries: {known}"
    if not enqueue_manual_canary(name):
        return "Error: scheduler unavailable — canary not queued."
    return (
        f"Canary '{name}' queued for background execution. Results land in "
        f"canary_runs (see canary_status) — the run takes as long as the task does."
    )


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
        name="canary_run",
        func=canary_run,
        description=(
            "Run one golden-task canary in the background (headless full-pipeline "
            "session scored by its deterministic gates). Use after approving a new "
            "canary or to spot-check a regression. Returns immediately; check "
            "canary_status for results."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Canary name (directory under data/canaries)"},
            },
            "required": ["name"],
        },
        category="evaluation",
        tags=["canary", "regression", "benchmark", "golden", "sweep", "measure"],
        timeout=15,
        parallel_safe=False,
        safety_level="caution",  # spawns a background LLM pipeline run
        denied_session_types={"canary", "worker"},
    )
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
