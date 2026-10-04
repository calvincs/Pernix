"""Pernix — Evaluation extension: deterministic-gate tools (add/list/remove_gate)."""

from __future__ import annotations

import logging

logger = logging.getLogger("pernix.ext.evaluation")


def add_gate(
    name: str,
    command: str,
    watch_paths: str = "",
    cwd: str = "",
    scope: str = "session",
    _context: dict | None = None,
) -> str:
    """Register a deterministic gate for this session."""
    from config import settings as _settings

    if not _settings.gates_enabled:
        return "Error: gates are disabled (settings.gates_enabled)."
    session_id = (_context or {}).get("session_id", "")
    if not session_id:
        return "Error: add_gate requires a session context."
    if not name or not command:
        return "Error: both name and command are required."
    scope = (scope or "session").strip().lower()
    if scope not in ("session", "goal"):
        return "Error: scope must be 'session' or 'goal'."

    # A gate is shell that runs unattended at every turn end, so it is held to
    # the same policy as the bash tool — checked here so the agent gets the
    # rejection while it can still fix the command, and again in gates._run_one
    # so a row that reached the table by any other route is still checked.
    from core.gates import check_gate_command, check_gate_cwd
    from core.tools.paths import workspace as _workspace
    from core.tools.paths import workspace_home as _default_root

    command = command.strip()
    blocked = check_gate_command(command)
    if blocked:
        return f"{blocked} (gate '{name}' not registered)"
    cwd = cwd.strip()
    # Validate against the root the gate will actually run in — the session's
    # working root — with the workspace still the fence it may not escape.
    bad_cwd = check_gate_cwd(cwd, _default_root(), _workspace())
    if bad_cwd:
        return f"{bad_cwd} (gate '{name}' not registered)"

    from db import models as db

    paths = [p.strip() for p in watch_paths.split(",") if p.strip()] if watch_paths else []
    db.add_gate(
        session_id,
        name.strip(),
        command,
        watch_paths=paths,
        cwd=cwd or None,
        scope=scope,
    )
    guard = (
        f" watch_paths={paths} (unchanged paths reuse a prior failure on later retries)"
        if paths
        else " (no watch_paths — the gate re-runs every attempt)"
    )
    return (
        f"Gate '{name}' registered (scope={scope}): `{command}`.{guard} "
        f"It runs before Reflect at every turn end; a non-zero exit blocks a pass verdict. "
        f"A passing gate verifies only what it checks."
    )


def list_gates(_context: dict | None = None) -> str:
    session_id = (_context or {}).get("session_id", "")
    if not session_id:
        return "Error: list_gates requires a session context."
    from db import models as db

    rows = db.get_gates(session_id, enabled_only=False)
    if not rows:
        return (
            "No gates registered for this session. Gates are per-session, so one "
            "registered in an earlier session does not carry over — register this "
            "session's own with add_gate(name=..., command=...). A gate is a shell "
            "command that runs at every turn end; a non-zero exit blocks a pass "
            "verdict."
        )
    lines = []
    for r in rows:
        state = "enabled" if r.get("enabled") else "disabled"
        watch = f" watch={r['watch_paths']}" if r.get("watch_paths") else ""
        lines.append(f"- {r['name']} [{state}] ({r.get('scope', 'session')}): `{r['command']}`{watch}")
    return "\n".join(lines)


def remove_gate(name: str, _context: dict | None = None) -> str:
    session_id = (_context or {}).get("session_id", "")
    if not session_id:
        return "Error: remove_gate requires a session context."
    from db import models as db

    if db.remove_gate(session_id, name):
        return f"Gate '{name}' removed."
    return f"Error: no gate named '{name}' in this session."


def register(reg) -> None:
    common = {"category": "evaluation", "source": "extension"}
    tags = ["evaluate", "test", "verify", "validate", "check", "qa", "quality", "assess"]

    from config import settings as _settings

    if _settings.gates_enabled:
        reg.register(
            name="add_gate",
            func=add_gate,
            description=(
                "Register a deterministic gate: a shell command that runs at every turn end "
                "before Reflect. A non-zero exit mechanically blocks a pass verdict — use for "
                "tests, builds, linters, or any host-observable completion check. Optional "
                "watch_paths (comma-separated, relative to the workspace) scope an unchanged-"
                "files guard so a stale failure isn't pointlessly re-run on later retries. "
                "scope='goal' marks the gate as a completion criterion for the session's goal "
                "(goal_complete is refused while it fails); scope='session' (default) is a "
                "plain per-turn check."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Short gate name (e.g. 'tests')"},
                    "command": {"type": "string", "description": "Shell command; exit 0 = pass"},
                    "watch_paths": {
                        "type": "string",
                        "description": "Optional comma-separated files/dirs the gate depends on",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Optional working directory, must be inside the workspace (default: workspace)",
                    },
                    "scope": {
                        "type": "string",
                        "enum": ["session", "goal"],
                        "description": "'session' (default) or 'goal' — a goal-scoped gate also blocks goal_complete",
                    },
                },
                "required": ["name", "command"],
            },
            tags=tags + ["gate", "deterministic", "ci", "build"],
            timeout=30,
            parallel_safe=False,
            # Registers shell that then runs unattended at EVERY turn end, for
            # the life of the session — a single approved call buys repeated
            # execution the user never sees again. That persistence is what
            # separates it from a one-shot `bash` call.
            safety_level="dangerous",
            **common,
        )
        reg.register(
            name="list_gates",
            func=list_gates,
            description="List this session's deterministic gates.",
            parameters={"type": "object", "properties": {}},
            tags=tags + ["gate", "list"],
            timeout=30,
            parallel_safe=True,
            safety_level="safe",
            **common,
        )
        reg.register(
            name="remove_gate",
            func=remove_gate,
            description="Remove a deterministic gate by name.",
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Gate name to remove"}},
                "required": ["name"],
            },
            tags=tags + ["gate", "remove"],
            timeout=30,
            parallel_safe=False,
            safety_level="safe",
            **common,
        )
