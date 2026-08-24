"""Deferred reflect graded 'retry' and nothing acted on it (F14, field case
17683100ecf8).

Deferred mode is observe-only by design — but on 2026-08-24 it produced a
correct, concrete 3-round finish strategy six minutes after the turn ended,
and that strategy sat unread in post_mortems while the session idled. The
harness knew how to finish and had no way to say so. Non-pass deferred
verdicts now raise a notification carrying the strategy, so a human can
trigger the finish with one reply.
"""

from types import SimpleNamespace

from db import models as db
from sessions.hooks import _deferred_verdict_notification


class _StubManager:
    def __init__(self):
        self.broadcasts = []

    def broadcast(self, n):
        self.broadcasts.append(n)


def _stub_manager(monkeypatch):
    mgr = _StubManager()
    monkeypatch.setattr("sessions.manager.get_manager", lambda: mgr)
    return mgr


def _result(verdict, strategy="", missing="", reasoning="r"):
    return SimpleNamespace(verdict=verdict, strategy=strategy, missing=missing, reasoning=reasoning)


def test_retry_verdict_raises_a_notification_with_the_strategy(monkeypatch):
    mgr = _stub_manager(monkeypatch)
    sid = db.create_session()
    _deferred_verdict_notification(sid, _result("retry", strategy="replay move (61,2) and assert done"))

    notes = [n for n in db.get_notifications() if n.get("session_id") == sid]
    assert len(notes) == 1
    assert "retry" in notes[0]["title"]
    assert "replay move (61,2) and assert done" in notes[0]["body"]
    # observe-only nature is stated so nobody thinks a retry already ran
    assert "no retry ran" in notes[0]["body"]
    assert len(mgr.broadcasts) == 1


def test_pass_verdict_stays_silent(monkeypatch):
    _stub_manager(monkeypatch)
    sid = db.create_session()
    _deferred_verdict_notification(sid, _result("pass", strategy="irrelevant"))
    assert [n for n in db.get_notifications() if n.get("session_id") == sid] == []


def test_escalate_falls_back_to_missing_when_no_strategy(monkeypatch):
    _stub_manager(monkeypatch)
    sid = db.create_session()
    _deferred_verdict_notification(sid, _result("escalate", missing="round_ceiling on consecutive attempts"))
    notes = [n for n in db.get_notifications() if n.get("session_id") == sid]
    assert len(notes) == 1
    assert "round_ceiling on consecutive attempts" in notes[0]["body"]


def test_broadcast_failure_never_raises(monkeypatch):
    def _boom():
        raise RuntimeError("no manager in this process")

    monkeypatch.setattr("sessions.manager.get_manager", _boom)
    sid = db.create_session()
    # Must not propagate — a failed notification costs a ping, not the grade.
    _deferred_verdict_notification(sid, _result("retry", strategy="s"))
