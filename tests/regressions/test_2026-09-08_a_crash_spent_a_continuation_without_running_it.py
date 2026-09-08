"""Harness review 3.2.1 / H12, 2026-09-08: the debit was durable and the
dispatch was not.

`_maybe_enqueue_goal_continuation` wrote `continuations_used = ordinal` — a
durable debit — and then appended a synthetic `PendingMessage` to
`session.pending_messages`, an in-memory deque nothing persists. An ordinary
queued user message survives a crash because it is pre-saved with a real row
id and the orphan sweep can find it again; the continuation had `pre_saved =
False`, `msg_id = None`, and its user row was written only later, by
`run_agent`, once the turn had actually started.

Measured against the unfixed source: after the debit, DB message rows 0,
`get_orphaned_user_messages` empty. Across three cycles with
`continuation_budget = 2` the allowance drained to `budget_limited` with ZERO
continuations ever executed. And the boot path has no orphan sweep at all —
`reconcile_processing_sessions` / `reconcile_interrupted_sessions` reset state
to IDLE_READY and dispatch nothing — so even a continuation that HAD reached
the DB waited for a human to send a new message.

The fix is a small outbox at schema v37: the status/allowance check, the
ordinal allocation and the insert are one transaction; dispatch takes a
durable claim and settles it; boot recovers what a dead process left behind.

Durable dispatch is not exactly-once side effects, and this file pins that
distinction: a row abandoned in `claimed` may have run a shell command or
written a file, so it comes back with an instruction to re-read receipts and
artifacts before repeating anything — never as a replay.
"""

from __future__ import annotations

import json
from collections import deque
from types import SimpleNamespace

import pytest

from db import models as db
from sessions.state import PendingMessage

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _fake_session(sid, termination="round_ceiling"):
    """The shape _maybe_enqueue_goal_continuation actually reads."""
    return SimpleNamespace(
        session_id=sid,
        session_type="normal",
        termination_reason=termination,
        pending_messages=deque(),
        worker_ids=[],
        emit_event=lambda e: None,
    )


@pytest.fixture
def mgr(monkeypatch):
    from sessions import state_v2 as sv2
    from sessions.manager import SessionManager

    monkeypatch.setattr(sv2, "_current_state", lambda s: sv2.SessionStateV2.FINALIZING)
    monkeypatch.setattr("config.settings.goals_enabled", True)
    m = SessionManager()
    monkeypatch.setattr(m, "broadcast", lambda *a, **k: None)
    return m


@pytest.fixture
def goal_session(mgr):
    sid = db.create_session(title="H12")
    gid = db.create_goal(sid, "long unattended objective", continuation_budget=3)
    return SimpleNamespace(sid=sid, gid=gid, mgr=mgr)


async def _enqueue(goal_session, termination="round_ceiling"):
    s = _fake_session(goal_session.sid, termination=termination)
    await goal_session.mgr._maybe_enqueue_goal_continuation(s)
    return s


async def _drain(session):
    """Let a dispatched turn actually run, so no coroutine is left dangling."""
    task = getattr(session, "task", None)
    if task is not None:
        await task
        session.task = None


def _outbox(sid):
    from db.database import connect_sessions

    with connect_sessions() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM goal_continuations WHERE session_id = ? ORDER BY id", (sid,)
            ).fetchall()
        ]


# ---------------------------------------------------------------------------
# The debit and the enqueue are now one thing
# ---------------------------------------------------------------------------


async def test_the_debit_and_the_enqueue_are_one_transaction(goal_session):
    """Unfixed: DB message rows 0, orphans [], allowance spent."""
    s = await _enqueue(goal_session)

    rows = _outbox(goal_session.sid)
    assert len(rows) == 1
    row = rows[0]
    assert row["ordinal"] == 1
    assert row["status"] == "pending"
    assert "[goal continuation 1/3]" in row["prompt"]
    assert db.get_active_goal(goal_session.sid)["continuations_used"] == 1

    # The in-memory entry now carries the durable row it belongs to.
    assert s.pending_messages[0].continuation_id == row["id"]

    # A bounded checkpoint, not just "continue".
    checkpoint = json.loads(row["checkpoint"])
    assert checkpoint["goal_id"] == goal_session.gid
    assert checkpoint["termination_reason"] == "round_ceiling"
    assert len(row["checkpoint"]) <= 2000


