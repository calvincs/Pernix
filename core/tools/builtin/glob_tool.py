"""Pernix — Glob file pattern search tool."""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from core.tools.paths import workspace as _workspace

logger = logging.getLogger("pernix.tools.glob")


def glob_search(pattern: str, path: str = "") -> str:
    """Find files matching a glob pattern.

    Uses git ls-files when in a git repo (respects .gitignore),
    falls back to pathlib.glob otherwise. Results sorted by
    modification time (newest first), limited to 100.

    Args:
        pattern: Glob pattern (e.g. '**/*.py', 'src/**/*.ts', '*.md').
        path: Optional subdirectory to search in. Default: workspace root.
    """
    workspace = _workspace()
    search_root = workspace

    if path:
        candidate = (workspace / path).resolve()
        if not candidate.is_relative_to(workspace):
            return f"Error: Path not within workspace: {path}"
        if not candidate.is_dir():
            return f"Error: Not a directory: {path}"
        search_root = candidate

    matches: list[Path] = []

    # Try git ls-files first (respects .gitignore)
    try:
        git_dir = workspace / ".git"
        if git_dir.exists():
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
                    if fp.is_relative_to(workspace) and fp.exists():
                        matches.append(fp)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass  # Fall through to pathlib

    # Fallback to pathlib.glob if git didn't find anything
    if not matches:
        try:
            for fp in search_root.glob(pattern):
                if fp.is_file() and fp.is_relative_to(workspace):
                    matches.append(fp)
                if len(matches) >= 500:  # Safety limit before sorting
                    break
        except Exception as e:
            return f"Error: Invalid glob pattern: {e}"

    if not matches:
        return f"No files found matching '{pattern}'" + (f" in {path}" if path else "")

    # Sort by modification time (newest first)
    try:
        matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        pass

    # Limit to 100 results
    total = len(matches)
    matches = matches[:100]

    # Format output as relative paths
    lines = []
    for fp in matches:
        try:
            rel = fp.relative_to(workspace)
        except ValueError:
            rel = fp
        lines.append(str(rel))

    result = "\n".join(lines)
    if total > 100:
        result += f"\n\n[... {total - 100} more files not shown]"
    else:
        result += f"\n\n[{total} file{'s' if total != 1 else ''} found]"

    return result


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
            "Respects .gitignore when in a git repo. Results sorted by modification time, limited to 100."
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
                    "description": "Subdirectory to search in. Default: workspace root.",
                },
            },
            "required": ["pattern"],
        },
        category="core",
        tags=["find", "search", "file", "pattern", "glob", "list", "discover", "locate"],
        timeout=30,
        parallel_safe=True,
    )
