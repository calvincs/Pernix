"""A bulk purge froze every stream on the box for the length of the batch.

`purge_sessions` offloaded candidate SELECTION correctly — one
`asyncio.to_thread` around `list_purge_candidates` — and then deleted the
candidates in a plain `for` loop calling the SYNCHRONOUS
`manager.delete_session`, with no await of any kind. Per session that is a
summary file unlink, a scheduler-budget purge, RLM artifact removal, a
kernel-state teardown, a recursive DB cascade across messages,
`messages_fts` and `session_state_log`, and a commit — all of it on the
event loop, once per candidate.

`sessions/manager.py` already carried the fix. Its own docstring says
"Prefer `delete_session_async` from the event loop", and the async form
splits the work exactly where it has to be split: phase 1 cancels the turn
and drops the in-memory session and MUST stay loop-affine because it calls
`cancel_session`; phase 2 is the blocking cleanup and goes to a thread. The
bulk caller simply bypassed it.

Measured, 1,000 sessions / 60,000 messages / 60,000 messages_fts rows /
12,000 session_state_log rows / 538 MB, purging 995:

    as shipped   995 sync calls, 0 async, wall 8,992.6 ms,
                 heartbeat worst gap 8,980.4 ms, 3 ticks where 1,798
                 were due, loop starved 8.975 s
    fixed        0 sync calls, 995 async, wall 6,736.9 ms,
                 heartbeat worst gap 7.6 ms, 1,252 of 1,347 ticks,
                 loop starved 0.003 s

Two things the audit did not name, found while reproducing it.

`purged += 1` sat inside no `try` and the route had no partial-completion
contract, so a failure partway through discarded the count of everything
that HAD been deleted: measured, 10 sessions actually deleted, the route
raised, the client got no body at all, and `settings.js`'s "Pruned N
sessions" never ran — the user was told the purge had failed while ten of
their chats were already gone. The audit's criterion says "do not report
unperformed deletions as completed"; the shipped defect was the mirror
image.

And `to_delete` is a snapshot taken in a worker thread, with no re-check at
delete time. A session that got pinned, moved into a space, was reopened,
or started a turn between selection and deletion was deleted anyway.

Pinned here: the async path and only the async path, a heartbeat that keeps
ticking through a deliberately slow cleanup, a re-check per candidate
against the same rules, and a response whose numbers add up after a partial
run.
"""

import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from db import models as db
from db.database import connect_sessions

HEARTBEAT_S = 0.005


def _ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _backdate(sid: str, days: float) -> None:
    with connect_sessions() as conn:
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (_ago(days), sid))


def _stale(n: int, days: float = 30) -> list[str]:
    ids = []
    for i in range(n):
        sid = db.create_session(title=f"stale chat {i}")
        db.add_message(sid, "user", f"something said in chat {i}")
        _backdate(sid, days + i)
        ids.append(sid)
    return ids


