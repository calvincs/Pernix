"""Regression: /api/storage/optimize asked the wrong question, once, too early.

Shipped defect (3.2.2 audit S07). The handler was two statements:

    if get_manager().has_active_work():
        raise HTTPException(409, ...)
    before, after = await asyncio.to_thread(_vacuum)

Four things were wrong with that, and all four reproduce.

1. The predicate was the wrong one. `has_active_work()` routes through
   `snooze_transparent`, which is the idle-SCHEDULING rule: it looks straight
   through canary sessions (SNOOZE_TRANSPARENT_TYPES) and through any session
   driven by a goal auto-continuation, because snooze must not deadlock
   against its own sweeps. A canary turn mid-write and an hours-long
   autonomous goal mid-write both read as an idle database, and `VACUUM` was
   admitted. `has_active_work(strict=True)` — the codebase's own stricter
   reading of the same generator — catches the goal continuation and still
   looks through the canary, so neither strictness was the right question.

2. Nothing recorded that a rebuild was in flight. There was no serialization
   primitive in storage.py at all, so a second `/optimize` walked straight
   past the check into a second `VACUUM` on the same file.

3. `asyncio.to_thread` cannot be cancelled once entered. A client that
   disconnected mid-rebuild left the vacuum running — and since nothing had
   been recorded in the first place, the NEXT `/optimize` was admitted on top
   of it.

4. The gap between the check and the thread was an `await`. Anything that
   started in it ran against the rebuild.

Fix: `sessions.manager.has_database_writers()` is the question with no
exemptions in it, and db/exclusive.py is the missing state. `/optimize` claims the exclusive
slot synchronously, before any await; the thread drains announced background
writers, re-asks the strict predicate immediately before the statement, and
releases the slot in its own `finally` — so a cancelled request cannot hand
the gate to the next caller while the vacuum still holds the writer lock.

Scope note, from the audit's own measurement: the writer-loss this docstring's
predecessor asserted does NOT currently occur. At 178 MB — the live box is
168 MB — a full VACUUM holds the writer lock 0.70 s and a concurrent writer
waits it out inside the 5 s busy_timeout; the timeout is not reached until the
file is past 1.2 GB. These four are structural defects that stand on their
own, and the fix is admission, never a longer timeout.
"""

from __future__ import annotations

import asyncio
import threading

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from db.exclusive import MaintenanceBusy, _DatabaseGate, get_gate
from sessions import state_v2 as sv2


def _app():
    from api.routers import storage

    app = FastAPI()
    app.include_router(storage.router)
    return app


async def _post(path="/api/storage/optimize", body=None):
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        return await client.post(path, json=body if body is not None else {})


class _Turn:
    """The attributes `_working_sessions` actually reads, and nothing else.

    Deliberately not a mock of the ANSWER: this stub is fed to the real
    generator in sessions/manager.py so the real `snooze_transparent`, the
    real SNOOZE_TRANSPARENT_TYPES and the real state machine decide. Mocking
    `has_active_work` to True — which is all tests/test_storage.py did — can
    never catch a predicate that returns the wrong answer.
    """

    def __init__(self, session_type="normal", goal_continuation_active=False):
        self.session_id = f"turn-{session_type}"
        self.session_type = session_type
        self.goal_continuation_active = goal_continuation_active
        self._state_v2 = sv2.SessionStateV2.PROCESSING
        self.has_background_tasks = False


@pytest.fixture(autouse=True)
def quiet_box():
    """A manager with nothing loaded, restored afterwards.

    The manager is a process singleton and these tests assert on what the
    real predicate says about its contents, so a session another test left
    behind would be indistinguishable from the one under test.
    """
    from sessions.manager import get_manager

    manager = get_manager()
    saved = dict(manager._sessions)
    manager._sessions.clear()
    try:
        yield manager
    finally:
        manager._sessions.clear()
        manager._sessions.update(saved)


@pytest.fixture
def live_turn(quiet_box):
    """Put a real-shaped session into the real manager."""

    def _add(turn: _Turn) -> _Turn:
        quiet_box._sessions[turn.session_id] = turn
        return turn

    return _add


@pytest.fixture
def free_gate():
    """The process-wide gate, guaranteed released whatever the test did."""
    gate = get_gate()
    yield gate
    gate.release()


