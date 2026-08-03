"""Pernix — Voice input (speech-to-text) endpoints.

Two server-side engines live here; the other two modes never touch these
endpoints (web_speech runs entirely in the browser, model_direct rides the
normal attachment pipeline in chat.py):

- local_whisper: faster-whisper on this machine. Optional dependency —
  the endpoint degrades to 501 with an install hint when absent.
- remote_whisper: OpenAI-compatible POST /audio/transcriptions. The API
  key comes from VOICE_STT_API_KEY (env / .env via /api/settings/apikey),
  never from settings.json.

GET /api/voice/status tells the frontend which engines are actually usable
right now, so the mic button can pick the configured engine or fall back to
browser dictation only when the user has opted into that.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile

from config import settings

logger = logging.getLogger("pernix.voice")

router = APIRouter(tags=["voice"])

# Recordings are short dictation clips, not podcasts. 25MB ≈ 40+ minutes of
# webm/opus — anything bigger is a mistake, reject before buffering it all.
MAX_AUDIO_BYTES = 25 * 1024 * 1024

# Formats browsers actually produce from MediaRecorder, plus WAV for tests
# and non-browser clients.
ACCEPTED_AUDIO_EXTENSIONS = {".webm", ".ogg", ".mp4", ".m4a", ".wav", ".mp3"}

REMOTE_TRANSCRIBE_TIMEOUT_S = 120

# Lazy singleton — faster-whisper model load takes seconds and holds RAM;
# do it once, on first use, off the event loop. Guarded by a lock so two
# concurrent first-requests don't both load it.
_whisper_model = None
_whisper_model_name: str | None = None
_whisper_lock = asyncio.Lock()


def _whisper_available() -> bool:
    try:
        import faster_whisper  # noqa: F401

        return True
    except ImportError:
        return False


async def _get_whisper_model():
    """Return the cached WhisperModel, (re)loading if the size setting changed."""
    global _whisper_model, _whisper_model_name
    async with _whisper_lock:
        if _whisper_model is not None and _whisper_model_name == settings.voice_whisper_model:
            return _whisper_model
        from faster_whisper import WhisperModel

        name = settings.voice_whisper_model

        def _load():
            # int8 halves memory vs float32 with negligible accuracy loss on
            # dictation-length clips; CPU keeps this off the GPU the LLM uses.
            return WhisperModel(name, device="cpu", compute_type="int8")

        logger.info("Loading faster-whisper model %r", name)
        _whisper_model = await asyncio.to_thread(_load)
        _whisper_model_name = name
        return _whisper_model


async def _active_model_supports_audio() -> bool:
    try:
        from core.llm.client import get_llm_client

        info = await get_llm_client().get_model_info(settings.llm_model)
        return bool(info and info.supports_audio)
    except Exception as e:
        logger.debug("model audio-capability probe failed: %s", e)
        return False


@router.get("/api/voice/status")
async def voice_status():
    """Engine availability for the mic button.

    `usable` is the server's verdict on the *configured* mode; the frontend
    combines it with voice_web_speech_fallback (and browser support) to
    decide whether to record, dictate, or explain what's missing.
    """
    mode = settings.voice_mode
    whisper_installed = _whisper_available()
    remote_configured = bool(settings.voice_remote_url)
    remote_key_set = bool(os.environ.get("VOICE_STT_API_KEY"))
    model_audio = await _active_model_supports_audio() if mode == "model_direct" else False

    usable = {
        "off": False,
        "local_whisper": whisper_installed,
        "remote_whisper": remote_configured,
        "model_direct": model_audio,
        "web_speech": True,  # browser-side; the client still checks SpeechRecognition support
    }[mode]

    reason = ""
    if mode == "local_whisper" and not whisper_installed:
        reason = "faster-whisper is not installed on the server (pip install faster-whisper)"
    elif mode == "remote_whisper" and not remote_configured:
        reason = "no remote transcription URL configured in Settings → Voice Input"
    elif mode == "model_direct" and not model_audio:
        reason = f"active model {settings.llm_model or '(not set)'} does not support audio input"

    return {
        "mode": mode,
        "usable": usable,
        "reason": reason,
        "language": settings.voice_language,
        "fallback_web_speech": settings.voice_web_speech_fallback,
        "auto_send": settings.voice_auto_send,
        "whisper_installed": whisper_installed,
        "whisper_model": settings.voice_whisper_model,
        "remote_configured": remote_configured,
        "remote_key_set": remote_key_set,
        "ffmpeg_installed": shutil.which("ffmpeg") is not None,
    }


async def _transcribe_local(wav_path: Path) -> str:
    model = await _get_whisper_model()
    language = settings.voice_language or None

    def _run() -> str:
        segments, _info = model.transcribe(str(wav_path), language=language, vad_filter=True)
        # segments is a lazy generator — consuming it here keeps the decode
        # inside the worker thread instead of blocking the event loop later.
        return " ".join(seg.text.strip() for seg in segments).strip()

    return await asyncio.to_thread(_run)


async def _transcribe_remote(audio_path: Path, content_type: str) -> str:
    import httpx

    url = settings.voice_remote_url.rstrip("/") + "/audio/transcriptions"
    headers = {}
    api_key = os.environ.get("VOICE_STT_API_KEY", "")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    files = {"file": (audio_path.name, audio_path.read_bytes(), content_type or "application/octet-stream")}
    data = {"model": settings.voice_remote_model}
    if settings.voice_language:
        data["language"] = settings.voice_language

    async with httpx.AsyncClient(timeout=REMOTE_TRANSCRIBE_TIMEOUT_S) as client:
        resp = await client.post(url, headers=headers, files=files, data=data)
    if resp.status_code != 200:
        detail = resp.text[:200]
        raise HTTPException(502, detail=f"Remote transcription failed ({resp.status_code}): {detail}")
    try:
        return (resp.json().get("text") or "").strip()
    except ValueError:
        raise HTTPException(502, detail="Remote transcription returned a non-JSON response")


@router.post("/api/voice/transcribe")
async def transcribe(file: UploadFile = File(...)):
    """Transcribe a dictation clip using the configured server-side engine."""
    mode = settings.voice_mode
    if mode not in ("local_whisper", "remote_whisper"):
        raise HTTPException(400, detail=f"voice_mode is {mode!r} — transcription only applies to whisper modes")

    ext = Path(file.filename or "clip.webm").suffix.lower() or ".webm"
    if ext not in ACCEPTED_AUDIO_EXTENSIONS:
        raise HTTPException(400, detail=f"Unsupported audio format {ext}")

    payload = await file.read()
    if len(payload) > MAX_AUDIO_BYTES:
        raise HTTPException(413, detail="Recording exceeds the 25MB limit")
    if not payload:
        raise HTTPException(400, detail="Empty recording")

    if mode == "local_whisper" and not _whisper_available():
        raise HTTPException(
            501,
            detail="faster-whisper is not installed — pip install faster-whisper, "
            "or choose a different engine in Settings → Voice Input",
        )
    if mode == "remote_whisper" and not settings.voice_remote_url:
        raise HTTPException(400, detail="No remote transcription URL configured in Settings → Voice Input")

    # Recordings are transient — transcribe from a temp dir, never the
    # workspace (dictation audio is not a user artifact the agent should see).
    tmpdir = tempfile.mkdtemp(prefix="pernix-voice-")
    try:
        src = Path(tmpdir) / f"clip{ext}"
        src.write_bytes(payload)

        if mode == "remote_whisper":
            text = await _transcribe_remote(src, file.content_type or "")
            return {"text": text, "engine": "remote_whisper"}

        # faster-whisper decodes via PyAV, which handles webm/opus directly —
        # but going through the existing ffmpeg→16k mono WAV path first keeps
        # behavior identical to the attachment pipeline and strips container
        # quirks from Safari's mp4 recordings. Fall back to the raw file if
        # ffmpeg is missing (PyAV usually copes).
        wav = src
        if ext != ".wav" and shutil.which("ffmpeg"):
            from api.routers.chat import _convert_audio_to_wav

            converted = Path(tmpdir) / "clip.wav"
            ok, err = await _convert_audio_to_wav(src, converted)
            if ok:
                wav = converted
            else:
                logger.warning("voice: ffmpeg conversion failed (%s) — trying raw file", err)

        try:
            text = await _transcribe_local(wav)
        except HTTPException:
            raise
        except Exception as e:
            logger.error("local whisper transcription failed: %s", e)
            raise HTTPException(500, detail=f"Transcription failed: {e}")
        return {"text": text, "engine": "local_whisper", "model": settings.voice_whisper_model}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
