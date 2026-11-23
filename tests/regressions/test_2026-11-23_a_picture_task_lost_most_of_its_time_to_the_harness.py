"""A picture task took 98 minutes, most of it lost to the harness (2026-11-23).

One session read 30 address cards from a phone video. Review of its steps:

- Five view_image calls in one round showed the last picture only. Each call
  still answered "Image queued", so the agent described pictures it never saw.
- A second look at a picture was refused as a repeated call, though a picture
  stays visible for one round only.
- call_model ran one call at a time: three calls that each timed out at 120 s
  held the round for six minutes. Its timeout error read as "try again".
- call_model sent pictures at full size; one provider answered 400.
- Three workers ran on a model that needed about 20 minutes per reply. The
  status line said "processing | idle 322s" and nothing about the model, and
  the parent waited 36 minutes for nothing.
"""

from __future__ import annotations

import base64
import io
from unittest.mock import MagicMock

import pytest

from config import settings
from core.tools.builtin.view_image import view_image
from db import models as db


@pytest.fixture()
def pictures():
    from core.tools.paths import workspace

    root = workspace()
    root.mkdir(parents=True, exist_ok=True)
    made = []
    for i in range(1, 6):
        p = root / f"card_probe_{i}.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 64)
        made.append(p)
    yield made
    for p in made:
        p.unlink(missing_ok=True)


def _user_notes(sid):
    return [m["content"] for m in db.get_messages(sid) if m["role"] == "user"]


# --- view_image -----------------------------------------------------------------


def test_view_image_calls_of_one_round_share_one_note(pictures, monkeypatch):
    monkeypatch.setattr(settings, "image_max_per_prompt", 2)
    sid = db.create_session(title="view-batch")
    first = view_image(str(pictures[0]), _context={"session_id": sid})
    second = view_image(str(pictures[1]), _context={"session_id": sid})
    assert "picture 1 of at most 2" in first and "picture 2 of at most 2" in second
    notes = _user_notes(sid)
    assert len(notes) == 1
    assert f"[attached: {pictures[0]}]" in notes[0] and f"[attached: {pictures[1]}]" in notes[0]


def test_a_picture_over_the_round_limit_is_refused_in_plain_words(pictures, monkeypatch):
    monkeypatch.setattr(settings, "image_max_per_prompt", 2)
    sid = db.create_session(title="view-cap")
    for p in pictures[:2]:
        view_image(str(p), _context={"session_id": sid})
    third = view_image(str(pictures[2]), _context={"session_id": sid})
    assert third.startswith("Error:") and "NOT queued" in third
    assert str(pictures[2]) not in _user_notes(sid)[0]


def test_the_round_limit_without_a_server_limit_is_four(pictures, monkeypatch):
    monkeypatch.setattr(settings, "image_max_per_prompt", 0)
    sid = db.create_session(title="view-default-cap")
    answers = [view_image(str(p), _context={"session_id": sid}) for p in pictures]
    assert [a.startswith("Error:") for a in answers] == [False, False, False, False, True]


def test_a_new_round_starts_a_new_note(pictures, monkeypatch):
    monkeypatch.setattr(settings, "image_max_per_prompt", 2)
    sid = db.create_session(title="view-rounds")
    view_image(str(pictures[0]), _context={"session_id": sid})
    db.add_message(sid, "tool", "Image queued")
    db.add_message(sid, "assistant", "I see the first card.")
    view_image(str(pictures[1]), _context={"session_id": sid})
    notes = _user_notes(sid)
    assert len(notes) == 2 and str(pictures[0]) not in notes[1]


def test_the_shared_note_reaches_the_model_as_two_pictures(tmp_path, monkeypatch):
    from PIL import Image

    from core.context.compiler import _expand_user_message_with_attachments

    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    monkeypatch.setattr(settings, "image_max_per_prompt", 2)
    for name in ("a.png", "b.png"):
        Image.new("RGB", (32, 32), (0, 0, 0)).save(tmp_path / name)
    note = "[view_image] note\n[attached: a.png]\n[attached: b.png]"
    blocks, _ = _expand_user_message_with_attachments(note, 32 * 1024 * 1024)
    assert [b.get("_filename") for b in blocks[1:]] == ["a.png", "b.png"]


