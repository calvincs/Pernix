"""Regression tests for the 2026-09-05 notification storm.

The verify-canary sync rendered the canonical CANARY.md without the flags
maintenance owns, compared it byte-for-byte with the file on disk, and
rewrote it — un-parking the canary. Maintenance parked it again on the next
idle cycle and notified. The two writers traded the file, and the box got
"Canary suite auto-maintenance" every twenty minutes. The same comparison
re-materialised a verify canary the user had retired.

Pinned here: Web Push honours an urgency floor; deleting a session takes its
feedback. (The verify-canary sync tests went with core/canary/skill_verify.py
in the 3.2 surface prune.)
"""

from __future__ import annotations

from db import models as db


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
