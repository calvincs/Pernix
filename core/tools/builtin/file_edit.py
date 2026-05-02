"""Pernix — Surgical file edit tool with fuzzy matching cascade."""

from __future__ import annotations

import difflib
import fcntl
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Generator

from config import settings
from core.tools.paths import safe_write_path as _safe_path

logger = logging.getLogger("pernix.tools.file_edit")


# Minimum middle-line similarity required for the fuzzy block-anchor
# strategy to accept a match. Below this, the edit is rejected rather
# than silently applied to the wrong block.
BLOCK_ANCHOR_MIN_SIMILARITY = 0.6

# Upper bound on the content size we'll write or edit in a single call.
# Mirrors the bash RLIMIT_FSIZE of 100 MB.
MAX_WRITE_SIZE = 100 * 1024 * 1024

# Upper bound on the size of a file we'll pull fully into memory for
# string-match editing. Beyond this the agent should pre-grep to find
# the change target and/or patch via bash (sed/awk) — not because we
# technically can't, but because whole-file fuzzy match over hundreds
# of megabytes is slow and rarely what the user actually wants.
MAX_EDIT_READ_SIZE = 5 * 1024 * 1024


# ---------------------------------------------------------------------------
# Levenshtein distance for fuzzy matching
# ---------------------------------------------------------------------------

# Hard cap on inputs to Levenshtein. Above this we report "completely
# different" rather than spending O(n*m) time and blowing the stack on
# Python's default recursion limit. Fuzzy matching is only meaningful
# over short snippets anyway.
_LEV_MAX_LEN = 4096


def _levenshtein(a: str, b: str) -> int:
    """Compute Levenshtein edit distance between two strings.

    Iterative (no recursion), with a hard length cap that short-circuits
    to "maximally different" for pathological inputs.
    """
    # Ensure `b` is the shorter string — shrinks inner loop, matches
    # the classic single-row DP layout.
    if len(a) < len(b):
        a, b = b, a
    if not b:
        return len(a)
    if len(a) > _LEV_MAX_LEN or len(b) > _LEV_MAX_LEN:
        # Treat as fully dissimilar; callers compute similarity as
        # 1 - dist/max_len which will collapse to ~0.
        return max(len(a), len(b))

    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            cost = 0 if ca == cb else 1
            curr.append(min(curr[j] + 1, prev[j + 1] + 1, prev[j] + cost))
        prev = curr
    return prev[-1]


def _similarity(a: str, b: str) -> float:
    """Return 0.0–1.0 similarity based on Levenshtein distance."""
    if not a and not b:
        return 1.0
    max_len = max(len(a), len(b))
    if max_len == 0:
        return 1.0
    return 1.0 - _levenshtein(a, b) / max_len


# ---------------------------------------------------------------------------
# Binary sniffing (duplicated from core_tools to avoid import cycle)
# ---------------------------------------------------------------------------


def _is_binary(resolved: Path) -> bool:
    """Check if file is binary by sampling first 512 bytes for null bytes."""
    try:
        with open(resolved, "rb") as f:
            chunk = f.read(512)
        return b"\x00" in chunk
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Line-ending detection
# ---------------------------------------------------------------------------


def _detect_eol(text: str) -> str:
    """Return the dominant line ending: '\\r\\n', '\\r', or '\\n'."""
    if "\r\n" in text:
        return "\r\n"
    # Lone CR (classic Mac-style): has \r but no \n at all.
    if "\r" in text and "\n" not in text:
        return "\r"
    return "\n"


def _normalize_eol(text: str, eol: str) -> str:
    """Collapse the given EOL to '\\n' for matching."""
    if eol == "\n":
        return text
    return text.replace(eol, "\n")


# ---------------------------------------------------------------------------
# Replacer strategies (cascade from most precise to most lenient)
# ---------------------------------------------------------------------------


