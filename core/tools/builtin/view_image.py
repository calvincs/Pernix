"""view_image — let the agent LOOK at an image it rendered.

The context compiler inlines image bytes for exactly one location: an
``[attached: file]`` reference in the LATEST user-role message (vision models
only). Agent-side paths are all text: tool results carry no image blocks,
file_read refuses binaries, and mid-turn system messages get stripped by
provider normalization. Field case (ARC-2 task 136b0064): agents rendered
grids to ASCII coordinate tables for hours because a self-made PNG had no
route into context — while the chained-path structure was visible at a
glance.

This tool closes the loop by riding the one path that already works: it
validates the file, then inserts a clearly-labelled synthetic user-role note
containing the ``[attached: ...]`` reference. On the next round that note IS
the latest user message, so the existing expansion turns it into a real
image block. No compiler or provider changes — the battle-tested user-image
path end to end.
"""

from __future__ import annotations

import logging
import threading

from config import settings
from db import models as db

logger = logging.getLogger(__name__)

# Keep in lockstep with the compiler's inline set (core/context/compiler.py).
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

_NOTE_PREFIX = "[view_image]"

# Pictures one round may queue when settings.image_max_per_prompt is 0 (no
# server limit). Each one costs prompt tokens, so the number stays small.
_DEFAULT_ROUND_CAP = 4
# view_image calls of one round can run at the same time; they share one note.
_note_lock = threading.Lock()


def _round_cap() -> int:
    return int(getattr(settings, "image_max_per_prompt", 0) or 0) or _DEFAULT_ROUND_CAP


def _open_round_note(session_id: str) -> dict | None:
    """The view_image note this round already wrote, if it is still the newest message.

    A reply from the model or a tool result after it means a new round began.
    """
    try:
        rows = db.get_messages(session_id, last=1)
    except Exception:
        return None
    last = rows[-1] if rows else None
    if last and last.get("role") == "user" and (last.get("content") or "").startswith(_NOTE_PREFIX):
        return last
    return None


def view_image(path: str, _context: dict | None = None) -> str:
    """Queue an image file so the model actually sees it next round."""
    from pathlib import Path

    from core.context.compiler import MAX_INLINE_ATTACH_BYTES
    from core.tools.paths import safe_read_path

    session_id = (_context or {}).get("session_id", "")
    if not session_id:
        return "Error: view_image needs a session context and none was provided."

    ext = Path(path).suffix.lower()
    if ext not in _IMAGE_EXTENSIONS:
        return (
            f"Error: '{path}' is not a supported image type "
            f"({', '.join(sorted(_IMAGE_EXTENSIONS))}). Render to PNG first."
        )
    try:
        resolved = safe_read_path(path)
    except ValueError as e:
        return f"Error: path rejected — {e}"
    if not resolved.exists() or not resolved.is_file():
        return f"Error: '{path}' does not exist (render it first, then call view_image)."

    budget = int(settings.max_inline_attach_bytes or MAX_INLINE_ATTACH_BYTES)
    size = resolved.stat().st_size
    if int(size * 1.34) > budget:  # base64 overhead, same maths as the compiler
        return (
            f"Error: '{path}' is {size} bytes; the inline budget is {budget}. "
            "Downscale the render (smaller figure, lower DPI) and retry."
        )

    # Only the newest user message has its pictures sent. Each call used to
    # write its own note, so five calls in one round showed the last picture
    # only, and every call still answered "Image queued": the agent then
    # described four pictures it never saw. The calls of one round now share
    # one note, up to the cap, and a call over the cap says so.
    cap = _round_cap()
    with _note_lock:
        open_note = _open_round_note(session_id)
        if open_note is not None:
            queued = open_note["content"].count("[attached:")
            if f"[attached: {resolved}]" in open_note["content"]:
                return f"Image queued: {resolved} is already queued for your next round."
            if queued >= cap:
                return (
                    f"Error: '{path}' is NOT queued. A round can show {cap} picture(s) and {queued} "
                    "are queued already. Look at those first, then call view_image for this one "
                    "in a later round."
                )
            db.update_message_content(open_note["id"], open_note["content"] + f"\n[attached: {resolved}]")
            logger.info("view_image queued %s (%d bytes) for session %s", resolved, size, session_id[:12])
            return _queued_text(resolved, size, queued + 1, cap)
        return _queue_new_note(session_id, resolved, size, cap)


def _queued_text(resolved, size: int, position: int, cap: int) -> str:
    return (
        f"Image queued: {resolved} ({size} bytes), picture {position} of at most {cap} this round. "
        "From your NEXT round it appears as an actual image in your context, attached to a "
        "'[view_image]' note. Only the pictures queued in your most recent round are visible; "
        "older ones revert to text markers, so call view_image again to look at one again. "
        "If the active model cannot accept images, the reference stays text."
    )


def _queue_new_note(session_id: str, resolved, size: int, cap: int) -> str:
    note = (
        f"{_NOTE_PREFIX} Harness-injected on the agent's own request via the "
        "view_image tool — not a human message. The agent asked to look at "
        f"this rendered image:\n[attached: {resolved}]"
    )
    # Stamp EXACTLY like /api/chat/inject: without metadata.injected the
    # compiler's turn-scoping filter reads this row as a QUEUED next-turn
    # message and excludes it from the current compile — the model never
    # received the note or the image (field case 66cc9f8865ae: smoke test
    # answered "Red" for a yellow PNG; prompt-token delta proved no image
    # was inlined; sessions f586/1aec were vision-blind all along). The
    # parent stamp also sorts the note chronologically inside the turn.
    import json as _json

    meta: dict = {"injected": True}
    try:
        from sessions.manager import get_manager

        _s = get_manager().get(session_id)
        _turn_root = getattr(_s, "current_turn_user_msg_id", None) if _s else None
        if _turn_root is not None:
            meta["parent_user_msg_id"] = _turn_root
    except Exception:
        pass  # stamp what we can — injected:True alone unblocks the filter
    db.add_message(session_id, "user", note, metadata=_json.dumps(meta))
    logger.info("view_image queued %s (%d bytes) for session %s", resolved, size, session_id[:12])
    return _queued_text(resolved, size, 1, cap)


def register(reg) -> None:
    reg.register(
        name="view_image",
        func=view_image,
        description=(
            "LOOK at an image file with your own eyes (vision-capable models). "
            "Render grids/plots/diagrams to PNG in the workspace, then call this "
            "with the path — the actual image enters your context on the next "
            "round. Visual structure (paths, symmetry, alignment, shapes) is "
            "often obvious in a picture and invisible in coordinate printouts. "
            "Several calls in one round arrive together, up to a small limit; "
            "a call over the limit returns an error and that picture is not shown. "
            "A picture is visible for one round: call again for a second look. "
            "Supported: .png .jpg .jpeg .gif .webp."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Image file path (workspace-relative or absolute within allowed roots)",
                },
            },
            "required": ["path"],
        },
        category="core",
        tags=["vision", "image", "analysis"],
        safety_level="safe",
    )
