"""Tiered notifications, stream 2b: the adaptive, dream, skills-apply and
fallback-burn producers route through core/notices.py, and the one
"N skill proposals wait for your decision" bell row (core/skills/review.py).

What is pinned here: each producer's category (and so its tier), that every
pre-v42 dedup marker still suppresses a repeat on deploy day, that a tripwire
suspect leaves the bell when a clean comparison clears it, and that
review.pending counts only never-auto-apply skill proposals in ONE
coalescing row.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from config import settings
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


def _backdate_proposal(pid: int, hours: int) -> None:
    stamp = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with connect_sessions() as conn:
        conn.execute("UPDATE adaptive_proposals SET created_at = ? WHERE id = ?", (stamp, pid))


_EDIT = [
    {
        "action": "create",
        "kind": "policy",
        "scope": "global",
        "title": "receipts veto test",
        "content": "Before claiming a file is written: read it back.",
        "evidence": ["the agent seemed sloppy"],
    }
]


# --- adaptive engine ---------------------------------------------------------


def test_queue_full_is_log_tier_and_keeps_its_daily_marker():
    from core.adaptive.engine import _notify_proposal_queue_full

    _notify_proposal_queue_full("dream")
    rows = _rows("adaptive.queue_full")
    assert len(rows) == 1 and rows[0]["tier"] == "log" and rows[0]["subject"] == "dream"
    assert rows[0]["title"] == "Adaptive layer: review queue is full"
    assert not _bell("adaptive.queue_full")
    assert db.get_snooze_state(f"adaptive_queue_full:{_today()}:dream")
    _notify_proposal_queue_full("dream")
    assert len(_rows("adaptive.queue_full")) == 1


def test_cap_reached_keeps_the_pre_v42_dedup_key():
    from core.adaptive.engine import CAP_REJECTION_MARKER, _notify_capped

    rejected = [{"reason": f"kind 'routing_hint' {CAP_REJECTION_MARKER} (3)"}]
    # A marker written by the old call site this morning still holds.
    db.set_snooze_state(f"notify_dedup:{_today()}:adaptive_capped:telos", "1")
    _notify_capped("telos", rejected)
    assert _rows("adaptive.cap_reached") == []

    _notify_capped("refine", rejected)
    rows = _rows("adaptive.cap_reached")
    assert len(rows) == 1 and rows[0]["tier"] == "log" and rows[0]["subject"] == "refine"
    assert "routing_hint" in rows[0]["body"]


def test_held_proposal_is_log_tier_and_keeps_its_once_ever_marker(monkeypatch):
    from core.adaptive.engine import _hold_unfounded

    pid = db.adaptive_add_proposal("dream", json.dumps(_EDIT), json.dumps(["the agent seemed sloppy"]), "why")
    prop = db.adaptive_get_proposal(pid)
    assert _hold_unfounded(prop) is True
    rows = _rows("adaptive.proposal_held")
    assert len(rows) == 1 and rows[0]["tier"] == "log" and rows[0]["subject"] == str(pid)
    assert db.get_snooze_state(f"adaptive_unfounded_notified:{pid}")
    assert _hold_unfounded(prop) is True
    assert len(_rows("adaptive.proposal_held")) == 1


# --- tripwire ------------------------------------------------------------------


@pytest.fixture
def tripwire(monkeypatch):
    import core.adaptive.tripwire as tw

    monkeypatch.setattr(settings, "adaptive_enabled", True)
    monkeypatch.setattr(settings, "adaptive_auto_rollback", False)
    monkeypatch.setattr("core.canary.scan_canaries", lambda *a, **k: [])
    monkeypatch.setattr(tw, "_post_mortem_signal", lambda *a, **k: None)
    signal = {"value": (True, False, "canary regression: t1 failed")}
    monkeypatch.setattr(tw, "_canary_signal", lambda *a, **k: signal["value"])
    db.adaptive_create_batch("b-1", "refine", "[]", status="applied")
    return tw, signal


def test_tripwire_suspect_is_a_bell_item_that_a_clean_comparison_resolves(tripwire):
    tw, signal = tripwire
    tw.evaluate_tripwire()
    open_rows = _bell("adaptive.tripwire_suspect")
    assert len(open_rows) == 1 and open_rows[0]["subject"] == "b-1" and open_rows[0]["tier"] == "bell"
    assert open_rows[0]["link"] == {"kind": "tab", "tab": "learning"}

    signal["value"] = (False, False, "")
    actions = tw.evaluate_tripwire()
    assert any(a["action"] == "cleared" for a in actions)
    assert _bell("adaptive.tripwire_suspect") == []
    # Resolved, not deleted: the activity log keeps the history.
    assert len(_rows("adaptive.tripwire_suspect")) == 1


def test_tripwire_rollback_is_its_own_bell_item(tripwire, monkeypatch):
    tw, signal = tripwire
    monkeypatch.setattr(settings, "adaptive_auto_rollback", True)
    signal["value"] = (True, True, "canary regression: t1 confirmed")
    monkeypatch.setattr("core.adaptive.engine.rollback", lambda **k: {})
    tw.evaluate_tripwire()
    assert len(_bell("adaptive.tripwire_rolled_back")) == 1
    # The rollback answered the suspect item's question.
    assert _bell("adaptive.tripwire_suspect") == []


# --- snooze producers ----------------------------------------------------------


async def test_skill_auto_apply_is_log_tier(monkeypatch):
    from core.snooze import SnoozeRunner

    monkeypatch.setattr(
        "core.skills.proposals.auto_apply_ripe_proposals", lambda: {"applied": ["p1"], "summaries": ["s § x"]}
    )
    runner = SnoozeRunner.__new__(SnoozeRunner)
    runner._stats = {}
    await SnoozeRunner._auto_apply_skill_proposals(runner)
    rows = _rows("skills.proposals_auto_applied")
    assert len(rows) == 1 and rows[0]["tier"] == "log" and rows[0]["title"] == "Skill proposals auto-applied"


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
    # Canary and adaptive proposals no longer count (both queues retired in 3.2).
    db.adaptive_add_proposal("canary_propose", json.dumps({"canary": {"name": "c1"}}), "[]", "new canary")
    db.adaptive_add_proposal("dream", json.dumps(_EDIT), "[]", "held")
    low = db.add_skill_proposal("s", "Notes", "p", "add a line", 0.3)
    big = db.add_skill_proposal("s", "Notes", "p", "x" * 5000, 0.9)
    db.add_skill_proposal("s", "Notes", "p", "add a line", 0.9)  # auto-applies: not counted
    return low, big


def test_review_pending_counts_only_skill_proposals_that_wait_for_a_human():
    from core.skills.review import count_review_pending, refresh_review_pending

    _seed_review_items()
    assert count_review_pending() == 2
    assert refresh_review_pending() == 2
    rows = _bell("review.pending")
    assert len(rows) == 1
    assert rows[0]["title"] == "2 skill proposal(s) wait for your decision"
    assert rows[0]["subject"] == "proposals" and rows[0]["link"] == {"kind": "tab", "tab": "skills"}
    assert "skill proposal" in rows[0]["body"]
    assert "canary" not in rows[0]["body"] and "adaptive" not in rows[0]["body"]


def test_review_pending_with_the_veto_clock_off_counts_every_pending_skill_proposal(monkeypatch):
    from core.skills.review import count_review_pending

    monkeypatch.setattr(settings, "skill_proposal_auto_apply_after_hours", 0)
    _seed_review_items()
    assert count_review_pending() == 3


def test_review_pending_coalesces_into_one_row_and_resolves_at_zero():
    from core.skills.review import refresh_review_pending

    low, big = _seed_review_items()
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


def test_the_rollup_rung_follows_skill_auto_apply_unconditionally():
    """Activity 15 (the adaptive step) is gone; the rollup is its own rung
    right after Activity 13b and does not hide behind the auto-apply gate."""
    import inspect

    from core.snooze import SnoozeRunner

    src = inspect.getsource(SnoozeRunner._do_cycle)
    assert "adaptive" not in src
    auto_apply = src.index('self._rung("auto_apply_skill_proposals"')
    rollup = src.index('self._rung("refresh_review_pending"')
    sweep = src.index('self._rung("sweep_skill_content_changes"')
    assert auto_apply < rollup < sweep
    guard = src[src.rindex("if ", 0, rollup) : rollup]
    assert "skill_proposal_auto_apply_after_hours" not in guard
