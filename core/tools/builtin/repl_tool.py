"""Pernix — repl tool: the session kernel's model-facing surface (plan 2c).

A persistent Python namespace per session: variables, imports, and helper
functions survive across tool rounds, turns, and compaction, and revive
across restarts from per-variable snapshots. Large results from binding-
eligible tools appear here as `tool_result_<n>` variables.
"""

from __future__ import annotations

import logging

from config import settings

logger = logging.getLogger("pernix.tools.repl")

_OUTPUT_CAP = 50_000  # chars, mirroring bash


def repl(code: str, timeout: int | None = None, _context: dict | None = None) -> str:
    """Execute Python in the session's persistent kernel."""
    from core.kernel import KernelError, get_kernel_registry

    if not settings.session_kernel_enabled:
        return "Error: the session kernel is disabled (settings.session_kernel_enabled)."
    session_id = (_context or {}).get("session_id", "")
    if not session_id:
        return "Error: repl requires a session context."

    kernel = get_kernel_registry().get_or_create(session_id)

    # Soft cancel: a user cancel SIGINTs the cell (aborting it, preserving
    # the namespace) rather than killing the kernel.
    cancel_check = None
    try:
        from sessions.manager import get_manager

        session = get_manager().get(session_id)
        if session is not None:
            cancel_check = lambda: bool(getattr(session, "cancel_requested", False))  # noqa: E731
    except Exception:
        pass

    effective_timeout = float(timeout) if timeout else 300.0
    try:
        result, note = kernel.execute(code, timeout=effective_timeout, cancel_check=cancel_check)
    except KernelError as e:
        # Kernel-level failure (child died mid-cell / unresponsive). The
        # next call respawns and revives from the last snapshot.
        return f"Error: {e} — the kernel will restart on the next repl call."

    parts: list[str] = []
    if note:
        parts.append(note)
    stdout = (result.stdout or "").rstrip()
    stderr = (result.stderr or "").rstrip()
    if stdout:
        parts.append(stdout)
    if stderr:
        # In-cell tracebacks are normal REPL iteration, NOT tool errors —
        # never prefix them with "Error:" (the executor's error classifier
        # and the StuckDetector key off that prefix).
        parts.append(stderr)
    if not stdout and not stderr:
        # Assignment-only cells are success, not failure: an empty result
        # string would be classified as a tool error (executor.py) and feed
        # the StuckDetector for perfectly correct work.
        parts.append("(no output)")
    if result.var_names and len(result.var_names) <= 20:
        parts.append(f"[vars: {', '.join(result.var_names)}]")

    output = "\n".join(parts)
    if len(output) > _OUTPUT_CAP:
        output = (
            output[:_OUTPUT_CAP]
            + f"\n… [output truncated at {_OUTPUT_CAP:,} chars — slice the variable instead of printing it whole]"
        )
    return output


def register(reg) -> None:
    # Registration-gated like Candor/RLM: flipping session_kernel_enabled
    # takes a restart. All call sites also gate at runtime.
    if not settings.session_kernel_enabled:
        return
    reg.register(
        name="repl",
        func=repl,
        description=(
            "Execute Python in this session's PERSISTENT kernel: variables, imports, and "
            "functions survive across calls, turns, and context compaction (compaction is "
            "not a reset — your kernel state remains), and revive across server restarts. "
            "Runs with cwd = the shared workspace, same venv as bash, so install_package "
            "results are importable. Large tool results are auto-bound as tool_result_<n> "
            "variables — slice/search them here instead of re-reading. Cells print like a "
            "REPL: use print() for output; an assignment-only cell returns '(no output)'. "
            "exec/eval/input are blocked (guardrail); tracebacks abort only the cell, never "
            "the kernel. Pass `timeout` (seconds, max 1800) for long computations."
        ),
        parameters={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python code to execute in the persistent namespace"},
                "timeout": {
                    "type": "integer",
                    "description": "Optional per-cell timeout override in seconds (default 300, max 1800)",
                },
            },
            "required": ["code"],
        },
        category="core",
        tags=[
            "python",
            "repl",
            "kernel",
            "analyze",
            "data",
            "transform",
            "compute",
            "dataframe",
            "parse",
            "persistent",
            "variable",
        ],
        timeout=300,
        max_timeout=1800,
        parallel_safe=False,
        worker_allowed=True,
        safety_level="caution",  # arbitrary code execution — same posture as bash
        idempotent=False,  # repeated identical cells MUST re-execute
    )
