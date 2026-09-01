"""Shared path-safety utilities for all file tools."""

from __future__ import annotations

from contextvars import ContextVar
from pathlib import Path

from config import settings

# Per-call workspace override, set by ToolRegistry.execute_sync from the
# session's workspace_override before invoking a tool function and reset
# after. A ContextVar (not a global) so concurrent tool calls from different
# sessions on different threads never see each other's override — execute_sync
# runs in the same thread as the tool function regardless of which executor
# dispatched it, so set/reset around the call is always visible to the tool
# and only the tool.
WORKSPACE_OVERRIDE: ContextVar[str | None] = ContextVar("workspace_override", default=None)

# Per-call space workspace home (v33), plumbed the same way. A SOFT default,
# not a sandbox: it becomes the first root (so bare relative paths resolve
# into the space folder) and bash's cwd/HOME, while the global workspace,
# /tmp and the other roots all remain — cross-visibility between spaces and
# ordinary sessions is a feature. WORKSPACE_OVERRIDE (a real sandbox) always
# wins: when an override is active the home is ignored entirely.
WORKSPACE_HOME: ContextVar[str | None] = ContextVar("workspace_home", default=None)

# Files whose names are reserved for agent instructions / secrets.
# Only protected when located *directly* at the root of an allowed tree,
# not deep in the hierarchy where a user might legitimately keep a skill
# file or sample data with the same name.
PROTECTED_FILES = frozenset(
    {
        "sessions.md",
        "instructions.md",
        "soul.md",
        "rules.md",
        "safety.md",
        ".env",
        "settings.json",
    }
)

# `.venv` is here for the same reason `.git` is, but the consequence is
# sharper: data/workspace/.venv sits inside the only write root, and
# ensure_workspace_venv_on_path() puts its site-packages on sys.path for every
# source="custom" tool. Without this entry, file_write — a "safe" tool — could
# drop a module into site-packages that the server then imports and executes
# in-process, with the full server environment. bash still manages the venv
# (pip, python -m venv); only the path-tool surface is closed.
PROTECTED_DIRS = frozenset({".git", "__pycache__", ".venv"})


def workspace() -> Path:
    """The active workspace root for the current tool call.

    Honors the session's workspace_override when one is set (canary runs,
    isolated tasks); otherwise the shared global workspace. Callers must
    always call this fresh rather than caching the result — the override is
    per-call state.
    """
    override = WORKSPACE_OVERRIDE.get()
    if override:
        return Path(override).resolve()
    return Path(settings.workspace_dir).resolve()


def workspace_home() -> Path:
    """The effective working folder for the current tool call: the space
    home when one is active (and no sandbox override is), else workspace().
    Used for bash cwd/HOME and kernel spawn cwd — never for containment."""
    home = WORKSPACE_HOME.get()
    if home and WORKSPACE_OVERRIDE.get() is None:
        return Path(home).resolve()
    return workspace()


# Harness-data subtrees that live under DATA_DIR, never under the workspace.
# `workspace` and `skills` are deliberately absent: the first is the tools'
# own root and the second is already an allowed read root, so a path leading
# with either is not a root-naming mistake.
HARNESS_DATA_DIRS = frozenset(
    {
        "adaptive",
        "telos",
        "memories",
        "canaries",
        "kernels",
        "sessions",
        "logs",
        "agent",
        "cache",
        "workflows",
        "candor",
    }
)


def data_root() -> Path:
    """Absolute path of the harness data tree (data/), which holds every
    subtree the workspace-rooted file tools cannot reach."""
    from config import DATA_DIR

    return Path(DATA_DIR).resolve()


