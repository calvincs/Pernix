"""Pernix — Deterministic gates: host-observable evidence Reflect cannot
overrule (adaptation plan 3a).

A gate is a user-authored shell command; its exit code is the verdict. Gates
run in FINALIZING immediately before Reflect, re-running on every reflect
retry attempt — that is what the unchanged-watch_paths reuse guard exists
for. A passing gate verifies only what that gate checks.
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from config import settings

logger = logging.getLogger("pernix.gates")

GATE_TIMEOUT_S = 120
GATE_OUTPUT_TAIL = 4096


@dataclass
class GateResult:
    name: str
    command: str
    passed: bool
    exit_code: int | None = None
    output_tail: str = ""
    reused: bool = False  # unchanged-watch_paths guard reused a prior failure
    error: str = ""  # runner-level problem (timeout, spawn failure)
    fingerprint: str = ""
    scope: str = "session"

    def to_payload(self) -> dict:
        return {
            "kind": "gate",
            "name": self.name,
            "command": self.command,
            "passed": self.passed,
            "exit_code": self.exit_code,
            "output_tail": self.output_tail[-1500:],
            "reused": self.reused,
            "error": self.error,
        }


def _fingerprint(watch_paths: list[str], base: Path) -> str:
    """mtime/size scan over the gate's declared watch set. Empty watch set ->
    empty fingerprint (no reuse guard: the gate always re-runs)."""
    if not watch_paths:
        return ""
    h = hashlib.sha256()
    for raw in sorted(watch_paths):
        p = (base / raw).resolve() if not os.path.isabs(raw) else Path(raw)
        entries: list[Path]
        if p.is_dir():
            entries = sorted(q for q in p.rglob("*") if q.is_file())
        elif p.is_file():
            entries = [p]
        else:
            h.update(f"missing:{raw}".encode())
            continue
        for q in entries:
            try:
                st = q.stat()
                h.update(f"{q}:{st.st_mtime_ns}:{st.st_size}".encode())
            except OSError:
                h.update(f"unstat:{q}".encode())
    return h.hexdigest()


def _gate_env(workspace: Path) -> dict:
    venv_bin = workspace / ".venv" / "bin"
    return {
        "PATH": f"{venv_bin}:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(workspace),
        "LANG": "C.UTF-8",
    }


def run_gates(session_id: str, prior: dict[str, tuple[str, "GateResult"]], attempt: int) -> list[GateResult]:
    """Run every enabled gate for the session. Blocking — call via to_thread.

    prior: {gate_name: (fingerprint, GateResult)} from earlier attempts this
    turn. A gate that FAILED on a previous attempt with an unchanged
    fingerprint is reused, never on the first retry (attempt <= 2 always
    re-runs) — turns whose deliverable isn't a watched file must be able to
    clear a stale failure by actually changing something.
    """
    from core.tools.paths import workspace as _workspace
    from db import models as db

    rows = db.get_gates(session_id)
    if not rows:
        return []
    ws = _workspace()
    # Honor the session's workspace override (plan 1g). Gates run from
    # post-hooks, outside the per-tool-call ContextVar window that
    # execute_sync sets — so resolve the override from the session directly
    # (canary runs execute in a temp workspace; their gates must too).
    try:
        from sessions.manager import get_manager

        _s = get_manager().get(session_id)
        _ov = getattr(_s, "workspace_override", None) if _s else None
        if _ov:
            ws = Path(_ov).resolve()
    except Exception:
        pass
    results: list[GateResult] = []
    for row in rows:
        name = row["name"]
        fp = _fingerprint(row.get("watch_paths") or [], ws)
        if attempt > 2 and fp:
            prev = prior.get(name)
            if prev is not None and prev[0] == fp and not prev[1].passed:
                reused = GateResult(
                    name=name,
                    command=row["command"],
                    passed=False,
                    exit_code=prev[1].exit_code,
                    output_tail=prev[1].output_tail,
                    reused=True,
                    fingerprint=fp,
                    scope=row.get("scope", "session"),
                )
                results.append(reused)
                logger.info("Gate '%s' not re-run: no changes under watch_paths since last failure", name)
                continue
        results.append(_run_one(row, ws, fp))
    return results


def _run_one(row: dict, workspace: Path, fingerprint: str) -> GateResult:
    name, command = row["name"], row["command"]
    cwd = row.get("cwd") or str(workspace)
    start = time.monotonic()
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            env=_gate_env(workspace),
            capture_output=True,
            text=True,
            timeout=GATE_TIMEOUT_S,
        )
        tail = ((proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else ""))[-GATE_OUTPUT_TAIL:]
        result = GateResult(
            name=name,
            command=command,
            passed=proc.returncode == 0,
            exit_code=proc.returncode,
            output_tail=tail.strip(),
            fingerprint=fingerprint,
            scope=row.get("scope", "session"),
        )
    except subprocess.TimeoutExpired:
        result = GateResult(
            name=name,
            command=command,
            passed=False,
            error=f"timed out after {GATE_TIMEOUT_S}s",
            fingerprint=fingerprint,
            scope=row.get("scope", "session"),
        )
    except Exception as e:
        result = GateResult(
            name=name,
            command=command,
            passed=False,
            error=f"{type(e).__name__}: {e}",
            fingerprint=fingerprint,
            scope=row.get("scope", "session"),
        )
    logger.info(
        "Gate '%s': %s (exit=%s, %.1fs)",
        name,
        "PASS" if result.passed else "FAIL",
        result.exit_code,
        time.monotonic() - start,
    )
    return result


def failing(results: list[GateResult]) -> list[GateResult]:
    return [r for r in results if not r.passed]


def format_evidence(results: list[GateResult]) -> str:
    """The GATE EVIDENCE block appended to Reflect's evidence. Deterministic
    facts — Reflect quotes them; the clamp in reflect_on_session enforces
    them regardless of what the model concludes."""
    if not results:
        return ""
    lines = ["GATE EVIDENCE (deterministic checks; a failing gate means the turn CANNOT pass):"]
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        detail = f"exit={r.exit_code}" if r.exit_code is not None else (r.error or "no result")
        reused = " [reused prior failure — watch_paths unchanged]" if r.reused else ""
        lines.append(f"- {r.name}: {status} ({detail}){reused}  cmd: {r.command}")
        if not r.passed and r.output_tail:
            lines.append(f"  output tail:\n  {r.output_tail[-800:]}")
    return "\n".join(lines)


def format_retry_guidance(results: list[GateResult]) -> str:
    """Failure text carried to the retry attempt via reflect_lessons — the
    only channel the next attempt's scout message actually reads."""
    bad = failing(results)
    if not bad:
        return ""
    lines = ["Deterministic gates failed — the retry must make these pass:"]
    for r in bad:
        detail = f"exit {r.exit_code}" if r.exit_code is not None else (r.error or "failed")
        lines.append(f"- `{r.name}` ({detail}): {r.command}")
        if r.output_tail:
            lines.append(f"  {r.output_tail[-400:]}")
    return "\n".join(lines)


@dataclass
class GateHistory:
    """Per-turn gate memory living on AgentSession: fingerprints + last
    results per gate, reset when a new turn starts."""

    turn_id: int | None = None
    prior: dict[str, tuple[str, GateResult]] = field(default_factory=dict)

    def reset_if_new_turn(self, turn_id: int | None) -> None:
        if turn_id != self.turn_id:
            self.turn_id = turn_id
            self.prior.clear()

    def record(self, results: list[GateResult]) -> None:
        for r in results:
            if r.fingerprint:
                self.prior[r.name] = (r.fingerprint, r)


def run_gates_for_turn(session_id: str, session_obj, attempt: int) -> list[GateResult]:
    """Entry point for hooks: history tracking + execution. Blocking."""
    if not settings.gates_enabled:
        return []
    history: GateHistory = getattr(session_obj, "_gate_history", None) or GateHistory()
    session_obj._gate_history = history
    history.reset_if_new_turn(getattr(session_obj, "current_turn_user_msg_id", None))
    results = run_gates(session_id, history.prior, attempt)
    history.record(results)
    return results