def _exact_replace(content: str, old: str, new: str, replace_all: bool) -> Generator[str, None, None]:
    """Strategy 1: Direct string match."""
    if old in content:
        if replace_all:
            yield content.replace(old, new)
        else:
            idx = content.index(old)
            yield content[:idx] + new + content[idx + len(old) :]


def _whitespace_normalized_replace(content: str, old: str, new: str, replace_all: bool) -> Generator[str, None, None]:
    """Strategy 2: Collapse whitespace before matching."""

    def normalize(s: str) -> str:
        return re.sub(r"[ \t]+", " ", s)

    norm_content = normalize(content)
    norm_old = normalize(old)

    if norm_old not in norm_content:
        return

    content_lines = content.split("\n")
    old_lines = old.split("\n")
    norm_old_lines = [normalize(l) for l in old_lines]

    for i in range(len(content_lines) - len(old_lines) + 1):
        chunk = content_lines[i : i + len(old_lines)]
        norm_chunk = [normalize(l) for l in chunk]
        if norm_chunk == norm_old_lines:
            new_lines = new.split("\n")
            result_lines = content_lines[:i] + new_lines + content_lines[i + len(old_lines) :]
            yield "\n".join(result_lines)
            if not replace_all:
                return


def _indentation_flexible_replace(content: str, old: str, new: str, replace_all: bool) -> Generator[str, None, None]:
    """Strategy 3: Strip common leading indentation, then match."""
    old_lines = old.split("\n")
    if not old_lines:
        return

    indents = []
    for line in old_lines:
        if line.strip():
            indents.append(len(line) - len(line.lstrip()))
    if not indents:
        return
    min_indent = min(indents)

    stripped_old_lines = []
    for line in old_lines:
        if line.strip():
            stripped_old_lines.append(line[min_indent:])
        else:
            stripped_old_lines.append("")

    content_lines = content.split("\n")

    for i in range(len(content_lines) - len(old_lines) + 1):
        chunk = content_lines[i : i + len(old_lines)]
        chunk_indent = 0
        for line in chunk:
            if line.strip():
                chunk_indent = len(line) - len(line.lstrip())
                break

        stripped_chunk = []
        for line in chunk:
            if line.strip():
                stripped_chunk.append(line[chunk_indent:] if len(line) >= chunk_indent else line)
            else:
                stripped_chunk.append("")

        if stripped_chunk == stripped_old_lines:
            new_lines = new.split("\n")
            new_indents = [len(l) - len(l.lstrip()) for l in new_lines if l.strip()]
            new_min_indent = min(new_indents) if new_indents else 0

            reindented = []
            for line in new_lines:
                if line.strip():
                    reindented.append(" " * chunk_indent + line[new_min_indent:])
                else:
                    reindented.append("")

            result_lines = content_lines[:i] + reindented + content_lines[i + len(old_lines) :]
            yield "\n".join(result_lines)
            if not replace_all:
                return


def _block_anchor_replace(content: str, old: str, new: str, replace_all: bool) -> Generator[str, None, None]:
    """Strategy 4: Match first+last lines as anchors, fuzzy-match middle.

    Requires middle-line similarity >= BLOCK_ANCHOR_MIN_SIMILARITY. Below
    that threshold the strategy yields nothing rather than risk a silent
    edit to the wrong block.
    """
    old_lines = old.split("\n")
    if len(old_lines) < 3:
        return

    first_anchor = old_lines[0].strip()
    last_anchor = old_lines[-1].strip()
    middle_old = [l.strip() for l in old_lines[1:-1]]

    if not first_anchor or not last_anchor:
        return

    content_lines = content.split("\n")
    candidates: list[tuple[int, int, float]] = []

    for i, line in enumerate(content_lines):
        if line.strip() != first_anchor:
            continue
        max_end = min(i + len(old_lines) * 2, len(content_lines))
        for j in range(i + 2, max_end):
            if j >= len(content_lines):
                break
            if content_lines[j].strip() != last_anchor:
                continue
            middle_content = [l.strip() for l in content_lines[i + 1 : j]]
            if not middle_old:
                score = 1.0 if not middle_content else 0.5
            else:
                scores = []
                for k, mol in enumerate(middle_old):
                    if k < len(middle_content):
                        scores.append(_similarity(mol, middle_content[k]))
                    else:
                        scores.append(0.0)
                len_penalty = 1.0 - abs(len(middle_old) - len(middle_content)) / max(
                    len(middle_old), len(middle_content), 1
                )
                score = (sum(scores) / max(len(scores), 1)) * len_penalty

            candidates.append((i, j, score))

    if not candidates:
        return

    candidates.sort(key=lambda x: -x[2])
    best_start, best_end, best_score = candidates[0]

    if best_score < BLOCK_ANCHOR_MIN_SIMILARITY:
        return

    new_lines = new.split("\n")
    result_lines = content_lines[:best_start] + new_lines + content_lines[best_end + 1 :]
    yield "\n".join(result_lines)


