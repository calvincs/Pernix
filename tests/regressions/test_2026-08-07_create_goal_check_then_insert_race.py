"""Regression: create_goal could create two active goals for one session.

Shipped defect (found in the 2026-08-07 architecture review, db/models.py
create_goal): the accessor read "does this session already have a live goal?"
and then inserted, both inside `with connect_sessions() as conn:`. Python's
sqlite3 in legacy isolation mode begins a transaction only before DML, so the
SELECT executed in autocommit — *outside* the transaction the INSERT opened.
Two concurrent callers therefore both read "no active goal" and both inserted.
The invariant migration v23 documented ("one active goal per session, enforced
in the accessor") was enforced by nothing: v23 shipped a NON-unique index. The
path is reachable from an agent tool, so this was a live bug, not a theoretical
one — and it corrupts budget accounting, which SUMs token_usage by goal_id.

Fix: create_goal wraps check+insert in an explicit BEGIN IMMEDIATE (the same
pattern the migration runner uses), and migration v26 adds the partial unique
index the invariant always claimed to have. A caller that loses the race to
another connection gets IntegrityError, which create_goal converts to None so
the documented "returns None if one already exists" contract still holds.
"""

from __future__ import annotations

import sqlite3
import threading
import time

from db import models as db
from db.database import connect_sessions


def _active_goals(session_id: str) -> list[dict]:
    with connect_sessions() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM session_goals WHERE session_id = ? "
                "AND status IN ('active', 'paused', 'budget_limited')",
                (session_id,),
            ).fetchall()
        ]


def test_concurrent_create_goal_yields_exactly_one_active_goal(monkeypatch):
    sid = db.create_session(title="race")

    # Widen the check→insert window. Unpatched, the GIL usually lets one
    # thread run create_goal end to end, so the pre-fix code passed this test
    # by luck; _now() is evaluated for the INSERT's parameters, i.e. after the
    # SELECT, so delaying it puts every thread inside the window at once.
    real_now = db._now

    def slow_now():
        time.sleep(0.02)
        return real_now()

    monkeypatch.setattr(db, "_now", slow_now)

    threads_n = 12
    start = threading.Barrier(threads_n)
    results: list[int | None] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        try:
            start.wait(timeout=10)
            goal_id = db.create_goal(sid, f"objective {i}")
        except BaseException as e:  # noqa: BLE001 — recorded and re-asserted below
            with lock:
                errors.append(e)
            return
        with lock:
            results.append(goal_id)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"create_goal raised under contention: {errors!r}"
    assert len(results) == threads_n
    created = [r for r in results if r is not None]
    assert len(created) == 1, f"expected exactly one winner, got {created}"
    assert len(_active_goals(sid)) == 1


def test_unique_index_rejects_a_second_active_goal_written_behind_the_accessor():
    """The index is the backstop, so assert it directly — a future refactor of
    create_goal must not be able to quietly reintroduce duplicates."""
    sid = db.create_session(title="index backstop")
    assert db.create_goal(sid, "first") is not None
    with connect_sessions() as conn:
        try:
            conn.execute(
                "INSERT INTO session_goals (session_id, objective, status) VALUES (?, 'second', 'active')",
                (sid,),
            )
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("v26 partial unique index did not reject a second active goal")


def test_create_goal_still_returns_none_when_one_is_already_live():
    sid = db.create_session(title="contract")
    first = db.create_goal(sid, "first")
    assert first is not None
    assert db.create_goal(sid, "second") is None
    # Completing the first frees the slot — the index predicate excludes
    # terminal statuses, so a follow-up goal is creatable.
    db.update_goal(first, status="complete")
    assert db.create_goal(sid, "third") is not None