async def test_a_crash_before_the_debit_costs_nothing(goal_session, monkeypatch):
    """The transaction rolls back whole; the allowance is untouched."""

    def _boom(*_a, **_kw):
        raise RuntimeError("process died mid-transaction")

    monkeypatch.setattr("db.models.enqueue_goal_continuation", _boom)
    s = _fake_session(goal_session.sid)
    with pytest.raises(RuntimeError):
        await goal_session.mgr._maybe_enqueue_goal_continuation(s)

    assert _outbox(goal_session.sid) == []
    assert db.get_active_goal(goal_session.sid)["continuations_used"] == 0
    assert not s.pending_messages


async def test_a_crash_after_the_durable_enqueue_loses_nothing(goal_session):
    """The whole point: the deque evaporates, the row does not."""
    s = await _enqueue(goal_session)
    del s  # the process dies; in-memory state is gone

    recovered = await goal_session.mgr.recover_goal_continuations()
    assert recovered == 1

    session = goal_session.mgr.get(goal_session.sid)
    assert len(session.pending_messages) == 1
    entry = PendingMessage.coerce(session.pending_messages[0])
    assert entry.is_goal_continuation
    assert "[goal continuation 1/3]" in entry.message
    # Never claimed, so nothing can have run: no verify-first preamble.
    assert "recovered continuation" not in entry.message
    # And no second debit.
    assert db.get_active_goal(goal_session.sid)["continuations_used"] == 1
    assert len(_outbox(goal_session.sid)) == 1


async def test_three_cycles_no_longer_drain_the_allowance_on_nothing(mgr):
    """Unfixed, this was the whole failure: budget=2, three finalizations, the
    goal reaches budget_limited having executed no continuation at all."""
    sid = db.create_session(title="drain")
    db.create_goal(sid, "an objective nobody is watching", continuation_budget=2)

    for _cycle in range(3):
        s = _fake_session(sid)
        await mgr._maybe_enqueue_goal_continuation(s)
        del s  # crash before dispatch, every time

    assert db.get_active_goal(sid)["status"] == "budget_limited"
    rows = _outbox(sid)
    assert [r["ordinal"] for r in rows] == [1, 2]
    # The debits are still recoverable work, not spent air.
    assert all(r["status"] == "pending" for r in rows)
    assert await mgr.recover_goal_continuations() == 0  # goal is budget_limited now


# ---------------------------------------------------------------------------
# Claim, settle, and the crash between them
# ---------------------------------------------------------------------------


async def test_a_dispatch_claims_and_settles(goal_session, monkeypatch):
    from sessions import state_v2 as sv2

    s = await _enqueue(goal_session)
    session = goal_session.mgr.get_or_create(goal_session.sid)
    session.pending_messages.extend(s.pending_messages)
    monkeypatch.setattr(sv2, "_current_state", lambda x: sv2.SessionStateV2.IDLE_READY)

    started: list[str] = []

    async def _runner(sess, message, system_prompt, pre_saved=False):
        started.append(message)

    monkeypatch.setattr(goal_session.mgr, "_run_agent_safe", _runner)
    await goal_session.mgr._process_pending(session)
    await _drain(session)

    assert len(started) == 1
    row = _outbox(goal_session.sid)[0]
    assert row["status"] == "dispatched"
    assert row["attempts"] == 1
    assert row["claimed_at"] and row["settled_at"]


async def test_a_duplicate_dispatch_attempt_loses_the_claim(goal_session):
    """Two dispatchers, one ordinal. The second must not start a turn."""
    s = await _enqueue(goal_session)
    cid = s.pending_messages[0].continuation_id

    assert db.claim_goal_continuation(cid) is not None
    assert db.claim_goal_continuation(cid) is None  # already owned
    db.settle_goal_continuation(cid, "dispatched", "turn started")
    assert db.claim_goal_continuation(cid) is None  # already settled
    assert db.get_goal_continuation(cid)["attempts"] == 1


async def test_a_crash_after_the_claim_recovers_without_replaying(goal_session):
    """The claim is held when the process dies, so what it did is UNKNOWN.

    It comes back — no lost continuation — but it comes back told to look
    before it repeats anything, and it is not re-debited.
    """
    s = await _enqueue(goal_session)
    cid = s.pending_messages[0].continuation_id
    assert db.claim_goal_continuation(cid) is not None
    del s  # crash with the claim held and side effects in flight

    assert await goal_session.mgr.recover_goal_continuations() == 1
    session = goal_session.mgr.get(goal_session.sid)
    message = PendingMessage.coerce(session.pending_messages[0]).message

    assert "[recovered continuation]" in message
    assert "Do NOT re-run a command" in message
    assert "re-read the workspace" in message
    assert "Checkpoint at the time it was dispatched" in message
    assert "[goal continuation 1/3]" in message  # the original work, still there

    row = db.get_goal_continuation(cid)
    assert row["recovered"] == 1
    assert row["status"] == "pending"
    assert row["ordinal"] == 1
    assert db.get_active_goal(goal_session.sid)["continuations_used"] == 1


