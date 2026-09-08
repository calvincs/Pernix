"""Pernix — Grep tool: structured ripgrep/grep wrapper for codebase search.

Every search here is bounded, and that is fine. What is not fine is a bounded
search that reads like an exhaustive one: the audit (3.2.1 H16) found a file
over the 1 MiB size cap returning the literal "No matches found." — the same
sentence a genuinely empty workspace returns — and a footer reading
"[200 matches]" for a file that held 500, stating the per-file cap as a count.

So each result now carries its scope, the caps that applied to it, whether the
backend errored, and returned/omitted counts that are either exact or say
"unknown". Nothing is measured twice to produce them: the per-file floor comes
from the lines already in hand, and the size-skip count is genuinely not
knowable without a second scan, so it is reported as unknown rather than
guessed at.

Each result also names the root it ran against ([root: ...]). Since the shared
relative-path contract landed (3.2 H07) `impl` may be the space home or the
global workspace, and a count is only honest if the reader can tell which tree
it counted.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from collections import Counter

from core.tools.paths import owning_root, resolve_workspace_path, root_mismatch_hint
from core.tools.paths import workspace_home as _default_root
from core.tools.truncation import write_artifact

logger = logging.getLogger("pernix.tools.grep")

MAX_MATCHES = 400  # matching lines rendered inline
MAX_LINE_LENGTH = 500
# Hard ceiling on lines held in memory (and therefore on what the artifact can
# contain). Above it the true total is still reported, along with how many hits
# were never captured at all — the old code quoted the total beside a pointer
# to a file that only held the first 5,000.
MAX_CAPTURED = 5000
PER_FILE_MAX = 200  # ripgrep --max-count
MAX_FILESIZE = "1M"  # ripgrep --max-filesize


def _find_rg() -> str | None:
    """Find ripgrep binary, return path or None."""
    return shutil.which("rg")


def _caps_line(scope: str, backend: str) -> str:
    """The bounds this search ran under, stated whether or not they bit.

    Stated on empty results too — that is the whole point. "No matches found."
    with no mention of a 1 MiB skip is indistinguishable from a clean sweep of
    every byte in the tree.
    """
    if backend == "rg":
        caps = (
            f"{PER_FILE_MAX} matches/file, files over {MAX_FILESIZE} skipped "
            f"(skipped count unknown — not measured), lines clipped at {MAX_LINE_LENGTH} chars, "
            f"{MAX_CAPTURED:,}-hit capture ceiling, .git/__pycache__/node_modules excluded"
        )
    else:
        caps = (
            f"lines clipped at {MAX_LINE_LENGTH} chars, {MAX_CAPTURED:,}-hit capture ceiling, "
            f".git/__pycache__/node_modules excluded (ripgrep not installed — no per-file or "
            f"file-size cap applied)"
        )
    return f"[scope: {scope} | caps: {caps}]"


def _per_file_floor(lines: list[str]) -> int:
    """How many files came back with exactly the per-file cap's worth of hits.

    Those files hold at least that many and possibly far more, which makes the
    total a floor rather than a count. Derived from the lines already read, so
    honesty costs no extra work.
    """
    counts = Counter(line.split(":", 1)[0] for line in lines if ":" in line)
    return sum(1 for n in counts.values() if n >= PER_FILE_MAX)


def grep(pattern: str, path: str = "", include: str = "") -> str:
    """Search files for a regex pattern using ripgrep (or grep fallback).

    Returns structured output: file:line_number:content
    """
    if not pattern or not pattern.strip():
        return "Error: pattern is required"

    # One relative-path root for every tool (2026-09-08): `impl` names the same
    # directory here that it names to file_read and to bash's cwd. grep used to
    # resolve against the global workspace while a space session edited and
    # tested inside its own home, so a search could report confidently on a
    # different copy of the project than the one being worked on.
    if path:
        try:
            search_path = resolve_workspace_path(path)
        except ValueError as e:
            return f"Error: {e}{root_mismatch_hint(path)}"
    else:
        search_path = _default_root()
    root = owning_root(search_path)
    # Two separate honesty obligations, kept as two separate lines: which tree
    # this search ran against (H07) and the bounds it ran under (H16).
    root_line = f"[root: {root}]"

    if not search_path.exists():
        return f"Error: Path not found: {search_path}{root_mismatch_hint(path)}"

    scope = str(path or ".") + (f" (include={include})" if include else "")

    rg = _find_rg()
    if rg:
        args = [
            rg,
            "-n",
            "--no-heading",
            "--hidden",
            "--no-messages",
            "--color=never",
            f"--max-count={PER_FILE_MAX}",  # per-file limit
            f"--max-filesize={MAX_FILESIZE}",  # skip large files
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

    caps = _caps_line(scope, "rg" if rg else "grep")

    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(root),
        )

        stderr = (result.stderr or "").strip()
        partial_error = ""
        if result.returncode == 2:
            # Exit 2 is ripgrep's error status, and it was whitelisted beside
            # 0 and 1 — so a pattern that failed to COMPILE returned an empty
            # result set and an audit could conclude the symbol was absent.
            if not result.stdout or _is_pattern_error(stderr):
                detail = stderr[:400] or "search backend reported an error with no detail"
                return f"Error: search failed — the pattern was not run as written.\n{detail}\n{caps}"
            # Results AND errors: some of the tree was searched, some was not.
            partial_error = stderr[:400]
        elif result.returncode not in (0, 1) and not result.stdout:
            return f"Error: Search failed (exit {result.returncode}): {stderr[:200]}\n{caps}"

        lines = result.stdout.splitlines()
        total = len(lines)
        if total == 0:
            head = "No matches found."
            if partial_error:
                head = (
                    "No matches found in what was searched — but the search was PARTIAL, so "
                    "coverage is incomplete and this is not evidence of absence."
                    f"\n[backend error] {partial_error}"
                )
            return f"{root_line}\n{head}\n{caps}"

        captured = min(total, MAX_CAPTURED)
        lines = lines[:MAX_CAPTURED]

        # Relative to the effective root, so a path copied out of these results
        # resolves back to the file it came from.
        root_str = str(root) + "/"

        def _render(raw: list[str]) -> list[str]:
            out = []
            for line in raw:
                if len(line) > MAX_LINE_LENGTH:
                    line = line[:MAX_LINE_LENGTH] + "..."
                out.append(line.replace(root_str, ""))
            return out

        shown = _render(lines[:MAX_MATCHES])
        result_text = "\n".join(shown)

        capped_files = _per_file_floor(lines)
        floor = " at least" if capped_files else ""
        parts = [f"[{floor} {total:,} matching line(s)".replace("[ ", "[")]
        parts.append(f"showing {len(shown):,}")
        if captured > len(shown):
            parts.append(f"{captured - len(shown):,} not shown but captured")
        if total > captured:
            parts.append(f"{total - captured:,} never captured ({MAX_CAPTURED:,}-hit capture ceiling)")
        footer = "; ".join(parts) + "]"
        if capped_files:
            footer += (
                f"\n[{capped_files} file(s) returned the full per-file cap of {PER_FILE_MAX} — "
                f"they hold at least that many, so the total above is a FLOOR, not a count]"
            )
        if partial_error:
            footer += (
                "\n[PARTIAL: the backend reported errors, so coverage is incomplete and an "
                f"absence here is not evidence of absence]\n[backend error] {partial_error}"
            )

        if captured > len(shown):
            # Persist unconditionally once anything is held back. The old code
            # routed this through truncate_output, which only persists past
            # MAX_OUTPUT — so 401 short hits produced no file, no pointer, and
            # no route to the 401st.
            full_text = f"{root_line}\n" + "\n".join(_render(lines)) + f"\n\n{footer}\n{caps}"
            saved = write_artifact(full_text, "grep")
            if saved:
                footer += (
                    f"\nFull results saved to: {saved}\n"
                    f'Use file_read(path="{saved}", offset=<line>, limit=<count>) to view all.'
                )
            else:
                footer += "\n[the full result set could not be persisted — narrow the pattern or the path]"

        return f"{root_line}\n{result_text}\n\n{footer}\n{caps}"

    except subprocess.TimeoutExpired:
        return (
            "Error: Search timed out after 30s — this is a PARTIAL result with no hits "
            f"reported, not an absence.\n{caps}"
        )
    except Exception as e:
        return f"Error: {e}\n{caps}"


def _is_pattern_error(stderr: str) -> bool:
    """Whether the backend rejected the pattern itself rather than a file.

    A pattern error means nothing was searched; a file error means part of the
    tree was. Conflating them is how an uncompiled regex read as a clean sweep.
    """
    low = stderr.lower()
    return any(s in low for s in ("regex parse error", "invalid regex", "unclosed", "syntax error"))


def register(reg) -> None:
    """Register grep tool."""
    reg.register(
        name="grep",
        func=grep,
        description=(
            "Search file contents for a regex pattern. Returns file:line:content matches. "
            "Uses ripgrep if available. Every result states its scope, the caps that applied "
            "(per-file, file-size, line-length, hit ceiling) and exact returned/omitted counts; "
            "a backend error is reported as a PARTIAL search, never as an empty one."
        ),
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern to search for"},
                "path": {
                    "type": "string",
                    "description": (
                        "Directory or file to search. A relative path resolves the same way it "
                        "does for file_read and bash. Default: your working root (shown as "
                        "[root: ...] in the result)."
                    ),
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
