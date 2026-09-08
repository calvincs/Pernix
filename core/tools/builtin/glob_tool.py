"""Pernix — Glob file pattern search tool."""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from core.tools.paths import owning_root, relative_path_roots, resolve_workspace_path, root_mismatch_hint
from core.tools.paths import workspace as _workspace
from core.tools.paths import workspace_home as _default_root

logger = logging.getLogger("pernix.tools.glob")

# How many paths are rendered inline.
MAX_RESULTS = 300
# How many are collected before the walk gives up. The old fallback broke at
# 500 and then used `total` as if it were the real count, so every number
# derived from it saturated in silence — the omitted count included. Reaching
# this ceiling now makes the total an explicit floor instead.
SCAN_CEILING = 5000


def glob_search(pattern: str, path: str = "") -> str:
    """Find files matching a glob pattern.

    Uses git ls-files when in a git repo (respects .gitignore),
    falls back to pathlib.glob otherwise. Results sorted by
    modification time (newest first), limited to 300.

    Args:
        pattern: Glob pattern (e.g. '**/*.py', 'src/**/*.ts', '*.md').
        path: Optional subdirectory to search in. Default: the working root.
    """
    # Same relative-path contract as file_read, bash and grep (2026-09-08):
    # `impl` is one directory, not one per tool. Containment is still the
    # workspace-ish roots — the default moved, the fence did not.
    if path:
        try:
            candidate = resolve_workspace_path(path)
        except ValueError as e:
            return f"Error: {e}{root_mismatch_hint(path)}"
        if not candidate.is_dir():
            return f"Error: Not a directory: {path}{root_mismatch_hint(path)}"
        search_root = candidate
    else:
        search_root = _default_root()
    root = owning_root(search_root)
    contained = relative_path_roots()

    def _inside(fp: Path) -> bool:
        return any(fp.is_relative_to(r) for r in contained)

    matches: list[Path] = []
    saturated = False  # the walk stopped at SCAN_CEILING, so `total` is a floor

    # Try git ls-files first (respects .gitignore)
    try:
        # git walks up to its repo root, so a .git at any of these means the
        # search root is inside a repo — checking only the global workspace
        # skipped a space home that is its own checkout.
        if any((p / ".git").exists() for p in (search_root, root, _workspace())):
            result = subprocess.run(
                ["git", "ls-files", "--cached", "--others", "--exclude-standard", pattern],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=str(search_root),
            )
            if result.returncode == 0 and result.stdout.strip():
                for line in result.stdout.strip().split("\n"):
                    fp = (search_root / line).resolve()
                    if _inside(fp) and fp.exists():
                        matches.append(fp)
                    if len(matches) >= SCAN_CEILING:
                        saturated = True
                        break
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass  # Fall through to pathlib

    # Fallback to pathlib.glob if git didn't find anything
    if not matches:
        try:
            for fp in search_root.glob(pattern):
                if fp.is_file() and _inside(fp):
                    matches.append(fp)
                if len(matches) >= SCAN_CEILING:  # Safety limit before sorting
                    saturated = True
                    break
        except Exception as e:
            return f"Error: Invalid glob pattern: {e}"

    # Two separate honesty obligations, kept as two separate lines: which tree
    # this listing came from (H07) and the bounds it ran under (H16).
    root_line = f"[root: {root}]"
    scope = f"{path or '.'} pattern={pattern!r}"
    caps = f"[scope: {scope} | caps: {MAX_RESULTS} shown, {SCAN_CEILING:,}-file scan ceiling, newest first]"

    if not matches:
        head = f"No files found matching '{pattern}'" + (f" in {path}" if path else "")
        return f"{root_line}\n{head}\n{caps}"

    # Sort by modification time (newest first)
    try:
        matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        pass

    total = len(matches)
    shown = matches[:MAX_RESULTS]

    # Format output as relative paths
    lines = []
    for fp in shown:
        try:
            rel = fp.relative_to(root)
        except ValueError:
            rel = fp
        lines.append(str(rel))

    # The arithmetic, spelled out because the old version's was wrong in both
    # directions: `total - 100` invented 50 omissions for a 150-file result
    # where nothing was omitted, and claimed 300 for a 400-file one where 100
    # were.
    omitted = total - len(lines)
    if saturated:
        head = (
            f"[at least {total:,} files matched (scan ceiling reached — the true total is "
            f"unknown); showing {len(lines):,}; at least {omitted:,} not shown]"
        )
    elif omitted:
        head = f"[{total:,} files matched; showing {len(lines):,}; {omitted:,} not shown]"
    else:
        head = f"[{total:,} file{'s' if total != 1 else ''} found; showing all {len(lines):,}]"

    return f"{root_line}\n" + "\n".join(lines) + f"\n\n{head}\n{caps}"


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(reg) -> None:
    """Register glob tool."""
    reg.register(
        name="glob",
        func=glob_search,
        description=(
            "Find files by name pattern using glob syntax (e.g. '**/*.py', 'src/**/*.ts'). "
            "Respects .gitignore when in a git repo. Results sorted by modification time, limited to 300."
        ),
        parameters={
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern to match files (e.g. '**/*.py', 'core/**/*.ts', '*.md')",
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Subdirectory to search in. A relative path resolves the same way it "
                        "does for file_read and bash. Default: your working root (shown as "
                        "[root: ...] in the result)."
                    ),
                },
            },
            "required": ["pattern"],
        },
        category="core",
        tags=["find", "search", "file", "pattern", "glob", "list", "discover", "locate"],
        timeout=30,
        parallel_safe=True,
    )
