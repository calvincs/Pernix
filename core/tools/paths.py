"""Shared path-safety utilities for all file tools."""

from __future__ import annotations

from pathlib import Path

from config import settings

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

PROTECTED_DIRS = frozenset({".git", "__pycache__"})


def workspace() -> Path:
    return Path(settings.workspace_dir).resolve()


def allowed_read_roots() -> list[Path]:
    """Directories that file_read may access (workspace + skills + workflows)."""
    roots = [workspace()]
    skills = Path(settings.skills_dir).resolve()
    if skills not in roots:
        roots.append(skills)
    workflows = Path(settings.workflows_dir).resolve()
    if workflows not in roots:
        roots.append(workflows)
    return roots


def allowed_write_roots() -> list[Path]:
    """Directories that file_write/file_edit may access (workspace only)."""
    return [workspace()]


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

    for root in roots:
        candidate = (root / path).resolve()
        if candidate.is_relative_to(root):
            check_protected(candidate, roots)
            return candidate

    raise ValueError(f"Path not within allowed directories: {path}")


def safe_read_path(path: str) -> Path:
    """Resolve path for reading (workspace + skills). Does not create dirs."""
    return _resolve_within(path, allowed_read_roots(), create_roots=False)


def safe_write_path(path: str) -> Path:
    """Resolve path for writing (workspace only). Ensures workspace exists."""
    return _resolve_within(path, allowed_write_roots(), create_roots=True)