def test_a_second_look_at_a_picture_is_not_a_repeated_call():
    from core.agent import _CROSS_ROUND_DEDUP_EXCLUDED

    assert "view_image" in _CROSS_ROUND_DEDUP_EXCLUDED


# --- call_model -----------------------------------------------------------------


def test_call_model_runs_in_parallel():
    from core.extensions import model_mgmt

    reg = MagicMock()
    model_mgmt.register(reg)
    by_name = {c.kwargs["name"]: c.kwargs for c in reg.register.call_args_list}
    assert by_name["call_model"]["parallel_safe"] is True


def test_a_timeout_says_to_use_another_model():
    from core.extensions.model_mgmt import _timeout_hint

    hint = _timeout_hint("calling slow-model failed — TimeoutError", "slow-model")
    assert "slow-model" in hint and "different model" in hint
    assert _timeout_hint("calling m returned an empty response", "m") == ""


def test_call_model_fits_a_large_picture(tmp_path, monkeypatch):
    import asyncio
    import threading

    from PIL import Image

    import core.llm.client as llm_client
    from core.extensions.model_mgmt import call_model

    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    monkeypatch.setattr(settings, "image_max_edge", 1000)
    Image.new("RGB", (400, 6000), (255, 255, 255)).save(tmp_path / "sheet.png")
    sent = []
    fake = MagicMock()
    fake.router.registry.resolve_model_id = lambda m: m
    fake.router.registry.get_model_info = lambda m: MagicMock()

    async def chat(messages, model="", max_tokens=0):
        sent.append(messages)
        resp = MagicMock()
        resp.content, resp.model, resp.provider = "read", model, "test"
        return resp

    fake.chat = chat
    monkeypatch.setattr(llm_client, "get_llm_client", lambda: fake)
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    try:
        out = call_model("a/vision", "read it", image_path="sheet.png", _context={"_loop": loop})
    finally:
        loop.call_soon_threadsafe(loop.stop)
    assert "read" in out
    url = sent[0][-1]["content"][1]["image_url"]["url"]
    img = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
    assert max(img.size) == 1000


# --- worker status ----------------------------------------------------------------


def _worker(model="", replies=0, age_s=400):
    from datetime import datetime, timedelta, timezone

    wid = db.create_session(title="slow worker", session_type="worker")
    for _ in range(replies):
        db.add_message(wid, "assistant", "step")
    obj = MagicMock()
    obj.model_override = model
    obj.all_processes = lambda: []
    row = {"model_override": model, "created_at": (datetime.now(timezone.utc) - timedelta(seconds=age_s)).isoformat()}
    return wid, obj, row


def test_status_names_a_model_that_has_not_answered_and_says_what_to_do():
    from core.extensions.orchestration import _model_progress

    wid, obj, row = _worker(model="big-vision:32b", replies=0, age_s=400)
    text = " | ".join(_model_progress(wid, obj, row, idle=322))
    assert "model big-vision:32b" in text and "0 model replies in" in text
    assert "SLOW MODEL" in text and "cancel_worker" in text


def test_status_flags_a_model_that_answers_once_in_twenty_minutes():
    from core.extensions.orchestration import _model_progress

    wid, obj, row = _worker(model="big-vision:32b", replies=2, age_s=2400)
    assert "SLOW MODEL" in " | ".join(_model_progress(wid, obj, row, idle=600))


def test_status_of_a_healthy_worker_has_no_warning():
    from core.extensions.orchestration import _model_progress

    wid, obj, row = _worker(model="", replies=6, age_s=120)
    text = " | ".join(_model_progress(wid, obj, row, idle=4))
    assert text.startswith("6 model replies in") and "SLOW" not in text and "model " not in text.split("6 model")[0]
