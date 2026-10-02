"""Regression tests for the 2026-10 notification-noise audit, phase 0.

In the 17 days before this fix the box raised ~104 bell items and Calvin
dismissed every one; ~95% were receipts nobody needed, and 17 of the 19
high-urgency pushes were reflect verdicts on *canary* sessions (synthetic
test runs the Canary tab already records). Pinned here:

- canary and worker sessions raise no reflect / timeout / stream-error alert
  (a normal session still does)
- a skill-verify rollback leaves ONE bell item, not two
- a maintenance sweep whose only change is `skills_changed` stays silent
  (the skill apply already announced it)
- ask_user statements never reach the event bus (no push / webhook)
"""

from __future__ import annotations

import pytest

from db import models as db
from tests.test_canary_isolation_hardening import _auto_applied, _skill_env


class _Recorder:
    def __init__(self):
        self.broadcasts = []
        self.emits = []

    def broadcast(self, event):
        self.broadcasts.append(event)

    def emit(self, *args, **kwargs):
        self.emits.append(args)


@pytest.fixture
def wired(monkeypatch):
    rec = _Recorder()
    bus = _Recorder()
    monkeypatch.setattr("sessions.manager.get_manager", lambda: rec)
    monkeypatch.setattr("core.events.get_event_bus", lambda: bus)
    return rec, bus


@pytest.mark.parametrize("session_type", ["canary", "worker"])
def test_reflect_alert_is_silent_for_synthetic_sessions(wired, session_type):
    from sessions.hooks import _broadcast_reflect_notification

    rec, bus = wired
    _broadcast_reflect_notification(
        "s1", {"title": "Canary: x", "session_type": session_type}, "Needs attention", "why"
    )
    assert db.get_notifications() == []
    assert rec.broadcasts == [] and bus.emits == []


def test_reflect_alert_still_fires_for_a_normal_session(wired):
    from sessions.hooks import _broadcast_reflect_notification

    rec, bus = wired
    _broadcast_reflect_notification("s1", {"title": "My chat", "session_type": "normal"}, "Needs attention", "why")
    rows = db.get_notifications()
    assert len(rows) == 1 and rows[0]["urgency"] == "high"
    assert len(rec.broadcasts) == 1 and len(bus.emits) == 1


def test_skill_verify_rollback_leaves_one_notice(tmp_path, monkeypatch):
    from core.skills.proposals import restore_skill_backup

    _skill_env(tmp_path, monkeypatch)
    pid = _auto_applied("heal-me")
    restore_skill_backup(pid, actor="skill-verify")
    # The log view holds every row, so this proves none was written at all.
    assert not [n for n in db.list_notifications("log") if "rolled back" in n["title"].lower()]


def test_user_rollback_still_notifies(tmp_path, monkeypatch):
    from core.skills.proposals import restore_skill_backup

    _skill_env(tmp_path, monkeypatch)
    pid = _auto_applied("heal-me")
    restore_skill_backup(pid)
    # skills.rolled_back is log tier (the user did it, or the Skills tab shows it).
    rows = [n for n in db.list_notifications("log") if "rolled back" in n["title"].lower()]
    assert rows and rows[0]["category"] == "skills.rolled_back"


def test_skills_changed_alone_does_not_raise_a_maintenance_notice(tmp_path, monkeypatch):
    from core.canary import maintain

    monkeypatch.setattr("config.settings.canary_enabled", True)
    monkeypatch.setattr("config.settings.canary_auto_maintain", True)
    monkeypatch.setattr("core.canary.skill_verify.sync_and_detect", lambda **kw: {"skills_changed": ["some-skill"]})
    stats = maintain.run_maintenance(base=tmp_path / "canaries")
    assert stats["skills_changed"] == ["some-skill"]  # still reported to the caller / log
    assert not [n for n in db.get_notifications() if n["title"] == "Canary suite auto-maintenance"]


def test_ask_user_statement_skips_the_event_bus(monkeypatch, wired):
    from core.tools.builtin.dialog_tools import ask_user
    from tests.test_core_tools import _dialog_session

    rec, bus = wired
    sid, _ = _dialog_session(monkeypatch)
    ask_user(question="FYI, retrying now.", question_type="statement", _context={"session_id": sid})
    assert bus.emits == []


def test_ask_user_question_still_reaches_the_event_bus(monkeypatch, wired):
    from core.tools.builtin.dialog_tools import ask_user
    from tests.test_core_tools import _dialog_session

    rec, bus = wired
    sid, _ = _dialog_session(monkeypatch)
    ask_user(question="Which device?", _context={"session_id": sid})
    assert len(bus.emits) == 1
