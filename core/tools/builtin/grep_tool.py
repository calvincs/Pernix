"""Pernix — Grep tool: structured ripgrep/grep wrapper for codebase search."""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from core.tools.paths import workspace as _workspace
from core.tools.truncation import truncate_output

logger = logging.getLogger("pernix.tools.grep")

MAX_MATCHES = 100
MAX_LINE_LENGTH = 500


def _find_rg() -> str | None:
    """Find ripgrep binary, return path or None."""
    return shutil.which("rg")


def grep(pattern: str, path: str = "", include: str = "") -> str:
    """Search files for a regex pattern using ripgrep (or grep fallback).

    Returns structured output: file:line_number:content
    """
    if not pattern or not pattern.strip():
        return "Error: pattern is required"

    workspace = _workspace()
    if path:
        search_path = Path(path)
        if not search_path.is_absolute():
            search_path = workspace / path
        search_path = search_path.resolve()
        # Security: must be within workspace
        if not search_path.is_relative_to(workspace):
            return f"Error: Path not within workspace: {path}"
    else:
        search_path = workspace

    if not search_path.exists():
        return f"Error: Path not found: {search_path}"

    rg = _find_rg()
    if rg:
        args = [
            rg,
            "-n",
            "--no-heading",
            "--hidden",
            "--no-messages",
            "--color=never",
            "--max-count=200",  # per-file limit
            "--max-filesize=1M",  # skip large files
            "--regexp",
            pattern,
        ]
        if include:
            args.extend(["--glob", include])
        # Exclude common noise
        args.extend(["--glob", "!.git/", "--glob", "!__pycache__/", "--glob", "!*.pyc", "--glob", "!node_modules/"])
        args.append(str(search_path))
    else:
        # Fallback to grep
        args = ["grep", "-rn", "--color=never"]
        if include:
            args.extend(["--include", include])
        args.extend(["--exclude-dir=.git", "--exclude-dir=__pycache__", "--exclude-dir=node_modules"])
        args.extend([pattern, str(search_path)])

    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(workspace),
        )

        if result.returncode == 1:
            return "No matches found."

        if result.returncode not in (0, 1, 2) and not result.stdout:
            return f"Error: Search failed (exit {result.returncode}): {result.stderr[:200]}"

        lines = result.stdout.splitlines()
        total = len(lines)
        # Hard cap to prevent unbounded memory use on massive result sets
        if total > 5000:
            lines = lines[:5000]

        # Truncate long lines and limit total matches
        output_lines = []
        for line in lines[:MAX_MATCHES]:
            if len(line) > MAX_LINE_LENGTH:
                line = line[:MAX_LINE_LENGTH] + "..."
            output_lines.append(line)

        # Make paths relative to workspace for readability
        ws_str = str(workspace) + "/"
        output_lines = [l.replace(ws_str, "") for l in output_lines]

        result_text = "\n".join(output_lines)
        if total > MAX_MATCHES:
            # Build full output for disk persistence
            full_lines = []
            for line in lines:
                if len(line) > MAX_LINE_LENGTH:
                    line = line[:MAX_LINE_LENGTH] + "..."
                full_lines.append(line)
            full_lines = [l.replace(ws_str, "") for l in full_lines]
            full_text = "\n".join(full_lines) + f"\n\n[{total} matches]"
            full_text, _meta = truncate_output(full_text, "grep")
            result_text += f"\n\n[{total} total matches, showing first {MAX_MATCHES}]"
            if _meta.get("output_path"):
                result_text += (
                    f"\nFull results saved to: {_meta['output_path']}\n"
                    f"Use file_read(path=\"{_meta['output_path']}\", offset=<line>, limit=<count>) to view all."
                )
        else:
            result_text += f"\n\n[{total} matches]"

        return result_text

    except subprocess.TimeoutExpired:
        return "Error: Search timed out after 30s"
    except Exception as e:
        return f"Error: {e}"


def register(reg) -> None:
    """Register grep tool."""
    reg.register(
        name="grep",
        func=grep,
        description="Search file contents for a regex pattern. Returns file:line:content matches. Uses ripgrep if available.",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern to search for"},
                "path": {
                    "type": "string",
                    "description": "Directory or file to search (relative to workspace). Default: workspace root",
                },
                "include": {"type": "string", "description": "Glob pattern to filter files, e.g. '*.py', '*.{js,ts}'"},
            },
            "required": ["pattern"],
        },
        category="core",
        tags=["search", "grep", "find", "regex", "pattern", "ripgrep", "rg", "code"],
        timeout=30,
        parallel_safe=True,
    )
