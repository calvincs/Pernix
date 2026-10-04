"""Pernix — Surgical file edit tool with fuzzy matching cascade."""

from __future__ import annotations

import difflib
import logging
import re
from pathlib import Path
from typing import NamedTuple

from config import settings
from core.tools.atomic import TargetBusy, WriteOutcome, atomic_write, target_lock
from core.tools.paths import root_mismatch_hint
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
#
# Each strategy reports what it found, rather than yielding one candidate at a
# time. The generators this replaced did loop over their matches when
# replace_all was set, but every candidate was rebuilt from the untouched
# original lines, so each one carried exactly one replacement and the cascade
# took the first — which is how `replace_all=True` migrated one call site of
# three and reported success (2026-09-08).
# ---------------------------------------------------------------------------


class _Attempt(NamedTuple):
    """What one strategy made of an (old_string, new_string) pair.

    `refusal` is not "no match". It means the strategy found matches it will
    not choose between, and the cascade must stop: falling through to a more
    lenient strategy after an ambiguous precise one widens the guess instead
    of narrowing it.
    """

    content: str | None = None
    count: int = 0
    refusal: str | None = None
    note: str | None = None


_NO_MATCH = _Attempt()


class EditResult(NamedTuple):
    """The cascade's verdict, including the count the caller has to be told."""

    content: str | None
    strategy: str | None
    count: int
    error: str | None = None
    notes: tuple[str, ...] = ()


def _normalize_ws(s: str) -> str:
    """Collapse runs of spaces and tabs, for whitespace-insensitive matching."""
    return re.sub(r"[ \t]+", " ", s)


def _splice(content_lines: list[str], hits: list[tuple[int, int, list[str]]]) -> str:
    """Apply (start, span, replacement_lines) hits in one cursor walk.

    Hits arrive sorted and non-overlapping, and the output is built once from
    a single cursor, so nothing drifts when a replacement is a different
    length from the block it replaces.
    """
    out: list[str] = []
    cursor = 0
    for start, span, new_lines in hits:
        out.extend(content_lines[cursor:start])
        out.extend(new_lines)
        cursor = start + span
    out.extend(content_lines[cursor:])
    return "\n".join(out)


def _resolve(
    content_lines: list[str],
    hits: list[tuple[int, int, list[str]]],
    replace_all: bool,
    label: str,
) -> _Attempt:
    """Turn a line-based strategy's hits into an attempt, refusing to guess."""
    if not hits:
        return _NO_MATCH
    if not replace_all and len(hits) > 1:
        where = ", ".join(str(h[0] + 1) for h in hits[:6])
        more = "" if len(hits) <= 6 else f" (and {len(hits) - 6} more)"
        return _Attempt(
            refusal=(
                f"{len(hits)} {label} matches, at lines {where}{more}. These are "
                f"near-matches, so picking one of them is a guess. Add surrounding "
                f"context to old_string until only the intended occurrence matches, "
                f"or pass replace_all=True to change all {len(hits)}."
            )
        )
    return _Attempt(_splice(content_lines, hits), len(hits))


def _exact_replace(content: str, old: str, new: str, replace_all: bool) -> _Attempt:
    """Strategy 1: Direct string match.

    Character-based rather than line-based, and first-match-wins stays the
    contract when replace_all is off: an exact match is precise, so there is
    nothing to guess between.
    """
    if old not in content:
        return _NO_MATCH
    if replace_all:
        # str.count and str.replace agree on non-overlapping occurrences.
        return _Attempt(content.replace(old, new), content.count(old))
    idx = content.index(old)
    return _Attempt(content[:idx] + new + content[idx + len(old) :], 1)


def _whitespace_normalized_starts(content_lines: list[str], old_lines: list[str]) -> list[int]:
    """Start index of every non-overlapping whitespace-insensitive match."""
    span = len(old_lines)
    if span == 0 or span > len(content_lines):
        return []
    norm_content = [_normalize_ws(line) for line in content_lines]
    norm_old = [_normalize_ws(line) for line in old_lines]

    starts: list[int] = []
    i = 0
    while i <= len(content_lines) - span:
        if norm_content[i : i + span] == norm_old:
            starts.append(i)
            i += span  # a match consumes its own lines; an overlap is not a match
        else:
            i += 1
    return starts


