"""Session / job / agent-tool producers route through core.notices (2026-10).

Each producer used to write its own bell row and hand-roll SSE + push, each
choosing its own urgency. Pinned here: the category each one uses and the
tier that results per session type —

- reflect "needs attention": normal = interrupt, cron = bell, canary/worker = nothing
- reflect deferred follow-up (answer already delivered): bell
- LLM time-budget timeout and stream error: normal = interrupt, canary/worker = nothing
- a failed job interrupts; a job test pass is a log line, a failed test a bell item
- notify_user: attended = bell; background session or urgency=high = interrupt;
  the 4th push in an hour from one session is downgraded to bell
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from db import models as db


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
    rec, bus = _Recorder(), _Recorder()
    monkeypatch.setattr("sessions.manager.get_manager", lambda: rec)
    monkeypatch.setattr("core.events.get_event_bus", lambda: bus)
    return rec, bus


def _all_rows():
    # The log view holds every row in every tier; the bell view only open interrupt/bell rows.
    return db.list_notifications("log", limit=500)


def _session(session_type: str) -> str:
    return db.create_session(title=f"A {session_type} chat", session_type=session_type)


# --- reflect ----------------------------------------------------------------


@pytest.mark.parametrize(
    "session_type,tier", [("normal", "interrupt"), ("cron", "bell"), ("canary", None), ("worker", None)]
)
def test_reflect_attention_tier_by_session_type(wired, session_type, tier):
    from sessions.hooks import _broadcast_reflect_notification

    rec, bus = wired
    sid = _session(session_type)
    _broadcast_reflect_notification(sid, {"title": "T", "session_type": session_type}, "Needs attention", "why")
    rows = _all_rows()
    if tier is None:
        assert rows == [] and rec.broadcasts == [] and bus.emits == []
        return
    assert [(r["category"], r["tier"]) for r in rows] == [("sessions.reflect_attention", tier)]
    assert rows[0]["title"] == "T: Needs attention"
    assert len(bus.emits) == (1 if tier == "interrupt" else 0)


def test_deferred_verdict_is_a_quiet_followup(wired):
    from sessions.hooks import _deferred_verdict_notification

    rec, bus = wired
    sid = _session("normal")
    _deferred_verdict_notification(sid, SimpleNamespace(verdict="retry", strategy="finish x", missing="", reasoning=""))
    rows = _all_rows()
    assert [(r["category"], r["tier"]) for r in rows] == [("sessions.reflect_followup", "bell")]
    assert "graded 'retry' after delivery" in rows[0]["title"]
    assert bus.emits == []  # a bell item never pushes


# --- timeout / stream error -------------------------------------------------


@pytest.mark.parametrize("session_type,tier", [("normal", "interrupt"), ("cron", "bell"), ("canary", None)])
def test_session_timeout_tier(wired, session_type, tier):
    from sessions.manager import _broadcast_session_timeout_notification

    sid = _session(session_type)
    _broadcast_session_timeout_notification(SimpleNamespace(session_id=sid, session_type=session_type))
    got = [(r["category"], r["tier"]) for r in _all_rows()]
    assert got == ([] if tier is None else [("sessions.timeout", tier)])


@pytest.mark.parametrize("session_type,tier", [("normal", "interrupt"), ("worker", None), ("canary", None)])
def test_stream_error_tier(wired, session_type, tier):
    from core.agent import _end_turn_on_stream_error

    sid = _session(session_type)
    sess = SimpleNamespace(session_type=session_type, emit_event=lambda e: None, error=None, termination_reason="")
    asyncio.run(
        _end_turn_on_stream_error(session=sess, session_id=sid, error="boom", partial_content="", save_turn_msg=None)
    )
    rows = _all_rows()
    assert [(r["category"], r["tier"]) for r in rows] == ([] if tier is None else [("sessions.stream_error", tier)])
    assert sess.error == "boom"  # the turn still errors regardless of the notice


# --- jobs ---------------------------------------------------------------------


def test_job_failure_interrupts(wired):
    from core.extensions.scheduling import _notify_job_failure

    rec, bus = wired
    _notify_job_failure(None, None, "nightly", "", "x" * 500)
    rows = _all_rows()
    assert [(r["category"], r["tier"], r["title"]) for r in rows] == [
        ("jobs.failed", "interrupt", "Job failed: nightly")
    ]
    assert len(rows[0]["body"]) == 200
    assert len(bus.emits) == 1


@pytest.mark.parametrize("ok,category,tier", [(True, "jobs.test_passed", "log"), (False, "jobs.test_failed", "bell")])
def test_job_test_outcome_tier(wired, monkeypatch, ok, category, tier):
    from api.routers import jobs as jobs_router

    async def fake_run(name):
        return {"ok": ok, "error": "" if ok else "bad", "answer_preview": "fine"}

    monkeypatch.setattr("core.extensions.scheduling._read_jobs_json", lambda: [{"name": "nightly"}])
    monkeypatch.setattr("core.extensions.scheduling.run_job_test", fake_run)

    async def go():
        await jobs_router.test_job_endpoint("nightly")
        await asyncio.gather(*list(jobs_router._bg_tasks))

    asyncio.run(go())
    rows = _all_rows()
    assert [(r["category"], r["tier"]) for r in rows] == [(category, tier)]
    # Only the bell view shows the failure; a pass is activity-log only.
    assert len(db.get_notifications()) == (0 if ok else 1)


# --- notify_user (D8) -----------------------------------------------------------


@pytest.fixture
def fresh_caps(monkeypatch):
    from core.tools.builtin import dialog_tools

    monkeypatch.setattr(dialog_tools, "_urgent_sent", {})
    return dialog_tools


@pytest.mark.parametrize(
    "session_type,urgency,category",
    [
        ("normal", "normal", "agent.notify_user"),
        ("normal", "high", "agent.notify_user_urgent"),
        ("normal", "urgent", "agent.notify_user_urgent"),
        ("cron", "normal", "agent.notify_user_urgent"),
        ("snooze", "low", "agent.notify_user_urgent"),
        ("rlm", "normal", "agent.notify_user_urgent"),
    ],
)
def test_notify_user_category(wired, fresh_caps, session_type, urgency, category):
    sid = _session(session_type)
    out = fresh_caps.notify_user(title="Done", body="b", urgency=urgency, _context={"session_id": sid})
    assert out.startswith("Notification broadcast to")
    rows = _all_rows()
    want_tier = "interrupt" if category.endswith("_urgent") else "bell"
    assert [(r["category"], r["tier"]) for r in rows] == [(category, want_tier)]


def test_notify_user_fourth_push_in_an_hour_goes_quiet(wired, fresh_caps):
    rec, bus = wired
    sid = _session("cron")
    outs = [fresh_caps.notify_user(title=f"n{i}", _context={"session_id": sid}) for i in range(5)]
    tiers = [r["tier"] for r in sorted(_all_rows(), key=lambda r: r["title"])]
    assert tiers == ["interrupt"] * 3 + ["bell"] * 2
    assert len(bus.emits) == 3
    assert all("quietly" not in o for o in outs[:3]) and all("quietly" in o for o in outs[3:])
    # Another session has its own allowance.
    other = _session("cron")
    fresh_caps.notify_user(title="other", _context={"session_id": other})
    assert len(bus.emits) == 4


def test_notify_user_cap_table_is_bounded(fresh_caps, monkeypatch):
    monkeypatch.setattr(fresh_caps, "_URGENT_MAX_SESSIONS", 4)
    for i in range(20):
        assert fresh_caps._take_urgent_slot(f"s{i}")
    assert len(fresh_caps._urgent_sent) <= 4