def root_mismatch_hint(path: str) -> str:
    """Suffix explaining a workspace-root failure caused by a harness-data path.

    glob/grep/file_read/file_write resolve relative paths under the workspace
    root, so `data/telos` silently becomes <workspace>/data/telos and fails
    with an error that reads as "wrong path" when the real fault is "wrong
    root". Returns "" unless the path is one of those cases — an ordinary
    workspace typo must keep its clean error.
    """
    if not path:
        return ""
    try:
        ws = workspace()
        data = data_root()
    except Exception:
        return ""

    raw = path.strip()
    while raw.startswith("./"):
        raw = raw[2:]
    if not raw:
        return ""

    candidate = Path(raw)
    if candidate.is_absolute():
        try:
            target = candidate.resolve()
        except OSError:
            return ""
        # An absolute path inside the workspace is not a root mistake.
        if not target.is_relative_to(data) or target.is_relative_to(ws):
            return ""
    else:
        # The tools resolve relative paths under the workspace; if that
        # resolution would have worked, the root was never the problem.
        if (ws / raw).exists():
            return ""
        parts = Path(raw).parts
        first = parts[0] if parts else ""
        if first in ("data", data.name):
            rest = Path(*parts[1:]) if len(parts) > 1 else None
            # data/workspace/... is the workspace itself under its full name.
            if rest is not None and (data / rest).is_relative_to(ws):
                return ""
        elif first in HARNESS_DATA_DIRS and (data / first).exists():
            pass
        else:
            return ""

    return (
        f" — resolved against workspace root ({ws}). Harness data (telos ledgers, memory "
        f"files, canary state) lives outside the workspace at {data}; use bash with an "
        f"absolute path (e.g. ls {data}/telos/questions/) or purpose-built tools "
        f"(telos_status, recall, search_sessions)."
    )


def kernel_state_root() -> Path | None:
    """Root of the session kernels' state trees (data/kernels), or None when
    the kernel is off. Read-only by intent: bound tool results are spilled to
    data/kernels/<sid>/payloads/, and the stub the model is shown advertises
    that exact path — without it as a read root, every binding pointer is
    dead on arrival. Never a write root."""
    if not getattr(settings, "session_kernel_enabled", False):
        return None
    try:
        from core.kernel import KERNEL_STATE_ROOT  # module attr: tests repoint it

        return Path(KERNEL_STATE_ROOT).resolve()
    except Exception:
        return None


def tool_output_root() -> Path:
    """Root of the truncation spill tree (data/.tool_output).

    Read-only by intent, and for the same reason kernel_state_root() exists:
    truncate_output() writes the full output there and tells the model to
    `file_read(path="data/.tool_output/<tool>_<ts>.txt", ...)`. Without it as a
    read root, that pointer falls through to the workspace-relative branch and
    resolves to a path that never exists — every drill-in from bash, grep and
    rlm_process dies with "File not found". Never a write root.
    """
    from core.tools.truncation import TOOL_OUTPUT_DIR  # module attr: tests repoint it

    return Path(TOOL_OUTPUT_DIR).resolve()


def allowed_read_roots() -> list[Path]:
    """Directories that file_read may access (workspace + skills + the
    truncation spill tree, plus the kernel payload spill tree when the
    session kernel is on).

    Order matters: workspace first, so a bare relative name still resolves
    against the workspace rather than being captured by a later root.
    """
    roots = _base_roots()
    skills = Path(settings.skills_dir).resolve()
    if skills not in roots:
        roots.append(skills)
    try:
        tool_output = tool_output_root()
        if tool_output not in roots:
            roots.append(tool_output)
    except Exception:
        pass
    kernels = kernel_state_root()
    if kernels is not None and kernels not in roots:
        roots.append(kernels)
    return roots


def allowed_write_roots() -> list[Path]:
    """Directories that file_write/file_edit may access.

    /tmp is included deliberately in DEFAULT mode: bash has always been able
    to write there freely, so refusing it in the file tools bought no
    containment — only mid-task "Path not within allowed directories"
    surprises (field case: bp35 solver scratch files). Approved by Calvin
    2026-08-25. Under a session workspace_override (canary runs, isolated
    tasks) the override stays the ONLY root: isolation is the whole point
    there, and pytest/canary sandboxes themselves live under /tmp.
    """
    return _base_roots()