def _whitespace_normalized_replace(content: str, old: str, new: str, replace_all: bool) -> _Attempt:
    """Strategy 2: Collapse whitespace before matching."""
    if _normalize_ws(old) not in _normalize_ws(content):
        return _NO_MATCH

    content_lines = content.split("\n")
    old_lines = old.split("\n")
    new_lines = new.split("\n")
    hits = [(start, len(old_lines), new_lines) for start in _whitespace_normalized_starts(content_lines, old_lines)]
    return _resolve(content_lines, hits, replace_all, "whitespace-normalized")


def _indentation_flexible_starts(content_lines: list[str], old_lines: list[str]) -> list[tuple[int, int]]:
    """(start, block indent) for every non-overlapping indentation-shifted match."""
    span = len(old_lines)
    if span == 0 or span > len(content_lines):
        return []

    indents = [len(line) - len(line.lstrip()) for line in old_lines if line.strip()]
    if not indents:
        return []
    min_indent = min(indents)
    stripped_old = [line[min_indent:] if line.strip() else "" for line in old_lines]

    found: list[tuple[int, int]] = []
    i = 0
    while i <= len(content_lines) - span:
        chunk = content_lines[i : i + span]
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

        if stripped_chunk == stripped_old:
            found.append((i, chunk_indent))
            i += span
        else:
            i += 1
    return found


def _reindent(new_lines: list[str], chunk_indent: int) -> list[str]:
    """Re-hang the replacement at the indent the matched block was found at."""
    new_indents = [len(line) - len(line.lstrip()) for line in new_lines if line.strip()]
    new_min_indent = min(new_indents) if new_indents else 0
    return [(" " * chunk_indent + line[new_min_indent:]) if line.strip() else "" for line in new_lines]


def _indentation_flexible_replace(content: str, old: str, new: str, replace_all: bool) -> _Attempt:
    """Strategy 3: Strip common leading indentation, then match.

    Each match keeps its own indent, so three copies of a block at three
    nesting depths all come back at the depth they were found at.
    """
    old_lines = old.split("\n")
    if not old_lines:
        return _NO_MATCH

    content_lines = content.split("\n")
    new_lines = new.split("\n")
    hits = [
        (start, len(old_lines), _reindent(new_lines, indent))
        for start, indent in _indentation_flexible_starts(content_lines, old_lines)
    ]
    return _resolve(content_lines, hits, replace_all, "indentation-flexible")


def _block_anchor_replace(content: str, old: str, new: str, replace_all: bool) -> _Attempt:
    """Strategy 4: Match first+last lines as anchors, fuzzy-match middle.

    Requires middle-line similarity >= BLOCK_ANCHOR_MIN_SIMILARITY. Below
    that threshold the strategy matches nothing rather than risk a silent
    edit to the wrong block.

    Single-match by definition: it anchors on one first/last line pair and
    scores the middle, so "all the matches" is not a thing it can mean.
    replace_all does not widen it, and rather than honour the flag in silence
    the attempt carries a note saying so. Two blocks that score identically
    are refused — sorting and taking the first is a coin flip dressed up as a
    match.
    """
    old_lines = old.split("\n")
    if len(old_lines) < 3:
        return _NO_MATCH

    first_anchor = old_lines[0].strip()
    last_anchor = old_lines[-1].strip()
    middle_old = [l.strip() for l in old_lines[1:-1]]

    if not first_anchor or not last_anchor:
        return _NO_MATCH

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

    viable = [c for c in candidates if c[2] >= BLOCK_ANCHOR_MIN_SIMILARITY]
    if not viable:
        return _NO_MATCH

    viable.sort(key=lambda c: (-c[2], c[0]))
    best_start, best_end, best_score = viable[0]

    tied = [c for c in viable[1:] if c[0] != best_start and abs(c[2] - best_score) < 1e-9]
    if tied:
        where = ", ".join(str(c[0] + 1) for c in [viable[0], *tied[:5]])
        return _Attempt(
            refusal=(
                f"{1 + len(tied)} blocks match the anchors equally well, at lines {where}. "
                f"Give old_string a line that only the intended block has — an anchor match "
                f"scores the middle, so a tie means the two blocks are indistinguishable to it."
            )
        )

    result_lines = content_lines[:best_start] + new.split("\n") + content_lines[best_end + 1 :]
    note = None
    if replace_all:
        note = (
            "[note: block-anchor matched a single block. It anchors on one first/last "
            "line pair, so replace_all does not widen it — run the edit again if there "
            "are more blocks to change.]"
        )
    return _Attempt("\n".join(result_lines), 1, note=note)