# The cascade: try each strategy in order, return first success.
# Named so we can report which strategy fired (exact is silent; fuzzy is annotated).
REPLACERS = [
    ("exact", _exact_replace),
    ("whitespace-normalized", _whitespace_normalized_replace),
    ("indentation-flexible", _indentation_flexible_replace),
    ("block-anchor-fuzzy", _block_anchor_replace),
]


def _apply_edit(content: str, old_string: str, new_string: str, replace_all: bool) -> tuple[str | None, str | None]:
    """Try each replacer strategy in cascade.

    Returns (new_content, strategy_name) or (None, None) if no strategy matched.
    """
    for name, replacer in REPLACERS:
        for result in replacer(content, old_string, new_string, replace_all):
            return result, name
    return None, None


# ---------------------------------------------------------------------------
# Diff generation
# ---------------------------------------------------------------------------


def _make_diff(original: str, modified: str, filepath: str) -> str:
    """Generate a unified diff string."""
    orig_lines = original.splitlines(keepends=True)
    mod_lines = modified.splitlines(keepends=True)
    diff = difflib.unified_diff(
        orig_lines,
        mod_lines,
        fromfile=f"a/{filepath}",
        tofile=f"b/{filepath}",
        lineterm="",
    )
    return "".join(diff)


# ---------------------------------------------------------------------------
# Atomic write helper
# ---------------------------------------------------------------------------


def _atomic_write(resolved: Path, content: str) -> None:
    """Write content to resolved path atomically (tempfile + fsync + rename)."""
    resolved.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(resolved.parent), suffix=".tmp", prefix=f".{resolved.name}.")
    try:
        # newline='' disables Python's write-side newline translation so
        # the caller's line endings are written verbatim.
        with os.fdopen(fd, "w", newline="") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(resolved))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _max_write_size() -> int:
    """Resolve the write-size cap from settings, falling back to the module default."""
    return int(getattr(settings, "max_file_write_size", MAX_WRITE_SIZE) or MAX_WRITE_SIZE)


def _max_edit_read_size() -> int:
    """Resolve the edit read-size cap from settings."""
    return int(getattr(settings, "max_edit_read_size", MAX_EDIT_READ_SIZE) or MAX_EDIT_READ_SIZE)


def _too_large_for_edit(resolved: Path, path: str) -> str | None:
    """Return an error string if the file is too big to load for whole-file edit."""
    try:
        size = resolved.stat().st_size
    except OSError:
        return None
    cap = _max_edit_read_size()
    if size > cap:
        return (
            f"Error: {path} is {size:,} bytes, above the {cap:,}-byte whole-file "
            f"edit cap. Use grep + a precise old_string, or patch via bash "
            f"(sed/awk) — fuzzy match over files this large is slow and "
            f"error-prone."
        )
    return None


# ---------------------------------------------------------------------------
# Main tool functions
# ---------------------------------------------------------------------------


