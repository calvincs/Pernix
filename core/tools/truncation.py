"""Pernix — Shared output truncation with disk-backed full output persistence.

When tool output exceeds MAX_OUTPUT, the full output is written to a temp file
so the agent can drill into it later via file_read with offset/limit.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

logger = logging.getLogger("pernix.tools.truncation")

MAX_OUTPUT = 50_000  # 50KB preview cap
TOOL_OUTPUT_DIR = Path("data/.tool_output")
CLEANUP_MAX_AGE_SECS = 3600  # 1 hour


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


def truncate_output(
    output: str,
    tool_id: str,
    direction: str = "head",
    max_bytes: int = MAX_OUTPUT,
) -> tuple[str, dict]:
    """Truncate output if it exceeds max_bytes, persisting full output to disk.

    Returns:
        (truncated_or_original_output, metadata_dict)

    metadata_dict always contains:
        - truncated: bool
        - total_chars: int
    If truncated, also contains:
        - output_path: str (relative path to full output file)
    """
    metadata: dict = {"truncated": False, "total_chars": len(output)}

    if len(output) <= max_bytes:
        return output, metadata

    # Persist full output to disk
    _cleanup_stale_files()
    out_dir = _ensure_output_dir()
    # Use tool_id + timestamp for uniqueness
    filename = f"{tool_id}_{int(time.time() * 1000)}.txt"
    out_path = out_dir / filename
    try:
        out_path.write_text(output)
    except Exception as e:
        logger.warning("Failed to persist tool output: %s", e)
        # Fall back to simple truncation
        preview = output[:max_bytes] + f"\n[truncated, {len(output)} total chars]"
        metadata["truncated"] = True
        return preview, metadata

    rel_path = str(out_path)

    total_lines = output.count("\n") + 1
    if direction == "tail":
        preview = output[-max_bytes:]
        shown_lines = preview.count("\n") + 1
    else:
        preview = output[:max_bytes]
        shown_lines = preview.count("\n") + 1

    preview = (
        f"⚠ TRUNCATED — showing {shown_lines:,} of {total_lines:,} lines "
        f"({max_bytes:,} of {len(output):,} chars). "
        f"You are missing content. To read more, call:\n"
        f'  file_read(path="{rel_path}", offset={shown_lines}, limit=200)\n'
        f"---\n"
    ) + preview

    metadata["truncated"] = True
    metadata["output_path"] = rel_path
    return preview, metadata