# The cascade: try each strategy in order, take the first that matched.
# Named so we can report which strategy fired (exact is silent; fuzzy is annotated).
REPLACERS = [
    ("exact", _exact_replace),
    ("whitespace-normalized", _whitespace_normalized_replace),
    ("indentation-flexible", _indentation_flexible_replace),
    ("block-anchor-fuzzy", _block_anchor_replace),
]


def _variants_left(content: str, old: str) -> list[int]:
    """1-based lines still holding a near-match of `old` after an exact pass.

    The fuzzy strategies only run when exact matching finds nothing, so a
    single byte-identical occurrence is enough to make every whitespace or
    indentation variant beside it invisible: exact replaces what it matched,
    the cascade stops, and the migration is half done. Run against the
    RESULT, not the original, because what matters is what survived.
    """
    content_lines = content.split("\n")
    old_lines = old.split("\n")
    starts = set(_whitespace_normalized_starts(content_lines, old_lines))
    starts.update(start for start, _ in _indentation_flexible_starts(content_lines, old_lines))
    return sorted(start + 1 for start in starts)


def _apply_edit(content: str, old_string: str, new_string: str, replace_all: bool) -> EditResult:
    """Run the cascade, most precise strategy first.

    Returns the first strategy that matched, with the number of replacements
    it actually made — the count belongs in the caller's result string so a
    refactor can be compared against what it expected. A strategy that
    refuses ends the cascade rather than handing the work to a looser one.
    """
    for name, replacer in REPLACERS:
        attempt = replacer(content, old_string, new_string, replace_all)
        if attempt.refusal:
            return EditResult(None, name, 0, attempt.refusal)
        if attempt.content is None:
            continue

        notes = [attempt.note] if attempt.note else []
        if name == "exact" and replace_all:
            leftover = _variants_left(attempt.content, old_string)
            if leftover:
                where = ", ".join(str(n) for n in leftover[:6])
                more = "" if len(leftover) <= 6 else f" (and {len(leftover) - 6} more)"
                plural = "es were" if len(leftover) != 1 else " was"
                notes.append(
                    f"[warning: {len(leftover)} near-match{plural} NOT changed — line {where}{more}. "
                    f"An exact match existed, so only exact matches were replaced. Edit those "
                    f"lines with an old_string that matches them, or check them by hand.]"
                )
        return EditResult(attempt.content, name, attempt.count, None, tuple(notes))

    return EditResult(None, None, 0)


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
# Reading the target, and checking it did not move under us
# ---------------------------------------------------------------------------


def _read_target(resolved: Path) -> str:
    """Read the file with its line endings intact.

    newline='' so Python does NOT collapse \\r\\n / \\r / \\n — we need the raw
    line endings to detect and preserve the style.
    """
    with open(resolved, "r", errors="replace", newline="") as f:
        return f.read()


def _stale_read_error(resolved: Path, path: str, original: str) -> str | None:
    """Refuse the write if the file no longer holds what we read.

    `target_lock` serializes every editor inside this process, but nothing
    coordinates a shell `sed -i`, a second Pernix process, or a human with an
    editor open. So the last thing an edit does before replacing the file is
    read it back: an outside change that has already landed is caught here
    and refused, leaving the outsider's version in place.

    A change landing between this check and `os.replace` is still lost. That
    residual race needs stronger isolation than an in-process lock and is
    disclosed rather than claimed fixed.
    """
    try:
        current = _read_target(resolved)
    except OSError as e:
        return f"Error: {path} could not be re-read before writing ({e}) — nothing was written."
    if current == original:
        return None
    return (
        f"Error: {path} changed on disk while this edit was being prepared — "
        f"nothing was written, so the other change is still there. "
        f"Call file_read(path='{path}') to see the current content before retrying."
    )


