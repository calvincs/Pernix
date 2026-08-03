"""Tests for the voice input (STT) router and its settings plumbing."""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from config import settings


def _make_app(*routers):
    app = FastAPI()
    for router in routers:
        app.include_router(router)
    return app


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# A minimal but valid WAV header + a little silence — enough for upload paths.
_TINY_WAV = (
    b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00"
    b"\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00" + b"\x00" * 64
)


# ---------------------------------------------------------------------------
# /api/voice/status
# ---------------------------------------------------------------------------


async def test_status_mode_off(monkeypatch):
    from api.routers import voice

    monkeypatch.setattr(settings, "voice_mode", "off")
    async with _client(_make_app(voice.router)) as client:
        resp = await client.get("/api/voice/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "off"
    assert data["usable"] is False


async def test_status_local_whisper_not_installed(monkeypatch):
    from api.routers import voice

    monkeypatch.setattr(settings, "voice_mode", "local_whisper")
    monkeypatch.setattr(voice, "_whisper_available", lambda: False)
    async with _client(_make_app(voice.router)) as client:
        resp = await client.get("/api/voice/status")
    data = resp.json()
    assert data["usable"] is False
    assert "faster-whisper" in data["reason"]


async def test_status_remote_configured(monkeypatch):
    from api.routers import voice

    monkeypatch.setattr(settings, "voice_mode", "remote_whisper")
    monkeypatch.setattr(settings, "voice_remote_url", "https://stt.example/v1")
    async with _client(_make_app(voice.router)) as client:
        resp = await client.get("/api/voice/status")
    data = resp.json()
    assert data["usable"] is True
    assert data["reason"] == ""
    assert "language" in data  # web_speech dictation reads its lang hint from here


async def test_status_model_direct_probes_registry(monkeypatch):
    from api.routers import voice

    monkeypatch.setattr(settings, "voice_mode", "model_direct")

    async def _yes():
        return True

    monkeypatch.setattr(voice, "_active_model_supports_audio", _yes)
    async with _client(_make_app(voice.router)) as client:
        resp = await client.get("/api/voice/status")
    assert resp.json()["usable"] is True


# ---------------------------------------------------------------------------
# /api/voice/transcribe — mode gating and error paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["off", "web_speech", "model_direct"])
async def test_transcribe_rejected_for_non_whisper_modes(monkeypatch, mode):
    from api.routers import voice

    monkeypatch.setattr(settings, "voice_mode", mode)
    async with _client(_make_app(voice.router)) as client:
        resp = await client.post("/api/voice/transcribe", files={"file": ("clip.wav", _TINY_WAV, "audio/wav")})
    assert resp.status_code == 400


async def test_transcribe_bad_extension(monkeypatch):
    from api.routers import voice

    monkeypatch.setattr(settings, "voice_mode", "local_whisper")
    async with _client(_make_app(voice.router)) as client:
        resp = await client.post("/api/voice/transcribe", files={"file": ("clip.txt", b"not audio", "text/plain")})
    assert resp.status_code == 400


async def test_transcribe_empty_recording(monkeypatch):
    from api.routers import voice

    monkeypatch.setattr(settings, "voice_mode", "local_whisper")
    async with _client(_make_app(voice.router)) as client:
        resp = await client.post("/api/voice/transcribe", files={"file": ("clip.wav", b"", "audio/wav")})
    assert resp.status_code == 400


async def test_transcribe_oversize(monkeypatch):
    from api.routers import voice

    monkeypatch.setattr(settings, "voice_mode", "local_whisper")
    monkeypatch.setattr(voice, "MAX_AUDIO_BYTES", 16)
    async with _client(_make_app(voice.router)) as client:
        resp = await client.post("/api/voice/transcribe", files={"file": ("clip.wav", _TINY_WAV, "audio/wav")})
    assert resp.status_code == 413


async def test_transcribe_local_whisper_missing_dep(monkeypatch):
    from api.routers import voice

    monkeypatch.setattr(settings, "voice_mode", "local_whisper")
    monkeypatch.setattr(voice, "_whisper_available", lambda: False)
    async with _client(_make_app(voice.router)) as client:
        resp = await client.post("/api/voice/transcribe", files={"file": ("clip.wav", _TINY_WAV, "audio/wav")})
    assert resp.status_code == 501
    assert "faster-whisper" in resp.json()["detail"]


async def test_transcribe_remote_without_url(monkeypatch):
    from api.routers import voice

    monkeypatch.setattr(settings, "voice_mode", "remote_whisper")
    monkeypatch.setattr(settings, "voice_remote_url", "")
    async with _client(_make_app(voice.router)) as client:
        resp = await client.post("/api/voice/transcribe", files={"file": ("clip.wav", _TINY_WAV, "audio/wav")})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# /api/voice/transcribe — success paths (engines mocked)
# ---------------------------------------------------------------------------


async def test_transcribe_local_success(monkeypatch):
    from api.routers import voice

    monkeypatch.setattr(settings, "voice_mode", "local_whisper")
    monkeypatch.setattr(voice, "_whisper_available", lambda: True)

    async def _fake_local(wav_path):
        assert wav_path.exists()
        return "hello from whisper"

    monkeypatch.setattr(voice, "_transcribe_local", _fake_local)
    async with _client(_make_app(voice.router)) as client:
        resp = await client.post("/api/voice/transcribe", files={"file": ("clip.wav", _TINY_WAV, "audio/wav")})
    assert resp.status_code == 200
    data = resp.json()
    assert data["text"] == "hello from whisper"
    assert data["engine"] == "local_whisper"


async def test_transcribe_remote_success(monkeypatch):
    from api.routers import voice

    monkeypatch.setattr(settings, "voice_mode", "remote_whisper")
    monkeypatch.setattr(settings, "voice_remote_url", "https://stt.example/v1")

    async def _fake_remote(audio_path, content_type):
        assert audio_path.exists()
        return "hello from the cloud"

    monkeypatch.setattr(voice, "_transcribe_remote", _fake_remote)
    async with _client(_make_app(voice.router)) as client:
        resp = await client.post(
            "/api/voice/transcribe", files={"file": ("clip.webm", b"\x1a\x45\xdf\xa3fake", "audio/webm")}
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["text"] == "hello from the cloud"
    assert data["engine"] == "remote_whisper"


# ---------------------------------------------------------------------------
# Settings plumbing — enum validation and persistence fields
# ---------------------------------------------------------------------------


async def test_settings_voice_mode_enum_guard(monkeypatch):
    from api.routers import health

    monkeypatch.setattr(settings, "voice_mode", "off")
    async with _client(_make_app(health.router)) as client:
        bad = await client.post("/api/settings", json={"voice_mode": "banana"})
        assert "voice_mode" not in bad.json()["updated"]
        assert settings.voice_mode == "off"

        good = await client.post("/api/settings", json={"voice_mode": "local_whisper"})
        assert "voice_mode" in good.json()["updated"]
        assert settings.voice_mode == "local_whisper"


async def test_settings_whisper_model_enum_guard(monkeypatch):
    from api.routers import health

    monkeypatch.setattr(settings, "voice_whisper_model", "base")
    async with _client(_make_app(health.router)) as client:
        bad = await client.post("/api/settings", json={"voice_whisper_model": "enormous"})
        assert "voice_whisper_model" not in bad.json()["updated"]

        good = await client.post("/api/settings", json={"voice_whisper_model": "small"})
        assert "voice_whisper_model" in good.json()["updated"]
        assert settings.voice_whisper_model == "small"


async def test_settings_voice_remote_url_clearable(monkeypatch):
    from api.routers import health

    monkeypatch.setattr(settings, "voice_remote_url", "https://stt.example/v1")
    async with _client(_make_app(health.router)) as client:
        resp = await client.post("/api/settings", json={"voice_remote_url": ""})
    assert "voice_remote_url" in resp.json()["updated"]
    assert settings.voice_remote_url == ""


async def test_settings_exposes_voice_fields():
    from api.routers import health

    async with _client(_make_app(health.router)) as client:
        resp = await client.get("/api/settings")
    data = resp.json()
    assert "voice_mode" in data
    assert "voice_web_speech_fallback" in data
    assert "voice_stt_api_key_set" in data
