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


def check_gate_command(command: str) -> str | None:
    """Reject a gate command that the bash tool would also reject.

    A gate is agent-registerable shell that then runs unattended at every turn
    end, so it must not be a way around the policy `bash` is subject to. The
    denylist scan is applied in every shell_security_mode, including "strict":
    strict mode's allowlist is a first-word check tuned for interactive
    commands and would reject ordinary gates (`pytest -q`, `make test`), while
    the denylist is the part that speaks to blast radius.

    Returns an error string (the bash tool's own wording) or None.
    """
    from core.tools.builtin.core_tools import _check_command_security

    return _check_command_security(command)


def resolve_gate_cwd(cwd: str, workspace: Path) -> Path:
    """Resolve a gate's working directory inside the workspace.

    add_gate takes cwd from the model, and gates run with shell=True, so an
    unconstrained cwd relocates the whole policy surface (`cwd="/"`). Mirrors
    safe_read_path's rule: a relative path is taken against the workspace (not
    the server's process cwd, which is the source tree), resolve() collapses
    `..` and symlinks, then containment is required.

    Raises ValueError when the result escapes the workspace.
    """
    base = workspace.resolve()
    if not cwd:
        return base
    candidate = Path(cwd)
    resolved = (candidate if candidate.is_absolute() else base / candidate).resolve()
    if not resolved.is_relative_to(base):
        raise ValueError(f"Error: gate cwd must be inside the workspace ({base}); got {resolved}")
    return resolved


def check_gate_cwd(cwd: str, workspace: Path) -> str | None:
    """Validation-only wrapper over resolve_gate_cwd. Returns an error or None."""
    try:
        resolve_gate_cwd(cwd, workspace)
    except ValueError as e:
        return str(e)
    except OSError as e:
        return f"Error: gate cwd could not be resolved: {e}"
    return None


def _gate_child_setup() -> None:
    """Applied in the gate child: new session + the same rlimits bash uses.

    setsid gives the gate its own process group so a timeout kills the whole
    tree rather than leaving orphans behind every turn.
    """
    import resource

    os.setsid()
    try:
        as_limit = int(getattr(settings, "shell_address_space_limit_bytes", 0) or 0)
        fsize_limit = int(getattr(settings, "shell_fsize_limit_bytes", 0) or 0)
        if as_limit > 0:
            resource.setrlimit(resource.RLIMIT_AS, (as_limit, as_limit))
        if fsize_limit > 0:
            resource.setrlimit(resource.RLIMIT_FSIZE, (fsize_limit, fsize_limit))
    except (ValueError, OSError):
        pass


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


def _refused(row: dict, fingerprint: str, reason: str) -> GateResult:
    """A gate that policy will not run. Refusal is a FAILURE, not a skip: an
    unrunnable gate has verified nothing, and the same clamp that blocks a pass
    on a failing gate should block one here. Gates registered before command
    and cwd were validated at registration time land here — logged and refused
    rather than raising, so one legacy row cannot break the whole turn-end
    sweep for every other gate in the session."""
    logger.warning("Gate '%s' refused by shell policy: %s", row.get("name"), reason)
    return GateResult(
        name=row["name"],
        command=row["command"],
        passed=False,
        error=reason,
        fingerprint=fingerprint,
        scope=row.get("scope", "session"),
    )


def _run_one(row: dict, workspace: Path, fingerprint: str) -> GateResult:
    name, command = row["name"], row["command"]
    # Re-validate at execution time, not only at add_gate: rows persist in the
    # DB, so gates registered before validation existed (or written by any
    # other path into the table) would otherwise still run unchecked.
    blocked = check_gate_command(command)
    if blocked:
        return _refused(row, fingerprint, blocked)
    try:
        cwd = str(resolve_gate_cwd(row.get("cwd") or "", workspace))
    except (ValueError, OSError) as e:
        return _refused(row, fingerprint, str(e))
    start = time.monotonic()
    proc = None
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=cwd,
            env=_gate_env(workspace),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            preexec_fn=_gate_child_setup,
        )
        stdout, stderr = proc.communicate(timeout=GATE_TIMEOUT_S)
        tail = ((stdout or "") + ("\n" + stderr if stderr else ""))[-GATE_OUTPUT_TAIL:]
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
        # Kill the group, not just the shell: gates run unattended every turn,
        # so a leaked child here accumulates silently.
        if proc is not None:
            from core.tools.builtin.core_tools import _kill_process_tree

            _kill_process_tree(proc)
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
    """Failure text carried to the retry attempt via turn.reflect_lessons — the
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
    """Per-turn gate memory living on AgentSession.turn: fingerprints + last
    results per gate.

    The turn_id check is belt-and-braces now that TurnState is replaced
    wholesale at every turn boundary — it also covers a session object built
    outside the manager (canary harness, tests) that never gets a fresh turn.
    """

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
    turn = session_obj.turn
    history: GateHistory = turn.gate_history or GateHistory()
    turn.gate_history = history
    history.reset_if_new_turn(getattr(session_obj, "current_turn_user_msg_id", None))
    results = run_gates(session_id, history.prior, attempt)
    history.record(results)
    return results