async def test_a_crash_after_a_side_effect_but_before_settlement(goal_session, tmp_path):
    """The concrete shape of "no automatic replay": a file the continuation
    wrote is still there afterwards, written once, and the recovery text tells
    the agent to check for exactly that before acting."""
    s = await _enqueue(goal_session)
    cid = s.pending_messages[0].continuation_id
    db.claim_goal_continuation(cid)

    receipt = tmp_path / "deploy.receipt"
    receipt.write_text("deployed once\n")  # the side effect that already happened

    assert await goal_session.mgr.recover_goal_continuations() == 1
    session = goal_session.mgr.get(goal_session.sid)
    message = PendingMessage.coerce(session.pending_messages[0]).message

    assert "command receipts" in message
    assert "may have completed, may have half-completed" in message
    # The harness itself replayed nothing.
    assert receipt.read_text() == "deployed once\n"
    assert db.get_active_goal(goal_session.sid)["continuations_used"] == 1


async def test_a_crash_loop_stops_recovering_eventually(goal_session):
    """A row that kills the process every time must not replay forever."""
    s = await _enqueue(goal_session)
    cid = s.pending_messages[0].continuation_id

    for _attempt in range(db.CONTINUATION_RECOVERY_LIMIT):
        db.claim_goal_continuation(cid)
        goal_session.mgr.get_or_create(goal_session.sid).pending_messages.clear()
        await goal_session.mgr.recover_goal_continuations()

    db.claim_goal_continuation(cid)
    goal_session.mgr.get_or_create(goal_session.sid).pending_messages.clear()
    assert await goal_session.mgr.recover_goal_continuations() == 0
    row = db.get_goal_continuation(cid)
    assert row["status"] == "abandoned"
    assert "recovery attempts" in row["outcome"]


# ---------------------------------------------------------------------------
# Standing user intent outranks a queued continuation
# ---------------------------------------------------------------------------


async def test_a_user_paused_task_does_not_resume_on_restart(goal_session):
    """A paused goal is the durable form of "the user stopped this"."""
    s = await _enqueue(goal_session)
    cid = s.pending_messages[0].continuation_id
    db.update_goal(goal_session.gid, status="paused")

    assert await goal_session.mgr.recover_goal_continuations() == 0
    assert not goal_session.mgr.get_or_create(goal_session.sid).pending_messages
    row = db.get_goal_continuation(cid)
    assert row["status"] == "abandoned"
    assert "paused" in row["outcome"]


async def test_queued_user_direction_supersedes_a_recovered_continuation(goal_session):
    """The user's words outrank the machine's, at recovery as at enqueue."""
    s = await _enqueue(goal_session)
    cid = s.pending_messages[0].continuation_id
    db.add_message(goal_session.sid, "user", "stop what you're doing and do this instead")

    assert await goal_session.mgr.recover_goal_continuations() == 0
    row = db.get_goal_continuation(cid)
    assert row["status"] == "abandoned"
    assert "user direction" in row["outcome"]


async def test_a_goal_completed_during_the_await_is_not_continued(goal_session, monkeypatch):
    """Status can change while the entry sits in the queue; the claim re-checks."""
    from sessions import state_v2 as sv2

    s = await _enqueue(goal_session)
    session = goal_session.mgr.get_or_create(goal_session.sid)
    session.pending_messages.extend(s.pending_messages)
    cid = s.pending_messages[0].continuation_id

    db.update_goal(goal_session.gid, status="complete")
    monkeypatch.setattr(sv2, "_current_state", lambda x: sv2.SessionStateV2.IDLE_READY)

    started: list[str] = []

    async def _runner(sess, message, system_prompt, pre_saved=False):
        started.append(message)

    monkeypatch.setattr(goal_session.mgr, "_run_agent_safe", _runner)
    await goal_session.mgr._process_pending(session)
    await _drain(session)

    assert started == []
    row = db.get_goal_continuation(cid)
    assert row["status"] == "abandoned"
    assert "not active" in row["outcome"]


async def test_a_cancel_during_the_await_stops_the_dispatch(goal_session, monkeypatch):
    from sessions import state_v2 as sv2

    s = await _enqueue(goal_session)
    session = goal_session.mgr.get_or_create(goal_session.sid)
    session.pending_messages.extend(s.pending_messages)
    session.cancel_requested = True
    cid = s.pending_messages[0].continuation_id
    monkeypatch.setattr(sv2, "_current_state", lambda x: sv2.SessionStateV2.IDLE_READY)

    started: list[str] = []

    async def _runner(sess, message, system_prompt, pre_saved=False):
        started.append(message)

    monkeypatch.setattr(goal_session.mgr, "_run_agent_safe", _runner)
    await goal_session.mgr._process_pending(session)
    await _drain(session)

    assert started == []
    assert db.get_goal_continuation(cid)["status"] == "abandoned"


