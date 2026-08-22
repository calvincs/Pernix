"""Regression — 2026-08-21, box (Calvin: "did we miss these in our cleanup sweeps?").

The canary-session and dream-journal pruners walked list_sessions(500) —
the 500 most recently updated rows — so once the table passed 500 the
OLDEST sessions, the ones due for pruning, were the ones they could not
see (161 outside the window on the live box; one journal already past its
14 days and unprunable). Worker sessions and dream hypotheses had no pruner
at all. Pinned here: pruning queries by type and age, workers a parent
still waits on are kept, and only terminal hypothesis statuses are swept.
"""

from datetime import datetime, timedelta, timezone

from db import models as db
from db.database import connect_sessions


def _ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _backdate_session(sid: str, days: float) -> None:
    with connect_sessions() as conn:
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (_ago(days), sid))


def _crowd(n: int = 505) -> None:
    """More than 500 fresher sessions, so a windowed scan cannot see the old ones."""
    for i in range(n):
        db.create_session(title=f"filler {i}")


async def test_old_canary_sessions_are_pruned_even_behind_500_newer_ones():
    from core.retention import prune_canary_runs

    old = db.create_session(title="canary old", session_type="canary")
    fresh = db.create_session(title="canary fresh", session_type="canary")
    _backdate_session(old, 40)
    _backdate_session(fresh, 2)
    _crowd()

    _, pruned = await prune_canary_runs(30)
    assert pruned == 1
    assert db.get_session(old) is None and db.get_session(fresh) is not None
    assert db.get_session(db.list_sessions(1)[0]["id"]) is not None  # fillers untouched


async def test_worker_sessions_prune_by_age_unless_a_parent_still_waits(monkeypatch):
    from core.retention import prune_worker_sessions

    monkeypatch.setattr("config.settings.worker_session_retention_days", 30)
    parent = db.create_session(title="parent")
    stale = db.create_session(title="worker stale", session_type="worker", parent_session_id=parent)
    watched = db.create_session(title="worker watched", session_type="worker", parent_session_id=parent)
    recent = db.create_session(title="worker recent", session_type="worker", parent_session_id=parent)
    for sid, days in ((stale, 45), (watched, 45), (recent, 3)):
        _backdate_session(sid, days)
    with connect_sessions() as conn:
        conn.execute(
            "UPDATE sessions SET state_v2 = 'awaiting_workers', watched_worker_ids = ? WHERE id = ?",
            (f'["{watched}"]', parent),
        )

    assert await prune_worker_sessions() == 1
    assert db.get_session(stale) is None
    assert db.get_session(watched) is not None and db.get_session(recent) is not None


def test_dream_hypotheses_terminal_rows_prune_pending_and_validated_never(monkeypatch):
    from core.retention import prune_dream_hypotheses

    monkeypatch.setattr("config.settings.dream_hypothesis_retention_days", 90)
    rows = {}
    for status in ("refuted", "expired", "archived", "promoted", "pending", "validated"):
        hid = db.add_dream_hypothesis("contradiction", f"{status} hypothesis", "[]")
        db.update_dream_hypothesis(hid, status=status)
        rows[status] = hid
    young = db.add_dream_hypothesis("contradiction", "young refuted", "[]")
    db.update_dream_hypothesis(young, status="refuted")
    with connect_sessions() as conn:
        for hid in rows.values():
            conn.execute("UPDATE dream_hypotheses SET created_at = ? WHERE id = ?", (_ago(120), hid))

    assert prune_dream_hypotheses() == 4
    left = {r["id"] for r in db.list_dream_hypotheses(limit=50)}
    assert rows["pending"] in left and rows["validated"] in left and young in left
    assert all(rows[s] not in left for s in ("refuted", "expired", "archived", "promoted"))


def test_journal_prune_reaches_past_the_window_and_keeps_today(monkeypatch):
    from core.dream.journal import prune_old_journals_sync

    monkeypatch.setattr("config.settings.dream_journal_retention_days", 14)
    today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    old = db.create_session(title="Dream journal — 2026-08-05", session_type="snooze")
    todays = db.create_session(title=f"Dream journal — {today}", session_type="snooze")
    _backdate_session(old, 20)
    _backdate_session(todays, 20)  # even a stale-looking today's row is never touched
    _crowd()

    assert prune_old_journals_sync() == 1
    assert db.get_session(old) is None and db.get_session(todays) is not None
