"""Regression tests for attachment ingest + compile-time expansion.

Prior to the rework, image attachments were base64-inlined into the
stored user message at ingest. A single dropped image could balloon
messages.content to ~1.4 MB and re-ship that payload to every future
turn plus the scout agent. These tests pin the new contract:

- Chat ingest stores plain text only. Image refs stay as [attached: x.jpg].
- PDF ingest extracts text to a sidecar and rewrites the reference.
- The compiler expands images to vision blocks ONLY for the latest user
  turn, only on vision-capable models.
- Legacy JSON-blob rows in the DB are collapsed to text markers before
  scout sees them, and aren't counted at full token cost.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from api.routers.chat import _extract_pdf_text, _prepare_attachments
from core.context.compiler import (
    _expand_user_message_with_images,
    _extract_attached_filenames,
    _legacy_multimodal_to_text,
)

# ---------------------------------------------------------------------------
# Ingest: no base64 in the stored message body
# ---------------------------------------------------------------------------


async def test_prepare_attachments_leaves_images_as_refs(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    (tmp_path / "cat.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"x" * 2000)  # dummy JPEG
    msg = "Look: [attached: cat.jpg]"
    out = await _prepare_attachments(msg)
    # The reference must stay; no base64, no data: URL, no JSON list.
    assert out == msg
    assert "base64" not in out
    assert "data:image" not in out


async def test_prepare_attachments_extracts_pdf(tmp_path, monkeypatch):
    """PDF attachments get extracted to a sidecar .txt file."""
    from pypdf import PdfWriter

    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    # Build a minimal PDF with one blank page — pypdf can round-trip it.
    # (Real PDFs usually have text; we just assert the pipeline runs.)
    pdf_path = tmp_path / "doc.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with open(pdf_path, "wb") as f:
        writer.write(f)

    msg = "Summarize this [attached: doc.pdf]"
    out = await _prepare_attachments(msg)
    # The reference is rewritten to mention the sidecar path.
    assert "doc.pdf.txt" in out or "extraction failed" in out


async def test_prepare_attachments_handles_missing_pdf(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    msg = "See [attached: ghost.pdf]"
    out = await _prepare_attachments(msg)
    # Missing file: reference stays as-is (no crash, no rewrite).
    assert out == msg


async def test_prepare_attachments_no_op_when_no_refs(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    assert await _prepare_attachments("plain question") == "plain question"


# ---------------------------------------------------------------------------
# Compile-time: images expanded only for the latest user turn, vision only
# ---------------------------------------------------------------------------


def test_extract_attached_filenames():
    text = "before [attached: a.jpg] middle [attached: b.pdf — text at b.pdf.txt] end"
    names = _extract_attached_filenames(text)
    assert "a.jpg" in names
    assert "b.pdf" in names


def test_expand_user_message_with_images_inlines_b64(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    img = tmp_path / "pic.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 500)

    blocks, spent = _expand_user_message_with_images(
        "Look [attached: pic.png]",
        budget=10_000_000,
    )
    assert isinstance(blocks, list)
    assert blocks[0]["type"] == "text"
    image_blocks = [b for b in blocks if b.get("type") == "image_url"]
    assert len(image_blocks) == 1
    assert image_blocks[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert spent > 0


def test_expand_honors_budget(tmp_path, monkeypatch):
    """When payload exceeds the cap, image falls back to text marker."""
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    img = tmp_path / "big.jpg"
    img.write_bytes(b"\xff\xd8" + b"y" * 10_000)
    blocks, spent = _expand_user_message_with_images(
        "See [attached: big.jpg]",
        budget=100,
    )
    # Only the text block, no image_url
    assert all(b.get("type") == "text" for b in blocks)
    assert spent == 0


# ---------------------------------------------------------------------------
# Legacy DB rows: collapse JSON multimodal blobs back to text markers
# ---------------------------------------------------------------------------


def test_legacy_multimodal_to_text_collapses_blob():
    legacy = json.dumps(
        [
            {"type": "text", "text": "hello"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA..."}, "_filename": "cat.jpg"},
        ]
    )
    out = _legacy_multimodal_to_text(legacy)
    assert "hello" in out
    assert "[image: cat.jpg]" in out
    assert "base64" not in out
    assert "data:image" not in out


def test_legacy_multimodal_to_text_passthrough_for_plain():
    assert _legacy_multimodal_to_text("plain text") == "plain text"


def test_legacy_multimodal_to_text_tolerates_bad_json():
    # Starts with [{ but isn't valid JSON — must not raise.
    assert _legacy_multimodal_to_text("[{broken") == "[{broken"


def test_legacy_multimodal_to_text_tolerates_leading_whitespace():
    """Regression: a legacy row starting with `"[ {"` (space after bracket)
    used to fall through the prefix test, leaving the base64 blob inlined.
    Now it should collapse to text markers just like `"[{"`."""
    legacy = (
        "[ "
        + json.dumps(
            [
                {"type": "text", "text": "hello"},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA..."}, "_filename": "cat.jpg"},
            ]
        )[1:]
    )  # swap the first `[` for `[ `
    # json.loads handles the space fine; the prefix check used to not.
    out = _legacy_multimodal_to_text(legacy)
    assert "[image: cat.jpg]" in out
    assert "base64" not in out


# ---------------------------------------------------------------------------
# Path-traversal rejection (C2): attachment refs pointing outside workspace
# must be refused, not read-through-resolve.
# ---------------------------------------------------------------------------


def test_expand_images_rejects_path_traversal(tmp_path, monkeypatch):
    """`[attached: ../outside.jpg]` must not read outside the workspace,
    even if the target exists and has an image extension."""
    ws = tmp_path / "workspace"
    ws.mkdir(exist_ok=True)
    monkeypatch.setattr("config.settings.workspace_dir", str(ws))
    monkeypatch.setattr("config.settings.skills_dir", str(tmp_path / "skills"))
    # Create a "sensitive" file OUTSIDE the workspace.
    evil = tmp_path / "outside.jpg"
    evil.write_bytes(b"\xff\xd8" + b"x" * 400)

    text = "look at [attached: ../outside.jpg]"
    blocks, spent = _expand_user_message_with_images(text, budget=10_000_000)
    # Only the text block, no image block. Nothing inlined.
    assert len(blocks) == 1
    assert blocks[0]["type"] == "text"
    assert spent == 0


async def test_prepare_attachments_rejects_pdf_traversal(tmp_path, monkeypatch):
    """`[attached: ../escape.pdf]` must not land the sidecar outside workspace."""
    from pypdf import PdfWriter

    ws = tmp_path / "workspace"
    ws.mkdir(exist_ok=True)
    monkeypatch.setattr("config.settings.workspace_dir", str(ws))
    monkeypatch.setattr("config.settings.skills_dir", str(tmp_path / "skills"))
    # Put a PDF OUTSIDE the workspace.
    outside_pdf = tmp_path / "escape.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with open(outside_pdf, "wb") as f:
        writer.write(f)

    msg = "grab [attached: ../escape.pdf]"
    out = await _prepare_attachments(msg)
    # Reference left intact; no sidecar written outside workspace.
    assert out == msg
    assert not (tmp_path / "escape.pdf.txt").exists()
    # And not inside the workspace either (escape.pdf isn't in workspace).
    assert not (ws / "escape.pdf.txt").exists()


# ---------------------------------------------------------------------------
# Integration: compile_context strips images from history, expands latest
# ---------------------------------------------------------------------------


def test_compile_context_only_expands_latest_user_turn(tmp_path):
    """Only the most recent user message gets image blocks, even with
    [attached:] refs on older turns."""
    ws = Path(tmp_path) / "workspace"
    ws.mkdir(exist_ok=True)
    (ws / "old.jpg").write_bytes(b"\xff\xd8" + b"o" * 400)
    (ws / "new.jpg").write_bytes(b"\xff\xd8" + b"n" * 400)

    from core.context.compiler import compile_context
    from db import models as db_models

    sid = db_models.create_session(title="attach-latest")
    db_models.add_message(sid, "user", "old [attached: old.jpg]")
    db_models.add_message(sid, "assistant", "ok")
    db_models.add_message(sid, "user", "new [attached: new.jpg]")

    payload = compile_context(sid, supports_vision=True)
    user_msgs = [m for m in payload.messages if m["role"] == "user"]
    assert len(user_msgs) == 2
    # Older user turn: plain text (no image_url block).
    older = user_msgs[0]["content"]
    assert isinstance(older, str)
    assert "[attached: old.jpg]" in older
    # Newest user turn: multimodal list with one image_url.
    newest = user_msgs[1]["content"]
    assert isinstance(newest, list)
    kinds = [b.get("type") for b in newest]
    assert kinds.count("image_url") == 1


def test_compile_context_non_vision_skips_expansion(tmp_path):
    ws = Path(tmp_path) / "workspace"
    ws.mkdir(exist_ok=True)
    (ws / "pic.png").write_bytes(b"\x89PNG" + b"\x00" * 400)

    from core.context.compiler import compile_context
    from db import models as db_models

    sid = db_models.create_session(title="no-vision")
    db_models.add_message(sid, "user", "hi [attached: pic.png]")

    payload = compile_context(sid, supports_vision=False)
    user_msgs = [m for m in payload.messages if m["role"] == "user"]
    assert isinstance(user_msgs[0]["content"], str)
    assert "[attached: pic.png]" in user_msgs[0]["content"]


# ---------------------------------------------------------------------------
# Scout regression: legacy base64 rows don't blow up context_utilization
# ---------------------------------------------------------------------------


def test_build_session_brief_handles_legacy_blob(tmp_path, monkeypatch):
    """A legacy DB row with a 1MB JSON blob must not tokenize to millions."""
    from core.scout.runner import build_session_brief
    from db import models as db_models

    # Synthesize a legacy inlined row
    fake_b64 = "A" * 800_000  # well over any sane message budget
    legacy = json.dumps(
        [
            {"type": "text", "text": "Read files"},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{fake_b64}"}, "_filename": "huge.jpg"},
        ]
    )

    sid = db_models.create_session(title="legacy-brief")
    db_models.add_message(sid, "user", legacy)

    brief = build_session_brief(sid, context_budget=32_000)
    # Without the fix this collapses to utilization >> 1.0 (clamped to 1.0
    # by min()), which still broke the UI display. The real proof is that
    # the recent_messages preview shows a text marker, not base64.
    assert any("[image: huge.jpg]" in rm for rm in brief.recent_messages)
    assert "base64" not in " ".join(brief.recent_messages)
    # And utilization should be comfortably small — the collapsed text is
    # tiny.
    assert brief.context_utilization < 0.1


# ---------------------------------------------------------------------------
# Model capability block + vision override
# ---------------------------------------------------------------------------


def test_capability_block_vision_enabled():
    from core.context.compiler import _build_model_capability_block

    block = _build_model_capability_block("qwen3.6:35b-a3b-q8_0", True)
    assert "qwen3.6:35b-a3b-q8_0" in block
    assert "Vision: ENABLED" in block
    assert "analyze them directly" in block


def test_capability_block_vision_disabled():
    from core.context.compiler import _build_model_capability_block

    block = _build_model_capability_block("tiny-text-model", False)
    assert "Vision: DISABLED" in block
    assert "call_model" in block


def test_compile_context_includes_capability_block(tmp_path):
    """System prompt must surface the active model and vision state."""
    from core.context.compiler import compile_context
    from db import models as db_models

    sid = db_models.create_session(title="cap-block")
    db_models.add_message(sid, "user", "hi")
    payload = compile_context(sid, supports_vision=True, model_name="qwen-demo:8b")
    system = payload.messages[0]["content"]
    assert "[ACTIVE MODEL]" in system
    assert "qwen-demo:8b" in system
    assert "Vision: ENABLED" in system


def test_vision_model_overrides_forces_supports_vision(monkeypatch):
    """The override list must flip supports_vision=True regardless of modelfile keys."""
    from core.llm.providers.ollama import OllamaProvider

    monkeypatch.setattr("config.settings.vision_model_overrides", ["qwen3.6:35b-a3b-q8_0"])

    async def fake_post(self, url, json=None, **kw):  # noqa: ARG001
        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "model_info": {"general.architecture": "qwen"},  # no vision keys
                    "details": {},
                }

        return _Resp()

    # Exercise the detection branch directly via get_model_info with a stub client.
    from unittest.mock import AsyncMock, MagicMock

    import httpx  # noqa: F401

    provider = OllamaProvider.__new__(OllamaProvider)
    provider.name = "ollama"
    provider._vision_cache = {}

    class _Cfg:
        base_url = "http://localhost:11434/v1"

    provider._config = _Cfg()

    fake_resp = MagicMock()
    fake_resp.raise_for_status = MagicMock()
    fake_resp.json = MagicMock(
        return_value={
            "model_info": {"general.architecture": "qwen"},
            "details": {},
        }
    )
    fake_client = MagicMock()
    fake_client.post = AsyncMock(return_value=fake_resp)
    provider._get_quick_client = lambda: fake_client
    provider._model = lambda m: m

    import asyncio

    info = asyncio.run(provider.get_model_info("qwen3.6:35b-a3b-q8_0"))
    assert info.supports_vision is True


def test_vision_key_scan_without_override(monkeypatch):
    """Without override, a model whose modelfile has no vision keys stays text-only."""
    from unittest.mock import AsyncMock, MagicMock

    from core.llm.providers.ollama import OllamaProvider

    monkeypatch.setattr("config.settings.vision_model_overrides", [])

    provider = OllamaProvider.__new__(OllamaProvider)
    provider.name = "ollama"
    provider._vision_cache = {}

    class _Cfg:
        base_url = "http://localhost:11434/v1"

    provider._config = _Cfg()

    fake_resp = MagicMock()
    fake_resp.raise_for_status = MagicMock()
    fake_resp.json = MagicMock(
        return_value={
            "model_info": {"general.architecture": "llama"},
            "details": {},
        }
    )
    fake_client = MagicMock()
    fake_client.post = AsyncMock(return_value=fake_resp)
    provider._get_quick_client = lambda: fake_client
    provider._model = lambda m: m

    import asyncio

    info = asyncio.run(provider.get_model_info("llama3:8b"))
    assert info.supports_vision is False