def _mode_note(outcome: WriteOutcome) -> str:
    """Say out loud when the write declined to carry a bit forward."""
    if outcome.dropped_setuid:
        return (
            "\n[note: the setuid bit was dropped — this tool does not re-grant setuid "
            "to content it just wrote. Re-apply with chmod if you meant it.]"
        )
    return ""


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

    The read, the transform and the replace all happen under one lock on the
    canonical target, so two editors in this process serialize instead of
    both reading the same original and both reporting success. The file also
    keeps whatever mode it had. Neither guard reaches outside this process —
    see `_stale_read_error` for what is caught and what is not.
    """
    if old_string == new_string:
        return "No changes made (old_string and new_string are identical — treated as no-op)."

    cap = _max_write_size()
    if len(new_string) > cap:
        return f"Error: new_string exceeds size cap ({len(new_string)} > {cap} bytes)"

    try:
        resolved = _safe_path(path)
    except ValueError as e:
        return f"Error: {e}{root_mismatch_hint(path)}"

    try:
        with target_lock(resolved):
            return _file_edit_locked(resolved, path, old_string, new_string, replace_all, cap)
    except TargetBusy as e:
        return f"Error: {e}"


def _file_edit_locked(
    resolved: Path,
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool,
    cap: int,
) -> str:
    """The read-transform-replace interval, run with the target lock held."""
    if not resolved.exists():
        if not old_string:
            try:
                outcome = atomic_write(resolved, new_string)
                logger.info("file_edit create path=%s bytes=%d mode=%o", resolved, len(new_string), outcome.mode)
                return f"Created new file: {resolved} ({len(new_string)} chars)"
            except Exception as e:
                return f"Error creating file: {e}"
        return f"Error: File not found: {path}{root_mismatch_hint(path)}"

    if not resolved.is_file():
        return f"Error: Not a file: {path}"

    if _is_binary(resolved):
        return f"Error: Refusing to edit binary file: {path}. Use bash for binary edits."

    too_big = _too_large_for_edit(resolved, path)
    if too_big:
        return too_big

    try:
        original = _read_target(resolved)
    except Exception as e:
        return f"Error reading file: {e}"

    eol = _detect_eol(original)
    original_normalized = _normalize_eol(original, eol) if eol != "\n" else original
    old_normalized = _normalize_eol(old_string, eol) if eol != "\n" else old_string
    new_normalized = _normalize_eol(new_string, eol) if eol != "\n" else new_string

    edit = _apply_edit(original_normalized, old_normalized, new_normalized, replace_all)
    strategy = edit.strategy
    result = edit.content

    if edit.error:
        return (
            f"Error: ambiguous {strategy} match in {path} — {edit.error}\n\n"
            f"Nothing was written; the file is exactly as it was."
        )

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
        return (
            f"Error: old_string not found in {path}.{hint}\n\n"
            f"The file may have changed since you last read it. "
            f"Call file_read(path='{path}') to see the current content before retrying."
        )

    if eol != "\n":
        result = result.replace("\n", eol)

    if len(result) > cap:
        return f"Error: edited content exceeds size cap ({len(result)} > {cap} bytes)"

    diff_text = _make_diff(original, result, path)

    stale = _stale_read_error(resolved, path, original)
    if stale:
        return stale

    try:
        written = atomic_write(resolved, result)
    except Exception as e:
        return f"Error writing file: {e}"

    added = sum(1 for l in diff_text.split("\n") if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff_text.split("\n") if l.startswith("-") and not l.startswith("---"))

    logger.info(
        "file_edit path=%s strategy=%s replacements=%d mode=%o +%d/-%d",
        resolved,
        strategy,
        edit.count,
        written.mode,
        added,
        removed,
    )

    strategy_note = ""
    if strategy and strategy != "exact":
        strategy_note = f" [fuzzy: {strategy}]"
    notes = "".join(f"\n{n}" for n in edit.notes)
    plural = "" if edit.count == 1 else "s"

    return (
        f"Edited {resolved}{strategy_note} ({edit.count} replacement{plural}, "
        f"+{added}/-{removed} lines){notes}{_mode_note(written)}\n\n{diff_text}"
    )


def multiedit(path: str, edits: list[dict]) -> str:
    """Apply multiple sequential edits to a single file.

    Each edit sees the result of the previous edit. Order matters. Any
    failure aborts the batch — no changes are written to disk.

    The whole batch runs under one lock on the canonical target, so a
    concurrent file_edit cannot land between the read and the replace and
    have its change erased by the batch.
    """
    if not edits:
        return "Error: No edits provided."

    try:
        resolved = _safe_path(path)
    except ValueError as e:
        return f"Error: {e}{root_mismatch_hint(path)}"

    try:
        with target_lock(resolved):
            return _multiedit_locked(resolved, path, edits)
    except TargetBusy as e:
        return f"Error: {e}"


def _multiedit_locked(resolved: Path, path: str, edits: list[dict]) -> str:
    """The read-transform-replace interval, run with the target lock held."""
    if not resolved.is_file():
        return f"Error: File not found: {path}{root_mismatch_hint(path)}"

    if _is_binary(resolved):
        return f"Error: Refusing to edit binary file: {path}. Use bash for binary edits."

    too_big = _too_large_for_edit(resolved, path)
    if too_big:
        return too_big

    try:
        raw = _read_target(resolved)
    except Exception as e:
        return f"Error reading file: {e}"

    content = raw
    eol = _detect_eol(content)
    if eol != "\n":
        content = _normalize_eol(content, eol)

    original = content
    applied = 0
    replacements = 0
    strategies: list[str] = []
    notes: list[str] = []

    for i, spec in enumerate(edits):
        old_str = spec.get("old_string", "")
        new_str = spec.get("new_string", "")
        replace_all = spec.get("replace_all", False)

        if old_str == new_str:
            continue

        if eol != "\n":
            old_str = _normalize_eol(old_str, eol)
            new_str = _normalize_eol(new_str, eol)

        edit = _apply_edit(content, old_str, new_str, replace_all)
        if edit.error:
            return (
                f"Error: Edit {i + 1} refused — ambiguous {edit.strategy} match: {edit.error} "
                f"{applied}/{len(edits)} edits matched in memory; aborted, no file changes written."
            )
        if edit.content is None:
            return (
                f"Error: Edit {i + 1} failed — old_string not found. "
                f"{applied}/{len(edits)} edits matched in memory; aborted, no file changes written.\n\n"
                f"The file may have changed since you last read it. "
                f"Call file_read(path='{path}') to see the current content before retrying."
            )
        content = edit.content
        applied += 1
        replacements += edit.count
        if edit.strategy:
            strategies.append(edit.strategy)
        notes.extend(f"[edit {i + 1}] {n}" for n in edit.notes)

    if content == original:
        return "No changes made (all edits were no-ops)."

    cap = _max_write_size()
    if len(content) > cap:
        return f"Error: edited content exceeds size cap ({len(content)} > {cap} bytes)"

    final = content.replace("\n", eol) if eol != "\n" else content

    stale = _stale_read_error(resolved, path, raw)
    if stale:
        return stale

    try:
        written = atomic_write(resolved, final)
    except Exception as e:
        return f"Error writing file: {e}"

    diff_text = _make_diff(original, content, path)
    added = sum(1 for l in diff_text.split("\n") if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff_text.split("\n") if l.startswith("-") and not l.startswith("---"))

    logger.info(
        "multiedit path=%s edits=%d replacements=%d strategies=%s mode=%o +%d/-%d",
        resolved,
        applied,
        replacements,
        ",".join(strategies) or "-",
        written.mode,
        added,
        removed,
    )

    fuzzy_used = [s for s in strategies if s != "exact"]
    strategy_note = f" [fuzzy: {','.join(sorted(set(fuzzy_used)))}]" if fuzzy_used else ""
    plural = "" if replacements == 1 else "s"
    note_text = "".join(f"\n{n}" for n in notes)

    return (
        f"Applied {applied}/{len(edits)} edits ({replacements} replacement{plural}) "
        f"to {path}{strategy_note}: +{added}/-{removed} lines{note_text}{_mode_note(written)}\n\n{diff_text}"
    )


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
            "and indentation differences. Returns the number of replacements made and a unified diff. "
            "Compare that count against what you expected. When a fuzzy match is ambiguous — several "
            "near-matches and replace_all off, or two blocks the anchors cannot tell apart — the edit is "
            "refused and the file left alone; add context to old_string or set replace_all. "
            "Idempotent: if old_string == new_string the call is a no-op success, not an error."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path within workspace"},
                "old_string": {"type": "string", "description": "Text to find in the file (exact or fuzzy match)"},
                "new_string": {"type": "string", "description": "Text to replace it with"},
                "replace_all": {
                    "type": "boolean",
                    "description": (
                        "Replace every non-overlapping occurrence, fuzzy matches included. "
                        "The result says how many. Default: false"
                    ),
                },
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
