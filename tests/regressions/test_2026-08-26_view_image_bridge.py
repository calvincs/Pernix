"""The view_image bridge: agents could render images but never look at them.

The compiler inlines image bytes only for [attached:] refs in the LATEST
user message. view_image validates a rendered file and injects a labelled
synthetic user note carrying that reference, so the existing expansion path
delivers real pixels next round. (ARC-2 field case: hours of ASCII
coordinate grinding while the answer was visible at a glance.)
"""

from __future__ import annotations

import pytest

from core.tools.builtin.view_image import view_image


@pytest.fixture()
def ws_png():
    from core.tools.paths import workspace

    root = workspace()
    root.mkdir(parents=True, exist_ok=True)
    p = root / "probe_render.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 64)
    yield p
    p.unlink(missing_ok=True)


def _last_user_message(sid):
    from db.database import connect_sessions

    with connect_sessions() as conn:
        row = conn.execute(
            "SELECT content FROM messages WHERE session_id=? AND role='user' ORDER BY id DESC LIMIT 1",
            (sid,),
        ).fetchone()
    return row[0] if row else None


def test_view_image_injects_expandable_user_note(ws_png):
    from core.context.compiler import _extract_attached_filenames
    from db import models as db

    sid = db.create_session(title="view-image")
    out = view_image(str(ws_png), _context={"session_id": sid})
    assert "Image queued" in out

    note = _last_user_message(sid)
    assert note is not None and note.startswith("[view_image]")
    assert "not a human message" in note, "reflect must never quote this as the user"
    # Round-trip: the compiler's own parser must find the reference.
    names = _extract_attached_filenames(note)
    assert names and names[0] == str(ws_png)


def test_view_image_rejects_non_image(ws_png):
    from db import models as db

    sid = db.create_session(title="view-image-ext")
    txt = ws_png.with_suffix(".txt")
    txt.write_text("not an image")
    try:
        out = view_image(str(txt), _context={"session_id": sid})
    finally:
        txt.unlink(missing_ok=True)
    assert out.startswith("Error:") and "not a supported image type" in out
    assert _last_user_message(sid) is None, "no note on rejection"


def test_view_image_rejects_missing_and_oversized(ws_png, monkeypatch):
    from db import models as db

    sid = db.create_session(title="view-image-missing")
    out = view_image(str(ws_png.with_name("nope.png")), _context={"session_id": sid})
    assert out.startswith("Error:") and "does not exist" in out

    monkeypatch.setattr("config.settings.max_inline_attach_bytes", 10)
    out = view_image(str(ws_png), _context={"session_id": sid})
    assert out.startswith("Error:") and "inline budget" in out
    assert _last_user_message(sid) is None


def test_view_image_requires_session_context(ws_png):
    out = view_image(str(ws_png), _context=None)
    assert out.startswith("Error:") and "session context" in out


def test_view_image_rejects_paths_outside_allowed_roots():
    # NOTE: tmp_path is NOT a valid traversal target here — in default mode
    # /tmp is an allowed read root (the 2026-08-25 paper-cuts change), so a
    # file under it is legitimately viewable. /etc never is.
    from db import models as db

    sid = db.create_session(title="view-image-traversal")
    out = view_image("/etc/shadow.png", _context={"session_id": sid})
    assert out.startswith("Error:")
    out2 = view_image("../../../../../../etc/shadow.png", _context={"session_id": sid})
    assert out2.startswith("Error:")
    assert _last_user_message(sid) is None


def test_view_image_is_registered():
    from core.tools.builtin.view_image import register

    captured = {}

    class _Reg:
        def register(self, **kw):
            captured.update(kw)

    register(_Reg())
    assert captured["name"] == "view_image"
    assert captured["safety_level"] == "safe"
    assert "path" in captured["parameters"]["properties"]