def _base_roots() -> list[Path]:
    """Workspace-ish roots shared by read and write: the space home first
    (when active — so bare relative paths resolve there), then the global
    workspace, then /tmp except under a sandbox override."""
    roots: list[Path] = []
    home = WORKSPACE_HOME.get()
    if home and WORKSPACE_OVERRIDE.get() is None:
        roots.append(Path(home).resolve())
    ws = workspace()
    if ws not in roots:
        roots.append(ws)
    if WORKSPACE_OVERRIDE.get() is None:
        roots.append(Path("/tmp").resolve())
    return roots


def check_protected(resolved: Path, roots: list[Path]) -> None:
    """Raise ValueError if path targets a protected location.

    A file is protected when:
      - Its name (case-insensitive) is in PROTECTED_FILES **and** it sits
        directly in one of the allowed roots (not in a subdirectory).
      - Any path component (case-insensitive) is in PROTECTED_DIRS.
    """
    if resolved.name.lower() in PROTECTED_FILES:
        for root in roots:
            if resolved.parent == root:
                raise ValueError(f"Protected file: {resolved.name}")
    for part in resolved.parts:
        if part.lower() in PROTECTED_DIRS:
            raise ValueError(f"Protected directory in path: {part}")


def _resolve_within(path: str, roots: list[Path], create_roots: bool = False) -> Path:
    """Resolve path within the given roots, preventing traversal.

    When create_roots is True, missing root directories are created. This
    only happens on the write path — reads never touch the filesystem for
    root creation.
    """
    if create_roots:
        for root in roots:
            root.mkdir(parents=True, exist_ok=True)

    resolved = Path(path).resolve()

    for root in roots:
        if resolved.is_relative_to(root):
            check_protected(resolved, roots)
            return resolved

    # Relative path: join against roots in order. When a space workspace-home
    # is active it sits at roots[0], and we prefer a candidate that already
    # EXISTS — a bare name like "SYSTEM-MAP.md" must still find the global
    # workspace copy instead of resolving to a phantom path in the home.
    # Applies to writes too, deliberately: edits land on the file where it
    # lives; only genuinely new files default into the home. Without a home
    # the historical rule stands unchanged: first root wins outright.
    prefer_existing = WORKSPACE_HOME.get() is not None and WORKSPACE_OVERRIDE.get() is None
    first_valid: Path | None = None
    for root in roots:
        candidate = (root / path).resolve()
        if candidate.is_relative_to(root):
            if not prefer_existing or candidate.exists():
                check_protected(candidate, roots)
                return candidate
            if first_valid is None:
                first_valid = candidate
    if first_valid is not None:
        check_protected(first_valid, roots)
        return first_valid

    raise ValueError(f"Path not within allowed directories: {path}")


def safe_read_path(path: str) -> Path:
    """Resolve path for reading (workspace + skills). Does not create dirs."""
    return _resolve_within(path, allowed_read_roots(), create_roots=False)


def safe_write_path(path: str) -> Path:
    """Resolve path for writing (workspace only). Ensures workspace exists."""
    return _resolve_within(path, allowed_write_roots(), create_roots=True)


def ensure_workspace_venv_on_path() -> None:
    """Add workspace venv site-packages to sys.path (idempotent).

    Core tools run in the project venv. Custom tools (source='custom') need
    packages installed via install_package into data/workspace/.venv.
    Called before any custom_* module is imported or reloaded.
    """
    import glob
    import sys

    ws_lib = Path(settings.workspace_dir).resolve() / ".venv" / "lib"
    site_pkgs = next(glob.iglob(str(ws_lib / "python*" / "site-packages")), None)
    if site_pkgs and site_pkgs not in sys.path:
        sys.path.insert(0, site_pkgs)
