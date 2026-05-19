"""Pernix — Chat endpoints.

All real-time events flow through the persistent SSE connection
(GET /api/sessions/{id}/events). Chat POST returns JSON immediately.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException

from config import settings
from db import models as db
from sessions.manager import get_manager

logger = logging.getLogger("pernix.chat")

# Image extensions recognized as vision-model inputs. Expansion to base64
# happens at compile-time (core/context/compiler.py), NOT at ingest —
# this keeps the DB small and prevents every future turn from re-shipping
# the full payload.
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

# Audio formats other than WAV. Ollama's gemma4/nemotron3 audio path only
# accepts WAV (RIFF/WAVE magic bytes), so anything else has to be
# transcoded at ingest. ffmpeg is invoked once per attachment; the result
# is cached as a sidecar `.wav` next to the original.
AUDIO_CONVERT_EXTENSIONS = {".mp3", ".m4a", ".ogg", ".flac", ".aac", ".opus", ".webm"}

# Hard cap on the time we'll wait for ffmpeg per attachment. A 10-minute
# audio file at 16kHz mono 16-bit decodes in well under 30s on modest
# hardware; longer means something's wrong (corrupt input, hung process).
AUDIO_CONVERT_TIMEOUT_S = 60

# Upper bound on extracted PDF text written to the sidecar file. At 200k
# chars it's a large read but still paginatable via file_read offset/limit.
PDF_EXTRACT_MAX_CHARS = 200_000

_ATTACHED_RE = re.compile(r"\[attached:\s*([^\]]+)\]")

router = APIRouter(tags=["chat"])


def _extract_pdf_text(pdf_path: Path) -> str | None:
    """Return extracted text from a PDF, or None on failure."""
    try:
        from pypdf import PdfReader
    except ImportError:
        logger.warning("pypdf not installed — PDF attachments won't be extracted")
        return None
    try:
        reader = PdfReader(str(pdf_path))
        parts: list[str] = []
        total = 0
        for i, page in enumerate(reader.pages):
            try:
                text = page.extract_text() or ""
            except Exception as e:
                logger.debug("pypdf page %d extract failed: %s", i, e)
                text = ""
            if not text:
                continue
            parts.append(f"--- page {i + 1} ---\n{text}")
            total += len(text)
            if total >= PDF_EXTRACT_MAX_CHARS:
                parts.append(
                    f"\n[truncated at {PDF_EXTRACT_MAX_CHARS:,} chars; " f"{len(reader.pages) - i - 1} pages remaining]"
                )
                break
        return "\n\n".join(parts) if parts else None
    except Exception as e:
        logger.warning("PDF text extraction failed for %s: %s", pdf_path, e)
        return None


async def _convert_audio_to_wav(src_path: Path, dst_path: Path) -> tuple[bool, str]:
    """Transcode `src_path` → `dst_path` (WAV, 16kHz mono PCM s16le) via ffmpeg.

    Returns (success, error_msg). 16kHz mono matches what Ollama's audio
    encoder resamples to internally (gemma4/process_audio.go), so doing it
    upfront keeps the inlined payload small.
    """
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",  # overwrite dst
        "-loglevel",
        "error",
        "-i",
        str(src_path),
        "-ar",
        "16000",
        "-ac",
        "1",
        "-c:a",
        "pcm_s16le",
        "-f",
        "wav",
        str(dst_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=AUDIO_CONVERT_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return False, f"ffmpeg timed out after {AUDIO_CONVERT_TIMEOUT_S}s"
    if proc.returncode != 0:
        msg = (stderr.decode(errors="replace").strip().splitlines() or ["unknown ffmpeg error"])[-1]
        return False, msg[:200]
    return True, ""


async def _prepare_attachments(message: str) -> str:
    """Rewrite attachment references in a fresh user message.

    - Leaves image `[attached: foo.jpg]` references untouched (compiler
      expands them to vision blocks only for the most-recent user turn on
      a vision-capable model).
    - Extracts text from `.pdf` attachments into a sidecar `foo.pdf.txt`
      in the workspace and rewrites the reference so the agent (and the
      LLM) can `file_read` the text directly. Falls back to a hint if
      extraction fails.
    - Transcodes non-WAV audio (mp3/m4a/ogg/flac/aac/opus/webm) into a
      sidecar `foo.mp3.wav` via ffmpeg, since Ollama's audio path only
      reads WAV. If ffmpeg isn't installed, leaves a hint pointing at it.
    - Does NOT base64-inline anything into the stored message body.
    """
    matches = _ATTACHED_RE.findall(message)
    if not matches:
        return message

    from core.tools.paths import safe_read_path, safe_write_path

    replacements: list[tuple[str, str]] = []
    # shutil.which is cheap but not free; check once per message rather
    # than per attachment.
    ffmpeg_available: bool | None = None

    for raw in matches:
        filename = raw.strip()
        ext = Path(filename).suffix.lower()

        if ext in AUDIO_CONVERT_EXTENSIONS:
            try:
                src = safe_read_path(filename)
            except ValueError as e:
                logger.warning("audio attachment rejected (path): %s — %s", filename, e)
                continue
            if not src.exists():
                logger.warning("Attached audio not found: %s", src)
                continue

            sidecar_name = f"{filename}.wav"
            try:
                sidecar = safe_write_path(sidecar_name)
            except ValueError as e:
                logger.warning("audio sidecar rejected (path): %s — %s", sidecar_name, e)
                continue
            if sidecar.exists() and sidecar.stat().st_mtime >= src.stat().st_mtime:
                replacements.append(
                    (f"[attached: {filename}]", f"[attached: {sidecar_name}]"),
                )
                continue

            if ffmpeg_available is None:
                ffmpeg_available = shutil.which("ffmpeg") is not None
            if not ffmpeg_available:
                logger.warning("ffmpeg not installed — cannot transcode %s to WAV", filename)
                replacements.append(
                    (
                        f"[attached: {filename}]",
                        f"[attached: {filename} (audio not transcoded — install ffmpeg "
                        f"to enable {ext} support; Ollama's audio path only reads WAV)]",
                    )
                )
                continue

            ok, err = await _convert_audio_to_wav(src, sidecar)
            if not ok:
                logger.warning("ffmpeg failed for %s: %s", filename, err)
                replacements.append(
                    (
                        f"[attached: {filename}]",
                        f"[attached: {filename} (ffmpeg transcode failed: {err})]",
                    )
                )
                continue
            replacements.append(
                (f"[attached: {filename}]", f"[attached: {sidecar_name}]"),
            )
            continue

        if ext != ".pdf":
            continue

        # Reject traversal — a user message with `[attached: ../x.pdf]`
        # must not cause us to read outside the workspace or land the
        # sidecar in a neighbour directory.
        try:
            pdf_path = safe_read_path(filename)
        except ValueError as e:
            logger.warning("PDF attachment rejected (path): %s — %s", filename, e)
            continue
        if not pdf_path.exists():
            logger.warning("Attached PDF not found: %s", pdf_path)
            continue

        sidecar_name = f"{filename}.txt"
        try:
            sidecar_path = safe_write_path(sidecar_name)
        except ValueError as e:
            logger.warning("PDF sidecar rejected (path): %s — %s", sidecar_name, e)
            continue
        # Reuse existing sidecar if it's newer than the PDF.
        if sidecar_path.exists() and sidecar_path.stat().st_mtime >= pdf_path.stat().st_mtime:
            replacements.append(
                (
                    f"[attached: {filename}]",
                    f"[attached: {filename} — text at {sidecar_name}]",
                )
            )
            continue

        text = await asyncio.to_thread(_extract_pdf_text, pdf_path)
        if text is None:
            replacements.append(
                (
                    f"[attached: {filename}]",
                    f"[attached: {filename} (PDF text extraction failed — " f'try: bash pdftotext "{filename}" -)]',
                )
            )
            continue

        try:
            sidecar_path.write_text(text)
        except Exception as e:
            logger.warning("Failed to write PDF sidecar %s: %s", sidecar_path, e)
            continue

        replacements.append(
            (
                f"[attached: {filename}]",
                f"[attached: {filename} — text at {sidecar_name}]",
            )
        )

    out = message
    for old, new in replacements:
        out = out.replace(old, new, 1)
    return out


@router.post("/api/chat")
async def chat(body: dict):
    """Send a message to a session. Events delivered via persistent SSE."""
    session_id = body.get("session_id")
    message = body.get("message", "")
    system_prompt = body.get("system_prompt", "")

    if not session_id:
        raise HTTPException(400, detail="session_id required")
    if not message:
        raise HTTPException(400, detail="message required")
    MAX_MESSAGE_SIZE = 1_000_000  # 1MB
    if len(message) > MAX_MESSAGE_SIZE:
        raise HTTPException(413, detail=f"Message too large ({len(message)} bytes, max {MAX_MESSAGE_SIZE})")

    # Check idempotency via DB index (not full scan)
    idempotency_key = body.get("idempotency_key")
    if idempotency_key:
        with db.connect_sessions() as conn:
            row = conn.execute(
                "SELECT id FROM messages WHERE idempotency_key = ? LIMIT 1",
                (idempotency_key,),
            ).fetchone()
            if row:
                return {"status": "duplicate", "session_id": session_id}

    manager = get_manager()
    session_db = db.get_session(session_id)
    if not session_db:
        raise HTTPException(404, detail=f"Session {session_id} not found")

    # Rewrite attachment references: extract PDF text to sidecars, leave
    # image refs as-is for compile-time expansion. No base64 enters the DB.
    stored_message = await _prepare_attachments(message)
    await manager.prompt(
        session_id,
        stored_message,
        system_prompt,
        idempotency_key=idempotency_key,
    )
    return {"status": "accepted", "session_id": session_id}


@router.post("/api/chat/inject")
async def inject(body: dict):
    """Inject a message into a running session's context.

    Unlike /api/chat which queues for a new turn, this writes the message
    directly to the DB. The agent sees it at the next tool round via
    compile_context(), which reads fresh from the DB each round.

    Inject-only ("context drop") semantics only work while the agent loop
    has more compile_context() calls ahead — i.e. PROCESSING /
    AWAITING_WORKERS / COMPACTING. In any other state (FINALIZING,
    IDLE_READY, AWAITING_USER, paused, cancelling) no further round will
    run and get_orphaned_user_messages skips injected rows by design, so
    the message would be permanently stranded. Fall through to
    manager.prompt() in those cases so the message lands as a queued or
    new turn instead.
    """
    session_id = body.get("session_id")
    message = body.get("message", "")

    if not session_id:
        raise HTTPException(400, detail="session_id required")
    if not message:
        raise HTTPException(400, detail="message required")
    MAX_MESSAGE_SIZE = 1_000_000  # 1MB
    if len(message) > MAX_MESSAGE_SIZE:
        raise HTTPException(413, detail=f"Message too large ({len(message)} bytes, max {MAX_MESSAGE_SIZE})")

    session_db = db.get_session(session_id)
    if not session_db:
        raise HTTPException(404, detail=f"Session {session_id} not found")

    manager = get_manager()
    session = manager.get(session_id)

    from sessions import state_v2 as sv2

    LIVE_LOOP_STATES = {
        sv2.SessionStateV2.PROCESSING,
        sv2.SessionStateV2.AWAITING_WORKERS,
        sv2.SessionStateV2.COMPACTING,
    }
    current = sv2._current_state(session) if session is not None else None
    if current not in LIVE_LOOP_STATES:
        await manager.prompt(session_id, message)
        return {"status": "queued", "session_id": session_id}

    # Tag with metadata.injected so compile_context's turn-scoping filter
    # doesn't drop this row. The filter (in core/context/compiler.py) hides
    # user messages with id > turn_user_msg_id to prevent turn N from
    # pre-answering queued messages bound for turn N+1 — but injected
    # messages are explicitly meant to land in the CURRENT turn's view.
    import json as _json

    db.add_message(
        session_id,
        "user",
        message,
        metadata=_json.dumps({"injected": True}),
    )

    if session:
        session.emit_event({"type": "message.injected", "content": message[:100]})

    return {"status": "injected", "session_id": session_id}


@router.post("/api/retry/{session_id}")
async def retry(session_id: str):
    """Retry from last user message (deletes partial + re-prompts)."""
    session_db = db.get_session(session_id)
    if not session_db:
        raise HTTPException(404, detail=f"Session {session_id} not found")

    # Find last partial
    partial = db.get_last_partial(session_id)
    if partial:
        db.delete_message(partial["id"])

    # Find last user message
    messages = db.get_messages(session_id)
    last_user = None
    for m in reversed(messages):
        if m["role"] == "user":
            last_user = m
            break

    if not last_user:
        raise HTTPException(400, detail="No user message to retry from")

    # Delete from last user message onward (re-process)
    db.delete_messages_from(session_id, last_user["id"])

    manager = get_manager()
    await manager.prompt(session_id, last_user["content"])
    return {"status": "retrying", "session_id": session_id}


@router.get("/api/partial/{session_id}")
async def get_partial(session_id: str):
    """Check for partial (interrupted) messages."""
    partial = db.get_last_partial(session_id)
    if not partial:
        return {"has_partial": False}
    return {
        "has_partial": True,
        "message_id": partial["id"],
        "content_preview": partial["content"][:200],
    }


@router.post("/api/compact/{session_id}")
async def compact(session_id: str):
    """Force context compaction."""
    from sessions.manager import get_manager

    manager = get_manager()
    session = manager.get(session_id)
    if session:
        from sessions import state_v2 as _sv2

        if _sv2._current_state(session) is not _sv2.SessionStateV2.IDLE_READY:
            raise HTTPException(
                409,
                detail=f"Session is {_sv2._current_state(session).value}, must be idle_ready to compact",
            )

    from core.context.compaction import compact_with_llm

    messages = db.get_messages(session_id)
    msg_dicts = [dict(m) for m in messages]

    did_compact = await compact_with_llm(session_id, msg_dicts)
    return {"compacted": did_compact}


@router.get("/api/usage/{session_id}")
async def get_usage(session_id: str):
    return db.get_session_usage(session_id)
