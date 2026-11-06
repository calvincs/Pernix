"""Pernix — deterministic checks on a worker's turn.

These replace the LLM verdict as the source of a worker's result header — the
one reflect output a parent actually acts on (it reads the transcript of every
worker flagged non-pass). The 2026-11-05 review found the grader wrong on most
of the non-pass verdicts that could be checked, and false ESCALATED headers on
finished work (8284dd1ced45, 64b7ff916658). The worker retries that did pay
were two shapes a program can see on its own:

  * the task named an output file and the worker never wrote it
    (d228d7d85697, 51a6e9e1afe6, 8aed50b072e6);
  * the reply cited identifiers or table rows no tool result contains
    (614eee2726a3, 64b7ff916658) — the mechanical `_grounding_check`.

Plus the deterministic gates that already ran for the turn. Nothing here calls
a model. The result is written as a `reflect` row with reflect_mode="checks",
so every existing reader (finalize stamp, get_worker_result, the parent's
resume manifest, sibling sharing) reads it through run_scoped_reflect.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger("pernix.worker_checks")

# A file the task asks to be written: a write-verb, then (within the same
# clause) a path with a known output extension.
_EXT = r"(?:md|markdown|txt|json|jsonl|csv|tsv|yaml|yml|html|xml|py|sh|sql|log|toml|ini|png|svg)"
_PATH = rf"(?P<path>(?:~|\.{{1,2}})?/?[\w.\-]+(?:/[\w.\-]+)*\.{_EXT})"
_WRITE_RE = re.compile(
    rf"\b(?:write|writes|writing|save|saves|saving|create|creates|creating|produce|output|"
    rf"generate|export|store|persist|dump|put)\b[^\n.;]{{0,80}}?(?<![\w/]){_PATH}(?![\w/])",
    re.IGNORECASE,
)
_URLISH = re.compile(r"^(?:https?:|www\.)", re.IGNORECASE)

# A handful of ungrounded tokens is normal (coined names, examples). Flag only
# above this, or on any unverified id→value table row.
UNGROUNDED_FLAG_MIN = 3

_WALK_CAP = 4000  # files visited when looking for a deliverable by basename


def deliverable_paths(task: str) -> list[str]:
    """Output files the task text asks the worker to write, in order, unique."""
    out: list[str] = []
    for m in _WRITE_RE.finditer(task or ""):
        p = m.group("path").strip().rstrip(".,)")
        if not p or _URLISH.match(p) or p in out:
            continue
        out.append(p)
    return out[:8]


def _find(path: str, workspace: Path) -> str | None:
    """Where the deliverable is, or None. Exact location first, then the same
    basename anywhere under the workspace (agents often pick a subfolder)."""
    p = Path(os.path.expanduser(path))
    candidates = [p] if p.is_absolute() else [workspace / p]
    for c in candidates:
        try:
            if c.is_file() and c.stat().st_size > 0:
                return str(c)
        except OSError:
            continue
    base = p.name
    seen = 0
    try:
        for root, _dirs, files in os.walk(workspace):
            if base in files:
                hit = Path(root) / base
                try:
                    if hit.stat().st_size > 0:
                        return str(hit)
                except OSError:
                    pass
            seen += len(files)
            if seen > _WALK_CAP:
                break
    except OSError:
        pass
    return None


def run_checks(
    session_id: str,
    *,
    task: str,
    final_reply: str,
    attempt_msgs: list[dict],
    workspace: Path,
    gate_results: list | None = None,
) -> dict:
    """Every deterministic check that applies to this turn, and what it found."""
    from core.gates import failing

    found: dict[str, str] = {}
    missing: list[str] = []
    for path in deliverable_paths(task):
        where = _find(path, workspace)
        if where:
            found[path] = where
        else:
            missing.append(path)

    ungrounded: list[str] = []
    unverified_rows: list[str] = []
    try:
        from core.reflect import _grounding_check

        g = _grounding_check(final_reply or "", attempt_msgs, task or "")
        ungrounded = list(g.get("ungrounded") or [])
        unverified_rows = list(g.get("unverified_rows") or [])
    except Exception as e:
        logger.debug("grounding check failed for %s: %s", session_id, e)

    gates = list(gate_results or [])
    gates_failed = [g.name for g in failing(gates)]
    gates_passed = sum(1 for g in gates if getattr(g, "passed", False))

    flags: list[str] = []
    if missing:
        flags.append("missing deliverable: " + ", ".join(missing))
    if gates_failed:
        flags.append("gate failed: " + ", ".join(gates_failed))
    if unverified_rows:
        flags.append(f"{len(unverified_rows)} table row(s) pair values no single tool result shows together")
    if len(ungrounded) >= UNGROUNDED_FLAG_MIN:
        flags.append("cites ids found in no tool result: " + ", ".join(ungrounded[:6]))

    checked = bool(found or missing or gates)
    return {
        "deliverables_found": found,
        "deliverables_missing": missing,
        "gates_failed": gates_failed,
        "gates_passed": gates_passed,
        "ungrounded": ungrounded[:12],
        "unverified_rows": unverified_rows[:6],
        "flags": flags,
        "checked": checked,
    }


def checks_row(checks: dict) -> dict:
    """The reflect-row payload the existing readers understand.

    verdict: "flagged" when any check failed, "pass" when checks ran and all
    passed, "unchecked" when nothing applied (no named file, no gate).
    """
    if checks["flags"]:
        verdict, verification = "flagged", "failed"
        reasoning = "; ".join(checks["flags"])
    elif checks["checked"]:
        verdict, verification = "pass", "verified"
        parts = []
        if checks["deliverables_found"]:
            parts.append(f"{len(checks['deliverables_found'])} named file(s) exist")
        if checks["gates_passed"]:
            parts.append(f"{checks['gates_passed']} gate(s) passed")
        reasoning = "; ".join(parts) or "checks passed"
    else:
        verdict, verification = "unchecked", ""
        reasoning = "no named output file or gate to check"
    return {
        "verdict": verdict,
        "reasoning": reasoning,
        "verification": verification,
        "verification_reason": "deterministic checks (no LLM)",
        "missing": "; ".join(checks["flags"]),
        "reflect_mode": "checks",
        "checks": checks,
    }


def missing_deliverable_gate(missing: list[str]):
    """A synthetic failing gate, so a missing named file rides the existing
    deterministic retry path (_apply_gate_retry_fallback)."""
    from core.gates import GateResult

    return GateResult(
        name="deliverable_exists",
        command="test -s " + " ".join(missing),
        passed=False,
        exit_code=1,
        output_tail=(
            "The task asked for these file(s) and none exists (checked the path and the same "
            "file name anywhere in the workspace): " + ", ".join(missing) + ". Write them, or say "
            "plainly in your final reply why they cannot be written."
        ),
        scope="turn",
    )


def dumps(row: dict) -> str:
    return json.dumps(row)