# ---------------------------------------------------------------------------
# 1. The predicate
# ---------------------------------------------------------------------------


async def test_a_canary_turn_is_no_longer_read_as_an_idle_database(live_turn, free_gate):
    """The verified admission: canary PROCESSING, and VACUUM went ahead."""
    from sessions.manager import get_manager

    live_turn(_Turn(session_type="canary"))

    assert get_manager().has_active_work() is False, "the idle-scheduling predicate looks through canaries"
    assert get_manager().has_active_work(strict=True) is False, "and so does the stricter one"
    assert get_manager().has_database_writers() is True, "a canary's writes are writes"

    resp = await _post()
    assert resp.status_code == 409, "a canary turn is a writer like any other"
    assert "turn is running" in resp.json()["detail"]


async def test_a_goal_continuation_turn_is_no_longer_read_as_an_idle_database(live_turn, free_gate):
    """The second verified admission, and the one that can last for hours."""
    from sessions.manager import get_manager

    live_turn(_Turn(session_type="normal", goal_continuation_active=True))

    assert get_manager().has_active_work() is False
    assert get_manager().has_active_work(strict=True) is True, "the stricter reading already saw this one"
    assert get_manager().has_database_writers() is True

    assert (await _post()).status_code == 409


async def test_an_ordinary_turn_is_still_refused(live_turn, free_gate):
    """The case that always worked keeps working — the fix is a widening."""
    live_turn(_Turn(session_type="normal"))
    assert (await _post()).status_code == 409


async def test_an_idle_box_still_gets_its_rebuild(free_gate):
    """A gate that refused everything would be no fix at all."""
    resp = await _post()
    assert resp.status_code == 200
    assert resp.json()["bytes_before"] > 0


# ---------------------------------------------------------------------------
# 2 & 3. One rebuild at a time, held until the thread is done
# ---------------------------------------------------------------------------


@pytest.fixture
def blocking_vacuum(monkeypatch):
    """Park the real rebuild on an event so a second request can be tried.

    Barriers, not sleeps: `entered` says the thread is inside the vacuum with
    the gate held, `release` lets it finish.
    """
    from api.routers import storage

    entered = threading.Event()
    release = threading.Event()

    def _fake_vacuum():
        entered.set()
        assert release.wait(10), "the test never released the vacuum"
        return 1_000, 900

    monkeypatch.setattr(storage, "_vacuum", _fake_vacuum)
    try:
        yield entered, release
    finally:
        release.set()


async def test_a_second_optimize_is_refused_while_the_first_is_still_rebuilding(blocking_vacuum, free_gate):
    entered, release = blocking_vacuum
    first = asyncio.create_task(_post())
    await asyncio.to_thread(entered.wait, 10)

    second = await _post()
    assert second.status_code == 409
    assert "already running" in second.json()["detail"]
    assert "storage.optimize" in second.json()["detail"], "409s name the holder — 'busy' is not actionable"

    release.set()
    assert (await first).status_code == 200


async def test_cancelling_the_request_does_not_release_the_gate_early(blocking_vacuum, free_gate):
    """The verified defect: cancel the HTTP waiter and a NEW optimize was
    admitted, because there was no in-flight state for it to collide with."""
    entered, release = blocking_vacuum
    first = asyncio.create_task(_post())
    await asyncio.to_thread(entered.wait, 10)

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    assert free_gate.status()["exclusive"] == "storage.optimize", "the thread still holds the writer lock"
    assert (await _post()).status_code == 409

    release.set()
    assert await asyncio.to_thread(free_gate.wait_until_free, 10), "the thread must release what it took"
    assert (await _post()).status_code == 200


async def test_only_one_vacuum_body_ever_runs_at_a_time(monkeypatch, free_gate):
    """Counted rather than inferred: two `_vacuum()` bodies overlapped."""
    from api.routers import storage

    lock = threading.Lock()
    running = 0
    peak = 0
    entered = threading.Event()
    proceed = threading.Event()

    def _counting_vacuum():
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        entered.set()
        assert proceed.wait(10), "the test never released the vacuum"
        with lock:
            running -= 1
        return 1_000, 900

    monkeypatch.setattr(storage, "_vacuum", _counting_vacuum)
    first = asyncio.create_task(_post())
    await asyncio.to_thread(entered.wait, 10)

    refused = [await _post() for _ in range(3)]

    proceed.set()
    assert (await first).status_code == 200
    assert peak == 1, "two rebuilds held the writer lock at once"
    assert [r.status_code for r in refused] == [409, 409, 409]