def _client() -> AsyncClient:
    from api.routers import sessions

    app = FastAPI()
    app.include_router(sessions.router)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _purge(**body) -> dict:
    async with _client() as c:
        resp = await c.post("/api/sessions/purge", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# (a) the async path, and only the async path
# ---------------------------------------------------------------------------


async def test_the_purge_uses_the_async_delete_for_every_candidate(monkeypatch):
    from sessions.manager import SessionManager

    ids = _stale(6)
    seen = {"sync": 0, "async": 0}
    real_async = SessionManager.delete_session_async
    real_sync = SessionManager.delete_session

    async def counting_async(self, sid):
        seen["async"] += 1
        return await real_async(self, sid)

    def counting_sync(self, sid):
        seen["sync"] += 1
        return real_sync(self, sid)

    monkeypatch.setattr(SessionManager, "delete_session_async", counting_async)
    monkeypatch.setattr(SessionManager, "delete_session", counting_sync)

    result = await _purge(keep_days=7, keep_min=0)

    assert result["purged"] == len(ids) == 6
    assert seen["async"] == 6
    assert seen["sync"] == 0, "the synchronous manager method is what froze the loop"
    assert all(db.get_session(sid) is None for sid in ids)


async def test_the_whole_manager_method_was_not_just_offloaded_wholesale(monkeypatch):
    """phase 1 cancels the turn and has to stay on the loop.

    Pushing `delete_session` into a thread would look like a fix and would
    move `cancel_session` off the loop with it. Phase 1 runs on the caller's
    thread; only phase 2 hops.
    """
    import threading

    from sessions.manager import SessionManager

    _stale(3)
    main = threading.current_thread()
    threads = {"phase1": set(), "phase2": set()}
    real1 = SessionManager._delete_session_phase1
    real2 = SessionManager._delete_session_phase2

    def p1(self, sid):
        threads["phase1"].add(threading.current_thread())
        return real1(self, sid)

    def p2(self, ids):
        threads["phase2"].add(threading.current_thread())
        return real2(self, ids)

    monkeypatch.setattr(SessionManager, "_delete_session_phase1", p1)
    monkeypatch.setattr(SessionManager, "_delete_session_phase2", p2)

    await _purge(keep_days=7, keep_min=0)

    assert threads["phase1"] == {main}, "phase 1 must stay loop-affine — it cancels the turn"
    assert threads["phase2"] and main not in threads["phase2"], "phase 2 must not run on the loop"


@pytest.mark.slow
async def test_another_coroutine_progresses_through_a_slow_cleanup(monkeypatch):
    """A heartbeat every 5 ms, against a cleanup made deliberately slow."""
    from sessions.manager import SessionManager

    _stale(8)
    real2 = SessionManager._delete_session_phase2

    def slow(self, ids):
        time.sleep(0.05)
        return real2(self, ids)

    monkeypatch.setattr(SessionManager, "_delete_session_phase2", slow)

    ticks = 0
    worst = 0.0
    last = time.perf_counter()

    async def beat():
        nonlocal ticks, worst, last
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            now = time.perf_counter()
            worst = max(worst, now - last)
            last = now
            ticks += 1

    task = asyncio.create_task(beat())
    await asyncio.sleep(0.05)
    ticks, worst, last = 0, 0.0, time.perf_counter()
    started = time.perf_counter()
    result = await _purge(keep_days=7, keep_min=0)
    wall = time.perf_counter() - started
    worst = max(worst, time.perf_counter() - last)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert result["purged"] == 8
    assert wall > 0.3, "the cleanup was not actually slow — the test proves nothing"
    assert ticks > 40, f"only {ticks} heartbeats fired across {wall*1000:.0f} ms of purging"
    assert worst < 0.2, f"the loop was held for {worst*1000:.0f} ms"


# ---------------------------------------------------------------------------
# (b) partial completion is reported, not thrown away
# ---------------------------------------------------------------------------


async def test_a_failure_partway_through_still_reports_what_was_deleted(monkeypatch):
    from sessions.manager import SessionManager

    ids = _stale(10)  # newest first in candidate order
    real = SessionManager.delete_session_async
    calls = {"n": 0}

    async def flaky(self, sid):
        calls["n"] += 1
        if calls["n"] == 4:
            raise OSError("disk went away")
        return await real(self, sid)

    monkeypatch.setattr(SessionManager, "delete_session_async", flaky)

    result = await _purge(keep_days=7, keep_min=0)

    assert result["purged"] == 9, "nine deletions happened and the user has to be told so"
    assert result["failed"] == 1
    assert result["complete"] is False
    assert len(result["failures"]) == 1
    assert "OSError" in result["failures"][0]["error"]
    assert sum(1 for sid in ids if db.get_session(sid) is None) == 9


async def test_the_numbers_add_up_after_a_partial_run(monkeypatch):
    from sessions.manager import SessionManager

    _stale(12)
    real = SessionManager.delete_session_async
    calls = {"n": 0}

    async def flaky(self, sid):
        calls["n"] += 1
        if calls["n"] % 5 == 0:
            raise RuntimeError("cleanup refused")
        return await real(self, sid)

    monkeypatch.setattr(SessionManager, "delete_session_async", flaky)

    r = await _purge(keep_days=7, keep_min=0)

    assert r["purged"] + r["failed"] + sum(r["skipped_at_delete"].values()) == r["would_delete"]
    assert r["failed"] == 2 and r["purged"] == 10


async def test_a_clean_run_says_it_completed():
    _stale(4)
    r = await _purge(keep_days=7, keep_min=0)
    assert r["complete"] is True
    assert r["failed"] == 0 and r["failures"] == []
    assert r["purged"] == r["would_delete"] == 4


async def test_a_dry_run_still_promises_nothing_it_did_not_do():
    ids = _stale(4)
    r = await _purge(keep_days=7, keep_min=0, dry_run=True)
    assert r["purged"] == 0 and r["would_delete"] == 4
    assert r["complete"] is True, "a dry run completes by doing nothing, which is the whole contract"
    assert all(db.get_session(sid) is not None for sid in ids)


async def test_cancellation_mid_batch_propagates_rather_than_lying(monkeypatch):
    """No response body can carry a partial count to a client that left."""
    from sessions.manager import SessionManager

    ids = _stale(10)
    calls = {"n": 0}
    real = SessionManager.delete_session_async

    async def cancel_after_three(self, sid):
        calls["n"] += 1
        if calls["n"] == 4:
            raise asyncio.CancelledError()
        return await real(self, sid)

    monkeypatch.setattr(SessionManager, "delete_session_async", cancel_after_three)

    from api.routers.sessions import purge_sessions

    with pytest.raises(asyncio.CancelledError):
        await purge_sessions({"keep_days": 7, "keep_min": 0})

    assert sum(1 for sid in ids if db.get_session(sid) is None) == 3, "the three that finished stay deleted"


# ---------------------------------------------------------------------------
# (c) the snapshot is re-checked at delete time
# ---------------------------------------------------------------------------


async def test_a_session_pinned_after_selection_is_not_deleted(monkeypatch):
    from sessions.manager import SessionManager

    ids = _stale(4)
    victim = ids[-1]  # oldest, so it is deleted last
    real = SessionManager.delete_session_async
    calls = {"n": 0}

    async def pin_the_last_one(self, sid):
        calls["n"] += 1
        if calls["n"] == 1:
            with connect_sessions() as conn:
                conn.execute("UPDATE sessions SET pinned = 1 WHERE id = ?", (victim,))
        return await real(self, sid)

    monkeypatch.setattr(SessionManager, "delete_session_async", pin_the_last_one)

    r = await _purge(keep_days=7, keep_min=0)

    assert db.get_session(victim) is not None, "the user said keep this while the purge was running"
    assert r["skipped_at_delete"]["pinned"] == 1
    assert r["purged"] == 3


@pytest.mark.parametrize(
    "change,bucket",
    [
        ("UPDATE sessions SET space_id = (SELECT id FROM spaces LIMIT 1) WHERE id = ?", "in_space"),
        ("UPDATE sessions SET session_type = 'worker' WHERE id = ?", "other_types"),
        ("UPDATE sessions SET state_v2 = 'processing' WHERE id = ?", "busy"),
        ("DELETE FROM sessions WHERE id = ?", "gone"),
    ],
)
async def test_a_candidate_that_changes_before_its_turn_is_spared(monkeypatch, change, bucket):
    from sessions.manager import SessionManager

    db.create_space(label="Research", slug="research", color="#8ab4f8")
    ids = _stale(3)
    victim = ids[-1]
    real = SessionManager.delete_session_async
    calls = {"n": 0}

    async def mutate_first(self, sid):
        calls["n"] += 1
        if calls["n"] == 1:
            with connect_sessions() as conn:
                conn.execute(change, (victim,))
        return await real(self, sid)

    monkeypatch.setattr(SessionManager, "delete_session_async", mutate_first)

    r = await _purge(keep_days=7, keep_min=0)

    assert r["skipped_at_delete"][bucket] == 1, r["skipped_at_delete"]
    assert r["purged"] == 2
    if bucket != "gone":
        assert db.get_session(victim) is not None


async def test_a_session_touched_after_selection_is_no_longer_idle(monkeypatch):
    """Reopening a chat mid-purge takes it back out of the stale set."""
    from sessions.manager import SessionManager

    ids = _stale(3)
    victim = ids[-1]
    real = SessionManager.delete_session_async
    calls = {"n": 0}

    async def touch_first(self, sid):
        calls["n"] += 1
        if calls["n"] == 1:
            with connect_sessions() as conn:
                conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (_ago(0), victim))
        return await real(self, sid)

    monkeypatch.setattr(SessionManager, "delete_session_async", touch_first)

    r = await _purge(keep_days=7, keep_min=0)

    assert r["skipped_at_delete"]["touched"] == 1
    assert db.get_session(victim) is not None


