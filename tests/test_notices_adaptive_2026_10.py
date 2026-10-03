"""Tiered notifications, stream 2b: the dream and fallback-burn
producers route through core/notices.py, and the one "N skill proposals
wait for your decision" bell row (core/skills/review.py).

What is pinned here: each producer's category (and so its tier), that every
pre-v42 dedup marker still suppresses a repeat on deploy day, and that
review.pending counts every pending skill proposal in ONE coalescing row.
(The adaptive engine and tripwire producers this file also covered went with
the adaptive layer in 3.2; the skill auto-apply producer went with the veto
window.)
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from db import models as db
from db.database import connect_sessions


class _Sink:
    def __init__(self):
        self.events = []

    def broadcast(self, event):
        self.events.append(event)

    emit = broadcast


@pytest.fixture(autouse=True)
def wires(monkeypatch):
    sse = _Sink()
    monkeypatch.setattr("sessions.manager.get_manager", lambda: SimpleNamespace(broadcast=sse.broadcast))
    monkeypatch.setattr("core.events.get_event_bus", lambda: _Sink())
    return sse


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _rows(category: str) -> list[dict]:
    return [n for n in db.list_notifications("log", limit=500) if n.get("category") == category]


def _bell(category: str) -> list[dict]:
    return [n for n in db.list_notifications("bell", limit=500) if n.get("category") == category]


# --- snooze producers ----------------------------------------------------------


async def test_fallback_burn_interrupts_once_a_day_across_the_deploy(monkeypatch, wires):
    from core.snooze import SnoozeRunner

    finding = {"model": "paid", "share": 0.9, "window_hours": 24, "tokens": 9, "total_tokens": 10, "calls": 3}
    monkeypatch.setattr("core.llm.burnwatch.check_fallback_burn", lambda: finding)
    runner = SnoozeRunner.__new__(SnoozeRunner)
    runner._stats = {}

    db.set_snooze_state(f"notify_dedup:{_today()}:fallback_burn", "1")
    await SnoozeRunner._fallback_burn_check(runner)
    assert _rows("system.fallback_burn") == []

    with connect_sessions() as conn:
        conn.execute("DELETE FROM snooze_state WHERE key LIKE 'notify_dedup:%'")
    await SnoozeRunner._fallback_burn_check(runner)
    rows = _bell("system.fallback_burn")
    assert len(rows) == 1 and rows[0]["tier"] == "interrupt"
    assert any(e.get("category") == "system.fallback_burn" for e in wires.events)


# --- dream -----------------------------------------------------------------------


def test_dream_corrections_are_log_tier_and_keep_their_daily_marker():
    from core.dream.promote import _announce_applied_corrections

    applied = [{"id": "abcdef123456", "kind": "contradiction", "written": ["dream"], "statement": "M1 is stale"}]
    _announce_applied_corrections(applied)
    _announce_applied_corrections(applied)
    rows = _rows("dream.corrections_applied")
    assert len(rows) == 1 and rows[0]["tier"] == "log"
    assert rows[0]["link"] == {"kind": "tab", "tab": "dream"}
    assert db.get_snooze_state(f"dream_corrections_notice:{_today()}")


def test_dream_stall_checks_are_log_tier():
    from core.dream import _STALL_DAYS, _check_promotion_health, _check_queue_health

    old = (datetime.now(timezone.utc) - timedelta(days=_STALL_DAYS + 1)).isoformat()
    _check_queue_health([{"created_at": old}])
    hid = db.add_dream_hypothesis("contradiction", "An old validated finding.", "[]")
    db.update_dream_hypothesis(hid, status="validated")
    with connect_sessions() as conn:
        conn.execute("UPDATE dream_hypotheses SET created_at = ? WHERE id = ?", (old, hid))
    _check_promotion_health()
    for cat in ("dream.queue_stalled", "dream.promotion_stalled"):
        rows = _rows(cat)
        assert len(rows) == 1 and rows[0]["tier"] == "log", cat


# --- review.pending ----------------------------------------------------------------


def _seed_review_items():
    # Leftover canary and adaptive proposal rows no longer count (both queues
    # were retired in 3.2; the table stays as history).
    with connect_sessions() as conn:
        for producer, payload in (("canary_propose", {"canary": {"name": "c1"}}), ("dream", [{"action": "create"}])):
            conn.execute(
                "INSERT INTO adaptive_proposals (producer, payload_json, status, created_at) "
                "VALUES (?, ?, 'pending', '2026-09-01T00:00:00+00:00')",
                (producer, json.dumps(payload)),
            )
    low = db.add_skill_proposal("s", "Notes", "p", "add a line", 0.3)
    big = db.add_skill_proposal("s", "Notes", "p", "x" * 5000, 0.9)
    plain = db.add_skill_proposal("s", "Notes", "p", "add a line", 0.9)
    db.resolve_skill_proposal(db.add_skill_proposal("s", "Notes", "p", "done", 0.9), "applied")  # not pending
    return low, big, plain


def test_review_pending_counts_every_pending_skill_proposal():
    from core.skills.review import count_review_pending, refresh_review_pending

    _seed_review_items()
    assert count_review_pending() == 3
    assert refresh_review_pending() == 3
    rows = _bell("review.pending")
    assert len(rows) == 1
    assert rows[0]["title"] == "3 skill proposal(s) wait for your decision"
    assert rows[0]["subject"] == "proposals" and rows[0]["link"] == {"kind": "tab", "tab": "skills"}
    assert "skill proposal" in rows[0]["body"]
    assert "veto" not in rows[0]["body"]
    assert "canary" not in rows[0]["body"] and "adaptive" not in rows[0]["body"]


def test_review_pending_coalesces_into_one_row_and_resolves_at_zero():
    from core.skills.review import refresh_review_pending

    low, big, plain = _seed_review_items()
    db.resolve_skill_proposal(plain, "rejected")
    refresh_review_pending()
    refresh_review_pending()  # unchanged count: no update, row stays read/unread as it was
    rows = _bell("review.pending")
    assert len(rows) == 1 and rows[0]["occurrences"] == 1

    db.resolve_skill_proposal(low, "rejected")
    assert refresh_review_pending() == 1
    rows = _bell("review.pending")
    assert len(rows) == 1 and rows[0]["occurrences"] == 2
    assert rows[0]["title"] == "1 skill proposal(s) wait for your decision"

    db.resolve_skill_proposal(big, "rejected")
    assert refresh_review_pending() == 0
    assert _bell("review.pending") == []
    assert len(_rows("review.pending")) == 1  # one row in the log, now resolved


async def test_snooze_refreshes_review_pending_through_the_skills_rollup(monkeypatch):
    from core.snooze import SnoozeRunner

    monkeypatch.setattr("core.snooze._mutation_blocked", lambda: True)  # read-only: never waits
    _seed_review_items()
    runner = SnoozeRunner.__new__(SnoozeRunner)
    runner._stats = {}
    runner._is_cancelled = lambda: False
    await SnoozeRunner._refresh_review_pending(runner)
    assert len(_bell("review.pending")) == 1


def test_the_rollup_rung_runs_unconditionally_after_refine():
    """Activity 15 (the adaptive step) and 13b (skill auto-apply) are gone;
    the rollup is its own rung between refine and the skill-change sweep,
    gated on nothing but cancellation."""
    import inspect

    from core.snooze import SnoozeRunner

    src = inspect.getsource(SnoozeRunner._do_cycle)
    assert "adaptive" not in src
    assert "auto_apply" not in src
    refine = src.index('self._rung("refine_one_session"')
    rollup = src.index('self._rung("refresh_review_pending"')
    sweep = src.index('self._rung("sweep_skill_content_changes"')
    assert refine < rollup < sweep
    guard = src[src.rindex("if ", 0, rollup) : rollup]
    assert guard.strip().startswith("if not self._is_cancelled():")