async def test_a_turn_that_starts_after_the_check_makes_optimization_decline(monkeypatch, live_turn, free_gate):
    """The check-to-thread window. Work started in it used to run against the
    rebuild; now the thread re-asks, and it is optimization that backs off."""
    from api.routers import storage

    def _too_late():
        pytest.fail("the vacuum ran despite a turn having started")

    monkeypatch.setattr(storage, "_vacuum", _too_late)

    real_turn_in_flight = storage._turn_in_flight
    seen = {"n": 0}

    def _busy_on_the_second_ask():
        seen["n"] += 1
        if seen["n"] > 1:
            live_turn(_Turn(session_type="cron"))
        return real_turn_in_flight()

    monkeypatch.setattr(storage, "_turn_in_flight", _busy_on_the_second_ask)

    resp = await _post()
    assert resp.status_code == 409
    assert seen["n"] == 2, "the thread must ask again — the handler's answer is already stale"
    assert free_gate.status()["exclusive"] is None, "a declined rebuild hands the slot straight back"


# ---------------------------------------------------------------------------
# The gate itself, driven directly
# ---------------------------------------------------------------------------


def test_an_exclusive_phase_waits_for_an_announced_writer():
    """Drain, on a private gate so the barriers are the only timing there is."""
    gate = _DatabaseGate()
    entered = threading.Event()
    let_go = threading.Event()
    drained = threading.Event()

    def _writer():
        with gate.writing("test:snapshot"):
            entered.set()
            assert let_go.wait(10)

    def _drainer():
        gate.drain(10)
        drained.set()

    writer = threading.Thread(target=_writer)
    writer.start()
    assert entered.wait(10)

    gate.claim("test:rebuild")
    drainer = threading.Thread(target=_drainer)
    drainer.start()
    assert not drained.is_set(), "a rebuild must not start on top of an announced writer"

    let_go.set()
    assert drained.wait(10), "and it must start once the writer is gone"
    writer.join(10)
    drainer.join(10)
    gate.release()


def test_a_writer_arriving_during_the_exclusive_phase_is_refused_not_admitted():
    gate = _DatabaseGate()
    gate.claim("test:rebuild")
    with pytest.raises(MaintenanceBusy) as caught:
        with gate.writing("test:incremental-vacuum", wait=0):
            pytest.fail("admitted into an exclusive phase")
    assert caught.value.holder == "test:rebuild"
    gate.release()
    # And admitted again the moment the phase ends — the gate is a gate, not a ban.
    with gate.writing("test:incremental-vacuum", wait=0):
        assert gate.status()["writers"] == ["test:incremental-vacuum"]


def test_a_writer_that_will_not_finish_defers_the_rebuild_and_hands_the_slot_back():
    """A drain timeout must not leave the gate closed behind it."""
    gate = _DatabaseGate()
    entered = threading.Event()
    let_go = threading.Event()

    def _writer():
        with gate.writing("test:stuck-checkpoint"):
            entered.set()
            assert let_go.wait(10)

    writer = threading.Thread(target=_writer)
    writer.start()
    assert entered.wait(10)

    with pytest.raises(MaintenanceBusy) as caught:
        with gate.exclusive("test:rebuild", drain_timeout=0):
            pytest.fail("rebuilt on top of a writer that never drained")
    assert caught.value.writers == ("test:stuck-checkpoint",)
    assert gate.status()["exclusive"] is None, "a deferred rebuild leaves no lock behind"

    let_go.set()
    writer.join(10)


def test_two_claimants_cannot_both_hold_the_slot():
    gate = _DatabaseGate()
    gate.claim("test:first")
    with pytest.raises(MaintenanceBusy) as caught:
        gate.claim("test:second")
    assert caught.value.holder == "test:first"
    gate.release()
    gate.claim("test:second")  # released means released
    gate.release()