def test_the_recheck_agrees_with_the_selection_it_re_checks():
    """Same rules, same vocabulary — a re-check must not invent a verdict."""
    cutoff = _ago(7)
    plain = db.create_session(title="ordinary and stale")
    _backdate(plain, 30)
    assert db.purge_candidate_spared_by(plain, cutoff) is None

    space = db.create_space(label="Research", slug="research", color="#8ab4f8")["id"]
    cases = {
        db.create_session(title="pinned"): "pinned",
        db.create_session(title="in a space", space_id=space): "in_space",
        db.create_session(title="a worker", session_type="worker"): "other_types",
    }
    with connect_sessions() as conn:
        conn.execute("UPDATE sessions SET pinned = 1 WHERE title = 'pinned'")
    for sid, bucket in cases.items():
        _backdate(sid, 30)
        assert db.purge_candidate_spared_by(sid, cutoff) == bucket

    assert db.purge_candidate_spared_by("never-existed", cutoff) == "gone"

    fresh = db.create_session(title="from a minute ago")
    assert db.purge_candidate_spared_by(fresh, cutoff) == "touched"


def test_the_idle_states_the_recheck_treats_as_deletable():
    """`SQL_SESSION_IS_IDLE` is the existing definition and stays the only one."""
    cutoff = _ago(7)
    for state in ("idle_ready", "cancelling", "finalizing", "awaiting_user", "awaiting_workers", None):
        sid = db.create_session(title=f"state {state}")
        _backdate(sid, 30)
        with connect_sessions() as conn:
            conn.execute("UPDATE sessions SET state_v2 = ? WHERE id = ?", (state, sid))
        assert db.purge_candidate_spared_by(sid, cutoff) is None, state

    for state in ("processing", "scouting", "compacting", "paused", "pause_requested"):
        sid = db.create_session(title=f"state {state}")
        _backdate(sid, 30)
        with connect_sessions() as conn:
            conn.execute("UPDATE sessions SET state_v2 = ? WHERE id = ?", (state, sid))
        assert db.purge_candidate_spared_by(sid, cutoff) == "busy", state
