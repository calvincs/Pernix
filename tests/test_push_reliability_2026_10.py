"""Regression tests for iOS Web Push reliability (2026-10).

The box's iPhone subscription was deleted as "stale" ten times in seventeen
days, each within a second of a push. send_push() lumped 401/403 in with 410,
so Apple refusing OUR VAPID credentials (subject `mailto:admin@localhost`)
destroyed a good subscription, and neither the status code nor any success
was ever logged.

Pinned here: only 404/410 delete; 401/403 keep the subscription, log the
status, and after three in a row raise one bell notice; success is counted
and logged at debug; a suspect vapid_subject warns once at dispatcher start;
network errors still propagate to the dispatcher's catch-all.
"""

from __future__ import annotations

import logging

import pytest
import pywebpush

from core import push
from core.notify import NotificationDispatcher
from db import models as db

APPLE = "https://web.push.apple.com/QGsecret-device-token"
SUB = {"endpoint": APPLE, "p256dh": "p", "auth": "a"}


class _Resp:
    def __init__(self, status: int, text: str = "", reason: str = ""):
        self.status_code = status
        self.text = text
        self.reason = reason


def _fake_webpush(status: int, text: str = ""):
    def fake(**kwargs):
        if status > 202:
            raise pywebpush.WebPushException(f"Push failed: {status}", response=_Resp(status, text))
        return _Resp(status)

    return fake


@pytest.fixture
def harness(monkeypatch):
    """Dispatcher with one Apple subscription and the DB writes captured."""
    deleted: list[str] = []
    notices: list[dict] = []
    monkeypatch.setattr(db, "get_push_subscriptions", lambda: [dict(SUB)])
    monkeypatch.setattr(db, "delete_push_subscription", lambda ep: deleted.append(ep))
    monkeypatch.setattr(db, "add_notification", lambda **kw: notices.append(kw) or "n1")
    monkeypatch.setattr("config.settings.push_urgency_floor", "normal")
    for k in push._STATS:
        monkeypatch.setitem(push._STATS, k, 0)

    def use(status: int, text: str = ""):
        monkeypatch.setattr(pywebpush, "webpush", _fake_webpush(status, text))

    return NotificationDispatcher(), use, deleted, notices


EVENT = {"type": "dialog.question", "session_title": "S", "question": "Which file?"}


@pytest.mark.parametrize("status", [404, 410])
async def test_gone_deletes_the_subscription(harness, status):
    d, use, deleted, notices = harness
    use(status)
    await d._send_web_push(EVENT)
    assert deleted == [APPLE]
    assert notices == []
    assert push.push_stats()["push_gone"] == 1


@pytest.mark.parametrize("status", [401, 403])
async def test_rejected_keeps_the_subscription_and_logs_the_status(harness, caplog, status):
    d, use, deleted, notices = harness
    use(status, '{"reason":"BadJwtToken"}')
    with caplog.at_level(logging.WARNING, logger="pernix.push"):
        await d._send_web_push(EVENT)
    assert deleted == [], "a 401/403 is our credentials being refused, not the phone being gone"
    msgs = [r.getMessage() for r in caplog.records if r.name == "pernix.push"]
    assert any(f"HTTP {status}" in m and "web.push.apple.com" in m and "BadJwtToken" in m for m in msgs)
    assert not any("QGsecret" in m for m in msgs), "the endpoint path is a device token and must not be logged"
    assert push.push_stats()["push_rejected"] == 1


async def test_three_rejects_raise_one_notice_and_success_resets(harness):
    d, use, deleted, notices = harness
    use(403, "BadJwtToken")
    for _ in range(2):
        await d._send_web_push(EVENT)
    assert notices == [], "two rejects can be a blip"
    await d._send_web_push(EVENT)
    assert len(notices) == 1
    n = notices[0]
    assert n["title"] == "Push rejected by web.push.apple.com"
    assert "HTTP 403" in n["body"] and "vapid_subject" in n["body"]
    assert n["dedup_key"] == "push_rejected:web.push.apple.com"
    for _ in range(5):
        await d._send_web_push(EVENT)
    assert len(notices) == 1, "once per process per endpoint"
    assert deleted == []

    use(201)
    await d._send_web_push(EVENT)
    assert APPLE not in d._push_rejects, "a success resets the consecutive count"
    use(403)
    await d._send_web_push(EVENT)
    assert d._push_rejects[APPLE] == 1


async def test_success_is_counted_and_logged_at_debug(harness, caplog):
    d, use, deleted, notices = harness
    use(201)
    with caplog.at_level(logging.DEBUG, logger="pernix.push"):
        await d._send_web_push(EVENT)
    assert push.push_stats()["push_sent_ok"] == 1
    recs = [r for r in caplog.records if r.name == "pernix.push"]
    assert any(r.levelno == logging.DEBUG and "web.push.apple.com" in r.getMessage() for r in recs)
    assert deleted == [] and notices == []


async def test_network_errors_still_propagate_to_the_dispatcher(harness, monkeypatch, caplog):
    d, use, deleted, notices = harness

    def boom(**kwargs):
        raise ConnectionError("network down")

    monkeypatch.setattr(pywebpush, "webpush", boom)
    with pytest.raises(ConnectionError):
        await push.send_push(SUB, "t", "b")
    with caplog.at_level(logging.WARNING, logger="pernix.notify"):
        await d._send_web_push(EVENT)
    assert deleted == [] and notices == []
    assert any("Web Push send failed" in r.getMessage() for r in caplog.records if r.name == "pernix.notify")
    assert push.push_stats()["push_failed"] == 2


async def test_other_http_errors_raise_as_before(harness):
    d, use, deleted, notices = harness
    use(500, "oops")
    with pytest.raises(pywebpush.WebPushException):
        await push.send_push(SUB, "t", "b")
    await d._send_web_push(EVENT)
    assert deleted == []


async def test_bool_returning_send_push_still_works(harness, monkeypatch):
    d, use, deleted, notices = harness

    async def legacy(sub, title, body, session_id=""):
        return False

    monkeypatch.setattr("core.push.send_push", legacy)
    await d._send_web_push(EVENT)
    assert deleted == [APPLE], "a bare False keeps its old meaning: gone"


@pytest.mark.parametrize(
    "subject,warns",
    [
        ("mailto:admin@localhost", True),
        ("", True),
        ("admin@example.com", True),
        ("https://LOCALHOST:8090", True),
        ("mailto:you@example.com", False),
        ("https://pernix.cc", False),
    ],
)
async def test_suspect_vapid_subject_warns_once_at_start(monkeypatch, caplog, subject, warns):
    monkeypatch.setattr("config.settings.vapid_private_key", "k")
    monkeypatch.setattr("config.settings.vapid_subject", subject)
    monkeypatch.setattr("config.settings.notify_webhook_url", "")
    d = NotificationDispatcher()
    with caplog.at_level(logging.WARNING, logger="pernix.notify"):
        d.start()
    try:
        hits = [r for r in caplog.records if r.name == "pernix.notify" and "vapid_subject" in r.getMessage()]
        assert len(hits) == (1 if warns else 0)
    finally:
        await d.stop()
