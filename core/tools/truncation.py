"""Pernix — Acquisition evidence and the previews rendered from it.

Two different jobs live here, and the audit (3.2.1 H15) found them fused.

*Acquisition* is what a tool actually managed to read: bytes off a pipe, bytes
off a socket, characters out of a DOM. It is bounded — the caps are deliberate
and stay — but whatever it captured is EVIDENCE, and evidence is written to
disk before anything reshapes it.

*Presentation* is the string the model reads: repeated-line collapse, long-line
clipping, a 50 KB head. It is a rendering, and a rendering is not a record.

Until this module grew `write_artifact`, `truncate_output` was handed the
already-collapsed, already-clipped string and dutifully persisted THAT as the
"full output", under a header quoting the clipped size as the source total. A
build whose decisive error landed after 5 MiB had no surviving copy of it and
nothing in the transcript said so.

Offsets in every continuation pointer are 0-BASED LINE NUMBERS, the same unit
`file_read(offset=...)` has always used. Previews are cut at a line boundary
precisely so that unit stays exact: a preview showing K complete lines advises
`offset=K`, which starts at the very next line with nothing skipped and
nothing repeated. The one case a line boundary cannot serve — a single line
longer than the whole preview budget — is called out by name instead of being
papered over with an offset that would skip it.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path

logger = logging.getLogger("pernix.tools.truncation")

MAX_OUTPUT = 50_000  # 50KB preview cap
TOOL_OUTPUT_DIR = Path("data/.tool_output")
# 24h, not 1h: the truncation pointer (`file_read(path=...)`) is quoted in the
# transcript and can be followed many turns later — a long-running goal or a
# session resumed after a break would otherwise find a dead path. Matches the
# lifetime callers already assume from kernel-bound payload pointers, which
# live as long as the session's kernel state.
CLEANUP_MAX_AGE_SECS = 86_400  # 24 hours

# Sidecar carrying an artifact's acquisition metadata. Kept beside the bytes
# rather than inside them so the artifact stays byte-exact evidence: an RLM
# pass, a diff, or a checksum over it must see what the source emitted and
# nothing the harness added.
META_SUFFIX = ".meta.json"


def _ensure_output_dir() -> Path:
    TOOL_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return TOOL_OUTPUT_DIR


def _cleanup_stale_files() -> None:
    """Remove tool output files older than CLEANUP_MAX_AGE_SECS."""
    try:
        if not TOOL_OUTPUT_DIR.exists():
            return
        cutoff = time.time() - CLEANUP_MAX_AGE_SECS
        for f in TOOL_OUTPUT_DIR.iterdir():
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
    except Exception as e:
        logger.debug("Cleanup error: %s", e)


# ---------------------------------------------------------------------------
# Acquisition metadata — what was captured, of how much, and why it stopped
# ---------------------------------------------------------------------------


def acquisition_meta(
    *,
    source: str,
    captured: int,
    source_total: int | None,
    unit: str = "bytes",
    truncation_reason: str = "",
    artifact: str = "",
) -> dict:
    """One acquisition's completeness record.

    ``source_total=None`` means genuinely unknown — a chunked HTTP response
    with no content-length, a stream still being written. Unknown is a real
    answer and is reported as such; it must never be back-filled with the
    captured size, which is the exact substitution that made the audit's
    artifact header claim 5,242,937 chars for an 11,400,026 char source.

    ``unit`` is stated because bytes and characters diverge on any non-ASCII
    source and the reader cannot tell which one a bare number meant.
    """
    complete = source_total is not None and captured >= source_total
    return {
        "source": source,
        "unit": unit,
        "captured": int(captured),
        "source_total": None if source_total is None else int(source_total),
        "source_complete": bool(complete),
        "truncation_reason": "" if complete else truncation_reason,
        "artifact": artifact,
    }


def acquisition_note(meta: dict | None) -> str:
    """The one-line statement a model reads. Empty when nothing was lost.

    A complete acquisition earns no commentary — the point is to make the
    INCOMPLETE case impossible to miss, not to decorate every tool result.
    """
    if not meta or meta.get("source_complete", True):
        return ""
    unit = meta.get("unit", "bytes")
    captured = int(meta.get("captured") or 0)
    total = meta.get("source_total")
    if total is None:
        size = f"captured {captured:,} {unit}; source total unknown"
    else:
        size = f"captured {captured:,} of {int(total):,} {unit}"
    reason = meta.get("truncation_reason") or "an acquisition cap"
    line = (
        f"[INCOMPLETE ACQUISITION: {meta.get('source', 'source')} — {size}; "
        f"source_complete=false, stopped at {reason}"
    )
    if meta.get("artifact"):
        line += f". Raw evidence (pre-collapse, pre-preview): {meta['artifact']}"
    return line + "]"


def evidence_pointer(meta: dict | None) -> str:
    """Where the raw bytes of a COMPLETE acquisition live, when a copy was kept.

    Completeness of acquisition is not completeness of presentation. Repeated
    line collapse turned 2000 identical warnings into 4, and truncation shows
    a 50 KB head — so a whole capture can still reach the model as a summary
    of itself. When a durable copy exists, name it.
    """
    if not meta or not meta.get("artifact") or not meta.get("source_complete", True):
        return ""
    return (
        f"[raw evidence (pre-collapse, pre-preview): {meta.get('source', 'source')} — "
        f"{int(meta.get('captured') or 0):,} {meta.get('unit', 'bytes')}, complete: {meta['artifact']}]"
    )


def acquisition_notes(metas: list[dict] | None) -> str:
    """One line per acquisition worth commenting on, newline joined.

    Incomplete acquisitions always speak. Complete ones speak only when a
    durable copy exists that the preview does not fully contain.
    """
    lines = [n for n in ((acquisition_note(m) or evidence_pointer(m)) for m in (metas or [])) if n]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Artifacts — the durable copy of what was acquired
# ---------------------------------------------------------------------------


def new_artifact_path(tool_id: str, suffix: str = ".txt") -> Path:
    """A collision-resistant artifact path.

    The old `{tool}_{ms}.txt` name collided whenever two parallel calls of the
    same tool landed in the same millisecond — the loser's evidence was
    silently overwritten by the winner's.
    """
    _cleanup_stale_files()
    return _ensure_output_dir() / f"{tool_id}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}{suffix}"


def write_artifact_meta(path: Path | str, meta: dict) -> None:
    """Write the sidecar for an artifact. Never fatal — a missing sidecar
    costs a downstream reader its completeness inheritance, not the result."""
    try:
        Path(str(path)).with_suffix(META_SUFFIX).write_text(json.dumps(meta, indent=2))
    except Exception as e:
        logger.warning("Failed to write artifact metadata for %s: %s", path, e)


def read_artifact_meta(path: Path | str) -> dict | None:
    """The acquisition record for an artifact, or None when it has none.

    None means "this file was not produced by a bounded acquisition" — an
    ordinary workspace file, say — which is different from a file known to be
    complete. Callers that must not upgrade a partial input treat only an
    explicit ``source_complete: false`` as partial.
    """
    try:
        p = Path(str(path)).with_suffix(META_SUFFIX)
        if not p.is_file():
            return None
        loaded = json.loads(p.read_text())
        return loaded if isinstance(loaded, dict) else None
    except Exception:
        return None


def write_artifact(text: str, tool_id: str, *, meta: dict | None = None) -> str:
    """Persist acquired text as evidence. Returns the path, or "" on failure."""
    path = new_artifact_path(tool_id)
    try:
        path.write_text(text)
    except Exception as e:
        logger.warning("Failed to persist tool output: %s", e)
        return ""
    if meta is not None:
        write_artifact_meta(path, {**meta, "artifact": str(path)})
    return str(path)


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------


def line_count(text: str) -> int:
    """Lines in ``text``, counting a trailing newline as a terminator rather
    than as the start of a phantom empty line — `text.count("\\n") + 1` said a
    3-line file had 4, and every "of N lines" built on it was one too many."""
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def _head_at_line_boundary(output: str, max_bytes: int) -> tuple[str, int, bool]:
    """(preview, complete_lines_shown, cut_mid_line).

    Cutting at the last newline inside the budget is what makes `offset=shown`
    exact. The audit's cursor advised `offset=shown_lines` after a preview that
    ended mid-line, and `file_read`'s 0-based offset then began at the line
    AFTER the cut one — the cut line was skipped whole, not just its remainder.
    """
    window = output[:max_bytes]
    cut = window.rfind("\n")
    if cut < 0:
        # A single line longer than the entire preview budget. There is no
        # line boundary to stop at, so say so rather than emit a cursor that
        # would step over the rest of it.
        return window, 0, True
    preview = window[: cut + 1]
    return preview, preview.count("\n"), False


def _tail_at_line_boundary(output: str, max_bytes: int) -> tuple[str, int, bool]:
    """(preview, complete_lines_shown, cut_mid_line) for direction='tail'."""
    window = output[-max_bytes:]
    nl = window.find("\n")
    if nl < 0:
        return window, 0, True
    preview = window[nl + 1 :]
    return preview, line_count(preview), False


def truncate_output(
    output: str,
    tool_id: str,
    direction: str = "head",
    max_bytes: int = MAX_OUTPUT,
    *,
    sources: list[dict] | None = None,
) -> tuple[str, dict]:
    """Render ``output`` down to a preview, persisting the full string to disk.

    ``output`` is the PRESENTATION copy — whatever readability transforms the
    caller applied have already run on it. ``sources`` carries the acquisition
    records for the raw evidence behind it (see ``acquisition_meta``), and the
    preview header quotes those for what the source actually held. Without
    them the header speaks only about the string it was given, and says so;
    what it must never do is present a clipped size as a source total.

    Returns:
        (truncated_or_original_output, metadata_dict)

    metadata_dict always contains:
        - truncated: bool
        - total_chars: int (of the string passed in)
        - source_complete: bool (False when any acquisition behind it was cut)
    If truncated, also contains:
        - output_path: str (path to the full presented string)
    """
    incomplete = [m for m in (sources or []) if not m.get("source_complete", True)]
    metadata: dict = {
        "truncated": False,
        "total_chars": len(output),
        "source_complete": not incomplete,
    }

    if len(output) <= max_bytes:
        return output, metadata

    rel_path = write_artifact(output, tool_id)
    if not rel_path:
        # Fall back to simple truncation.
        preview = output[:max_bytes] + f"\n[truncated, {len(output):,} total chars in this rendering]"
        metadata["truncated"] = True
        return preview, metadata

    total_lines = line_count(output)
    if direction == "tail":
        preview, shown_lines, mid_line = _tail_at_line_boundary(output, max_bytes)
        next_offset = max(total_lines - shown_lines, 0)
    else:
        preview, shown_lines, mid_line = _head_at_line_boundary(output, max_bytes)
        next_offset = shown_lines

    if mid_line:
        # One line wider than the preview budget: a minified bundle, a JSON
        # blob on one line. A line offset cannot address the middle of it, so
        # name the mechanism that can instead of inventing a cursor that would
        # step over the remainder.
        cursor = (
            f"  This rendering's first line alone exceeds the preview budget "
            f"({len(preview):,} chars shown of it). Line offsets cannot address inside a line — read "
            f'the rest with bash(command="cut -c {len(preview) + 1}- {rel_path} | head -c 40000")'
            + (f', or step past it with file_read(path="{rel_path}", offset=1, limit=200)' if total_lines > 1 else "")
            + "\n"
        )
    else:
        cursor = f'  file_read(path="{rel_path}", offset={next_offset}, limit=200)\n'

    header = (
        f"⚠ TRUNCATED — showing {shown_lines:,} of {total_lines:,} lines "
        f"({len(preview):,} of {len(output):,} chars of this rendering). "
        f"You are missing content. To read more, call:\n" + cursor
    )
    notes = acquisition_notes(sources)
    if notes:
        header += notes + "\n"
    header += (
        "For WHOLE-file analysis (summarize, extract structure, answer "
        "questions across all of it), rlm_process handles inputs this "
        "size in one call instead of many windowed reads.\n"
        if _rlm_available()
        else ""
    ) + "---\n"

    metadata["truncated"] = True
    metadata["output_path"] = rel_path
    return header + preview, metadata


def _rlm_available() -> bool:
    try:
        from config import settings

        return bool(settings.rlm_enabled)
    except Exception:
        return False