def file_edit(path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    """Edit a file by finding and replacing text.

    Uses a cascade of matching strategies from exact to fuzzy:
    1. Exact string match
    2. Whitespace-normalized match
    3. Indentation-flexible match
    4. Block-anchor match (first/last line anchors + fuzzy middle).
    """
    if old_string == new_string:
        return "No changes made (old_string and new_string are identical — treated as no-op)."

    cap = _max_write_size()
    if len(new_string) > cap:
        return f"Error: new_string exceeds size cap ({len(new_string)} > {cap} bytes)"

    try:
        resolved = _safe_path(path)
    except ValueError as e:
        return f"Error: {e}"

    if not resolved.exists():
        if not old_string:
            try:
                _atomic_write(resolved, new_string)
                logger.info("file_edit create path=%s bytes=%d", resolved, len(new_string))
                return f"Created new file: {resolved} ({len(new_string)} chars)"
            except Exception as e:
                return f"Error creating file: {e}"
        return f"Error: File not found: {path}"

    if not resolved.is_file():
        return f"Error: Not a file: {path}"

    if _is_binary(resolved):
        return f"Error: Refusing to edit binary file: {path}. Use bash for binary edits."

    too_big = _too_large_for_edit(resolved, path)
    if too_big:
        return too_big

    try:
        # Read with newline='' so Python does NOT collapse \r\n / \r / \n —
        # we need the raw line endings to detect and preserve the style.
        with open(resolved, "r", errors="replace", newline="") as f:
            original = f.read()
    except Exception as e:
        return f"Error reading file: {e}"

    eol = _detect_eol(original)
    original_normalized = _normalize_eol(original, eol) if eol != "\n" else original
    old_normalized = _normalize_eol(old_string, eol) if eol != "\n" else old_string
    new_normalized = _normalize_eol(new_string, eol) if eol != "\n" else new_string

    result, strategy = _apply_edit(original_normalized, old_normalized, new_normalized, replace_all)

    if result is None:
        lines = original_normalized.split("\n")
        old_first_line = old_normalized.split("\n")[0].strip() if old_normalized else ""
        candidates = []
        for i, line in enumerate(lines):
            if old_first_line and _similarity(line.strip(), old_first_line) > 0.6:
                candidates.append(f"  line {i + 1}: {line.rstrip()[:80]}")
        hint = ""
        if candidates:
            hint = "\n\nPossible matches:\n" + "\n".join(candidates[:5])
        return f"Error: old_string not found in {path}.{hint}"

    if eol != "\n":
        result = result.replace("\n", eol)

    if len(result) > cap:
        return f"Error: edited content exceeds size cap ({len(result)} > {cap} bytes)"

    diff_text = _make_diff(original, result, path)

    try:
        _atomic_write(resolved, result)
    except Exception as e:
        return f"Error writing file: {e}"

    added = sum(1 for l in diff_text.split("\n") if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff_text.split("\n") if l.startswith("-") and not l.startswith("---"))

    logger.info("file_edit path=%s strategy=%s +%d/-%d", resolved, strategy, added, removed)

    strategy_note = ""
    if strategy and strategy != "exact":
        strategy_note = f" [fuzzy: {strategy}]"

    return f"Edited {resolved}{strategy_note} (+{added}/-{removed} lines)\n\n{diff_text}"


def multiedit(path: str, edits: list[dict]) -> str:
    """Apply multiple sequential edits to a single file.

    Each edit sees the result of the previous edit. Order matters. Any
    failure aborts the batch — no changes are written to disk.
    """
    if not edits:
        return "Error: No edits provided."

    try:
        resolved = _safe_path(path)
    except ValueError as e:
        return f"Error: {e}"

    if not resolved.is_file():
        return f"Error: File not found: {path}"

    if _is_binary(resolved):
        return f"Error: Refusing to edit binary file: {path}. Use bash for binary edits."

    too_big = _too_large_for_edit(resolved, path)
    if too_big:
        return too_big

    try:
        with open(resolved, "r", errors="replace", newline="") as fh:
            content = fh.read()
    except Exception as e:
        return f"Error reading file: {e}"

    eol = _detect_eol(content)
    if eol != "\n":
        content = _normalize_eol(content, eol)

    original = content
    applied = 0
    strategies: list[str] = []

    for i, edit in enumerate(edits):
        old_str = edit.get("old_string", "")
        new_str = edit.get("new_string", "")
        replace_all = edit.get("replace_all", False)

        if old_str == new_str:
            continue

        if eol != "\n":
            old_str = _normalize_eol(old_str, eol)
            new_str = _normalize_eol(new_str, eol)

        result, strategy = _apply_edit(content, old_str, new_str, replace_all)
        if result is None:
            return (
                f"Error: Edit {i + 1} failed — old_string not found. "
                f"{applied}/{len(edits)} edits matched in memory; aborted, no file changes written."
            )
        content = result
        applied += 1
        if strategy:
            strategies.append(strategy)

    if content == original:
        return "No changes made (all edits were no-ops)."

    cap = _max_write_size()
    if len(content) > cap:
        return f"Error: edited content exceeds size cap ({len(content)} > {cap} bytes)"

    final = content.replace("\n", eol) if eol != "\n" else content

    try:
        _atomic_write(resolved, final)
    except Exception as e:
        return f"Error writing file: {e}"

    diff_text = _make_diff(original, content, path)
    added = sum(1 for l in diff_text.split("\n") if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff_text.split("\n") if l.startswith("-") and not l.startswith("---"))

    logger.info(
        "multiedit path=%s edits=%d strategies=%s +%d/-%d",
        resolved,
        applied,
        ",".join(strategies) or "-",
        added,
        removed,
    )

    fuzzy_used = [s for s in strategies if s != "exact"]
    strategy_note = f" [fuzzy: {','.join(sorted(set(fuzzy_used)))}]" if fuzzy_used else ""

    return f"Applied {applied}/{len(edits)} edits to {path}{strategy_note}: +{added}/-{removed} lines\n\n{diff_text}"


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(reg) -> None:
    """Register file edit tools."""
    reg.register(
        name="file_edit",
        func=file_edit,
        description=(
            "Edit a file by finding and replacing text. More efficient than file_write for small changes — "
            "only specify the text to find and its replacement. Uses fuzzy matching to handle whitespace "
            "and indentation differences. Returns a unified diff of the change. "
            "Idempotent: if old_string == new_string the call is a no-op success, not an error."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path within workspace"},
                "old_string": {"type": "string", "description": "Text to find in the file (exact or fuzzy match)"},
                "new_string": {"type": "string", "description": "Text to replace it with"},
                "replace_all": {"type": "boolean", "description": "Replace all occurrences. Default: false"},
            },
            "required": ["path", "old_string", "new_string"],
        },
        category="core",
        tags=["edit", "modify", "replace", "change", "update", "patch", "fix", "refactor"],
        timeout=30,
        parallel_safe=False,
        safety_level="safe",
    )

    reg.register(
        name="multiedit",
        func=multiedit,
        description=(
            "Apply multiple sequential edits to a single file in one call. "
            "Each edit sees the result of the previous one. More efficient than "
            "multiple file_edit calls for batch changes to the same file."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path within workspace"},
                "edits": {
                    "type": "array",
                    "description": "Array of edit operations to apply sequentially",
                    "items": {
                        "type": "object",
                        "properties": {
                            "old_string": {"type": "string", "description": "Text to find"},
                            "new_string": {"type": "string", "description": "Text to replace with"},
                            "replace_all": {"type": "boolean", "description": "Replace all occurrences"},
                        },
                        "required": ["old_string", "new_string"],
                    },
                },
            },
            "required": ["path", "edits"],
        },
        category="core",
        tags=["edit", "modify", "batch", "multi", "replace", "change"],
        timeout=60,
        parallel_safe=False,
        safety_level="safe",
    )
