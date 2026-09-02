"""session_state_log kept rows nothing would ever delete.

Two gaps. delete_session never removed a session's transitions (the table
has no FK), so every cron, worker and canary session deleted after a week
left 10-50 rows behind permanently. And prune_state_log skipped any
session with fewer rows than keep_per_session outright — `if floor == 0:
continue` — so those same small sessions were exempt from age-based
pruning too. The table became the largest by row count, which also slowed
the prune's own DISTINCT scan.
"""

import time

from db import models as db
from db.database import connect_sessions

_OLD_MS = int((time.time() - 90 * 86400) * 1000)


def _log(sid: str, n: int, *, old: bool) -> None:
    with connect_sessions() as conn:
        for i in range(n):
            conn.execute(
                "INSERT INTO session_state_log "
                "(session_id, turn_id, from_state, to_state, reason, timestamp_ms) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (sid, 1, "idle_ready", "processing", "test", _OLD_MS + i if old else int(time.time() * 1000)),
            )


def _count(sid: str) -> int:
    with connect_sessions() as conn:
        return conn.execute("SELECT COUNT(*) c FROM session_state_log WHERE session_id = ?", (sid,)).fetchone()["c"]


def test_deleting_a_session_takes_its_transitions_with_it():
    sid = db.create_session(title="worker", session_type="worker")
    _log(sid, 20, old=False)
    assert _count(sid) == 20

    db.delete_session(sid)
    assert _count(sid) == 0, "transitions of a deleted session are unreachable garbage"


def test_old_rows_are_pruned_even_below_the_keep_floor():
    sid = db.create_session(title="small cron", session_type="cron")
    _log(sid, 12, old=True)  # far fewer than keep_per_session
    db.prune_state_log(keep_per_session=500, max_age_days=30)
    assert _count(sid) == 0, "a small session's ancient rows were exempt forever"


def test_recent_rows_below_the_floor_are_kept():
    sid = db.create_session(title="live", session_type="normal")
    _log(sid, 5, old=False)
    db.prune_state_log(keep_per_session=500, max_age_days=30)
    assert _count(sid) == 5


def test_orphaned_rows_are_reclaimed():
    sid = db.create_session(title="gone", session_type="cron")
    _log(sid, 8, old=True)
    with connect_sessions() as conn:
        conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))  # bypasses the cascade
    assert _count(sid) == 8

    db.prune_state_log(keep_per_session=500, max_age_days=30)
    assert _count(sid) == 0