async def test_a_refused_continuation_does_not_block_a_real_user_message(goal_session, monkeypatch):
    """The queue drains past it rather than stalling behind a dead entry."""
    from sessions import state_v2 as sv2

    s = await _enqueue(goal_session)
    session = goal_session.mgr.get_or_create(goal_session.sid)
    cid = s.pending_messages[0].continuation_id
    session.pending_messages.extend(s.pending_messages)
    mid = db.add_message(goal_session.sid, "user", "actually, do this")
    session.pending_messages.append(PendingMessage("actually, do this", "", True, 0.0, mid))
    db.update_goal(goal_session.gid, status="paused")
    monkeypatch.setattr(sv2, "_current_state", lambda x: sv2.SessionStateV2.IDLE_READY)

    started: list[str] = []

    async def _runner(sess, message, system_prompt, pre_saved=False):
        started.append(message)

    monkeypatch.setattr(goal_session.mgr, "_run_agent_safe", _runner)
    await goal_session.mgr._process_pending(session)
    await _drain(session)

    assert started == ["actually, do this"]
    assert db.get_goal_continuation(cid)["status"] == "abandoned"


# ---------------------------------------------------------------------------
# The outbox itself
# ---------------------------------------------------------------------------


def test_the_ordinal_is_unique_per_goal(goal_session):
    """The index that makes a double debit impossible whatever races occur."""
    import sqlite3

    from db.database import connect_sessions

    db.enqueue_goal_continuation(goal_session.sid, goal_session.gid, 3, lambda o, b: f"[goal continuation {o}/{b}]")
    with connect_sessions() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO goal_continuations (goal_id, session_id, ordinal, prompt, created_at) "
                "VALUES (?, ?, 1, 'dup', '2026-09-08')",
                (goal_session.gid, goal_session.sid),
            )


def test_the_outbox_refuses_what_the_goal_no_longer_authorizes(goal_session):
    def _prompt(o, b):
        return f"[goal continuation {o}/{b}]"

    assert db.enqueue_goal_continuation(goal_session.sid, goal_session.gid, 1, _prompt) is not None
    # Allowance spent.
    assert db.enqueue_goal_continuation(goal_session.sid, goal_session.gid, 1, _prompt) is None
    # Goal no longer active.
    db.update_goal(goal_session.gid, status="paused")
    assert db.enqueue_goal_continuation(goal_session.sid, goal_session.gid, 3, _prompt) is None
    # Wrong owner.
    other = db.create_session(title="other")
    assert db.enqueue_goal_continuation(other, goal_session.gid, 3, _prompt) is None


def test_the_schema_is_at_version_37():
    from db.database import MIGRATIONS

    assert MIGRATIONS[-1][0] == 37
    assert "continuation" in MIGRATIONS[-1][1]


def test_migration_v37_adds_the_outbox_to_a_v36_database(tmp_path, monkeypatch):
    """A live box gets the table by upgrade, not by a fresh schema."""
    from db.database import MIGRATIONS, connect_sessions, init_sessions_db

    def _tables(conn):
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

    monkeypatch.setattr("config.settings.db_path", str(tmp_path / "v36.db"))
    monkeypatch.setattr("db.database.MIGRATIONS", [m for m in MIGRATIONS if m[0] <= 36])
    init_sessions_db()
    with connect_sessions() as conn:
        assert "goal_continuations" not in _tables(conn)
        assert int(conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]) == 36

    monkeypatch.undo()
    monkeypatch.setattr("config.settings.db_path", str(tmp_path / "v36.db"))
    init_sessions_db()
    with connect_sessions() as conn:
        assert "goal_continuations" in _tables(conn)
        assert int(conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]) >= 37
        idx = {r["name"] for r in conn.execute("PRAGMA index_list(goal_continuations)")}
        assert "idx_goal_continuations_ordinal" in idx


def test_the_boot_path_runs_the_recovery():
    """No boot path swept for orphaned continuations at all. This one does, and
    it runs after the reconciles that make the session dispatchable."""
    import inspect

    from api import app as _app

    src = inspect.getsource(_app)
    assert "manager.recover_goal_continuations()" in src
    assert src.index("reconcile_interrupted_sessions()") < src.index("recover_goal_continuations()")
