"""Regression tests for the 2026-09-05 notification storm.

The verify-canary sync rendered the canonical CANARY.md without the flags
maintenance owns, compared it byte-for-byte with the file on disk, and
rewrote it — un-parking the canary. Maintenance parked it again on the next
idle cycle and notified. The two writers traded the file, and the box got
"Canary suite auto-maintenance" every twenty minutes. The same comparison
re-materialised a verify canary the user had retired.

Pinned here: maintenance flags survive a sync; a retired verify canary stays
retired while its verify block is unchanged, and returns when it changes;
Web Push honours an urgency floor; deleting a session takes its feedback.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.canary.maintain import _rewrite_frontmatter, retire_canary, retired_dir
from core.canary.parser import load_canary
from core.canary.skill_verify import sync_and_detect, verify_canary_name
from db import models as db

SKILL_WITH_VERIFY = """---
name: {name}
description: a test skill
tags: [test]
verify:
  prompt: |
    Create out.txt containing {word}.
  gates:
    - name: out
      command: grep -qx {word} out.txt
---
Body of the skill.
"""


@pytest.fixture(autouse=True)
def _dirs(monkeypatch, tmp_path):
    monkeypatch.setattr("config.settings.skills_dir", str(tmp_path / "skills"))
    monkeypatch.setattr("config.settings.canaries_dir", str(tmp_path / "canaries"))
    monkeypatch.setattr("config.settings.canary_enabled", True)


def _mk_skill(name: str, word: str = "DONE") -> Path:
    from config import settings

    d = Path(settings.skills_dir) / name
    d.mkdir(parents=True, exist_ok=True)
    md = d / "SKILL.md"
    md.write_text(SKILL_WITH_VERIFY.format(name=name, word=word), encoding="utf-8")
    return md


def _cbase() -> Path:
    from config import settings

    return Path(settings.canaries_dir)


def test_a_parked_verify_canary_stays_parked_across_a_sync():
    _mk_skill("parky")
    sync_and_detect()
    cname = verify_canary_name("parky")
    c = load_canary(cname, base=_cbase())
    assert c is not None and not c.parked

    assert _rewrite_frontmatter(c.path, {"parked": True})
    assert load_canary(cname, base=_cbase()).parked is True

    stats = sync_and_detect()
    assert stats["verify_synced"] == [], "an unchanged verify block must not rewrite the file"
    assert load_canary(cname, base=_cbase()).parked is True, "the sync un-parked it"

    # And a real content change carries the flag over instead of dropping it.
    _mk_skill("parky", word="CHANGED")
    stats = sync_and_detect()
    assert stats["verify_synced"] == [cname]
    again = load_canary(cname, base=_cbase())
    assert again.parked is True
    assert "CHANGED" in again.prompt


def test_a_retired_verify_canary_stays_retired_until_its_block_changes():
    _mk_skill("retiree")
    sync_and_detect()
    cname = verify_canary_name("retiree")
    c = load_canary(cname, base=_cbase())
    assert retire_canary(c, _cbase(), reason="user retired it in the Canary tab", by="test")
    assert load_canary(cname, base=_cbase()) is None
    assert (retired_dir(_cbase()) / cname / "CANARY.md").exists()

    stats = sync_and_detect()
    assert stats["verify_synced"] == []
    assert load_canary(cname, base=_cbase()) is None, "the sync resurrected a retired canary"

    _mk_skill("retiree", word="NEWWORD")
    stats = sync_and_detect()
    assert stats["verify_synced"] == [cname], "a changed verify block is new content and comes back"
    assert "NEWWORD" in load_canary(cname, base=_cbase()).prompt


async def test_web_push_honours_the_urgency_floor(monkeypatch):
    from core.notify import NotificationDispatcher

    sent: list[tuple[str, str]] = []

    async def fake_send(sub, title, body, session_id=""):
        sent.append((title, body))
        return True

    monkeypatch.setattr("core.push.send_push", fake_send)
    monkeypatch.setattr(db, "get_push_subscriptions", lambda: [{"endpoint": "https://push.example/x"}])
    monkeypatch.setattr("config.settings.push_urgency_floor", "high")
    d = NotificationDispatcher()

    await d._send_web_push(
        {"type": "notification", "title": "Canary suite auto-maintenance", "body": "parked", "urgency": "normal"}
    )
    assert sent == [], "a normal notification must not buzz a phone above a 'high' floor"

    await d._send_web_push({"type": "notification", "title": "Tripwire", "body": "regression", "urgency": "high"})
    assert [t for t, _ in sent] == ["Tripwire"]

    await d._send_web_push({"type": "dialog.question", "session_title": "S", "question": "Which file?"})
    assert sent[-1][1] == "Which file?", "questions always push"

    monkeypatch.setattr("config.settings.push_urgency_floor", "normal")
    await d._send_web_push({"type": "notification", "title": "Normal again", "body": "x", "urgency": "normal"})
    assert sent[-1][0] == "Normal again", "the default floor keeps today's behaviour"


def test_deleting_a_session_takes_its_feedback_with_it():
    sid = db.create_session(title="fb")
    uid = db.add_message(sid, "user", "hi")
    aid = db.add_message(sid, "assistant", "hello", metadata='{"parent_user_msg_id": %d}' % uid)
    db.upsert_message_feedback(sid, aid, "up", "")
    assert db.list_message_feedback(sid)

    db.delete_session(sid)

    assert db.list_message_feedback(sid) == []
