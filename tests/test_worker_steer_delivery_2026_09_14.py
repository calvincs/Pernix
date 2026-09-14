"""message_worker claimed a delivery it had not made, on both branches.

On the event loop the tool cannot await its own steer — that would deadlock
against the worker's session lock — so it detached the coroutine and returned
"Message submitted to worker …". A rejection (the worker cancelling, the
manager draining for shutdown) was therefore invisible: the agent read
success and moved on.

Off the loop it called `.result(timeout=10)` and let the
`concurrent.futures.TimeoutError` raise into the tool — while the coroutine
kept running on the loop and could still deliver. The agent saw a failure,
retried, and the worker got the message twice.

Now the on-loop branch says plainly that nothing has been delivered yet and
names the two events that settle it, the detached task reports the outcome on
the parent's stream and in the parent's transcript, and the off-loop branch
cancels the future before reporting "NOT delivered", so a retry is safe.
"""

from __future__ import annotations

import asyncio

import pytest

from core.extensions.orchestration import message_worker
from db import models as db
from sessions import state_v2 as sv2
from sessions.manager import SessionManager


@pytest.fixture
def mgr(monkeypatch):
    fresh = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", fresh)
    monkeypatch.setattr("sessions.manager.get_manager", lambda: fresh)
    return fresh


@pytest.fixture
async def pair(mgr):
    """A parent and a worker parked mid-turn, so steering injects."""
    parent_id = mgr.create_session(title="parent")
    worker_id = mgr.create_session(title="worker", session_type="worker", parent_session_id=parent_id)
    worker = mgr.get(worker_id)
    hold = asyncio.Event()
    worker.task = asyncio.create_task(hold.wait())
    sv2.transition(worker, sv2.S.SCOUTING, "prompt-arrived")
    sv2.transition(worker, sv2.S.PROCESSING, "scout-done")
    yield parent_id, worker_id, worker
    hold.set()
    await worker.task


def _events(mgr, monkeypatch) -> list[tuple[str, dict]]:
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(mgr, "emit", lambda session_id, event: seen.append((session_id, event)))
    return seen


def _notes(session_id: str) -> list[str]:
    return [m["content"] for m in db.get_messages(session_id) if m["role"] == "system"]


async def _settle() -> None:
    """Let the detached reporter run to completion."""
    for _ in range(200):
        await asyncio.sleep(0.01)
        if not [t for t in asyncio.all_tasks() if t.get_name() == "worker-steering"]:
            return
    raise AssertionError("the detached steer never settled")


# ---------------------------------------------------------------------------
# On the loop: the return value stops claiming delivery
# ---------------------------------------------------------------------------


async def test_the_on_loop_return_does_not_claim_delivery(mgr, pair, monkeypatch):
    parent_id, worker_id, _worker = pair
    events = _events(mgr, monkeypatch)
    loop = asyncio.get_running_loop()

    out = message_worker(worker_id, "try the other branch", {"session_id": parent_id, "_loop": loop})

    assert "NOT yet delivered" in out
    assert "worker.steered" in out
    assert "submitted" not in out.lower()
    await _settle()

    steered = [e for sid, e in events if sid == parent_id and e["type"] == "worker.steered"]
    assert len(steered) == 1
    assert steered[0]["status"] == "injected"
    assert steered[0]["worker_id"] == worker_id
    assert any("Worker steer delivered" in n for n in _notes(parent_id))
    # And the worker really did get the row.
    assert any(m["content"] == "try the other branch" for m in db.get_messages(worker_id))


async def test_a_rejected_steer_reaches_the_parent(mgr, pair, monkeypatch):
    """The defect: a rejection was invisible because the tool had already
    said the message was submitted."""
    parent_id, worker_id, worker = pair
    worker.cancel_requested = True  # steer() refuses a cancelling session
    events = _events(mgr, monkeypatch)
    loop = asyncio.get_running_loop()

    out = message_worker(worker_id, "too late", {"session_id": parent_id, "_loop": loop})

    assert "NOT yet delivered" in out
    await _settle()

    rejected = [e for sid, e in events if sid == parent_id and e["type"] == "worker.steer_rejected"]
    assert len(rejected) == 1
    assert rejected[0]["reason"] == "cancelling"
    notes = _notes(parent_id)
    assert any("Worker steer NOT delivered" in n and "cancelling" in n for n in notes)
    assert not any(m["content"] == "too late" for m in db.get_messages(worker_id))


async def test_a_steer_that_raises_is_reported_rather_than_swallowed(mgr, pair, monkeypatch):
    parent_id, worker_id, _worker = pair
    events = _events(mgr, monkeypatch)
    loop = asyncio.get_running_loop()

    async def exploding(*_a, **_kw):
        raise RuntimeError("the lock went away")

    monkeypatch.setattr(mgr, "steer", exploding)
    message_worker(worker_id, "boom", {"session_id": parent_id, "_loop": loop})
    await _settle()

    rejected = [e for sid, e in events if e["type"] == "worker.steer_rejected"]
    assert len(rejected) == 1
    assert "the lock went away" in rejected[0]["reason"]
    assert any("NOT delivered" in n for n in _notes(parent_id))


# ---------------------------------------------------------------------------
# Off the loop: a timeout cancels the delivery it is giving up on
# ---------------------------------------------------------------------------


async def test_an_off_loop_timeout_cancels_the_delivery_and_says_so(mgr, pair, monkeypatch):
    """Retrying after this must be safe, which means the abandoned coroutine
    must not still be able to deliver."""
    from core.extensions import orchestration

    parent_id, worker_id, _worker = pair
    monkeypatch.setattr(orchestration, "STEER_DELIVERY_TIMEOUT", 0.2)
    started = asyncio.Event()
    outcome: list[str] = []

    async def never_finishes(*_a, **_kw):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            outcome.append("cancelled")
            raise
        outcome.append("delivered")

    monkeypatch.setattr(mgr, "steer", never_finishes)
    loop = asyncio.get_running_loop()

    out = await asyncio.to_thread(message_worker, worker_id, "slow one", {"session_id": parent_id, "_loop": loop})

    assert started.is_set()
    assert "NOT delivered" in out
    assert "Retrying is safe" in out
    for _ in range(200):
        await asyncio.sleep(0.01)
        if outcome:
            break
    assert outcome == ["cancelled"]


async def test_an_off_loop_success_still_reports_the_real_status(mgr, pair, monkeypatch):
    parent_id, worker_id, _worker = pair
    loop = asyncio.get_running_loop()

    out = await asyncio.to_thread(message_worker, worker_id, "in good time", {"session_id": parent_id, "_loop": loop})

    assert out == f"Message injected into worker {worker_id[:8]}"
    assert any(m["content"] == "in good time" for m in db.get_messages(worker_id))


async def test_an_off_loop_rejection_still_refuses(mgr, pair, monkeypatch):
    parent_id, worker_id, worker = pair
    worker.cancel_requested = True
    loop = asyncio.get_running_loop()

    out = await asyncio.to_thread(message_worker, worker_id, "too late", {"session_id": parent_id, "_loop": loop})

    assert out == "Refused: cancelling"
