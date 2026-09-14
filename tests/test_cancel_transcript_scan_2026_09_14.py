"""Cancel and delete read the whole transcript to stamp three ids.

`drop_pending_for_cancel` called `db.get_orphaned_user_messages`, which
selects every row of the session — content included — and JSON-parses each
one in Python. It ran on the event loop, and both `/cancel` and the delete
path reached it, so a bulk purge of N sessions paid for N full transcript
loads there.

It also cost the cancel its own correctness. The running turn's user row
stays `delivery_status='pending'` until the delivery acknowledge lands, which
is *after* the assistant row is saved. The explicit `turn_has_assistant_row`
guard above the scan therefore decided not to stamp that row, and the scan
put it straight back — a cancel landing in that window marked an answered
message cancelled.

Now: a narrow id-only query, off the loop on the route, skipped entirely when
the rows are about to be deleted, and the guard has the final say.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import api.routers.sessions as sessions_router
from db import models as db
from sessions.manager import SessionManager
from sessions.state import PendingMessage


@pytest.fixture
def mgr(monkeypatch):
    fresh = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", fresh)
    monkeypatch.setattr("api.routers.sessions.get_manager", lambda: fresh)
    return fresh


@pytest.fixture
def calls(monkeypatch):
    """Record which of the two queries the cancel path asks for.

    Assert on the call, not a stopwatch — the 3.2.2 lesson. A wall-clock
    threshold would pass on a fast box with the wide read still in place.
    """
    seen: dict[str, list[str]] = {"wide": [], "narrow": []}
    wide = db.get_orphaned_user_messages
    narrow = db.get_pending_user_message_ids

    def spy_wide(session_id):
        seen["wide"].append(session_id)
        return wide(session_id)

    def spy_narrow(session_id):
        seen["narrow"].append(session_id)
        return narrow(session_id)

    monkeypatch.setattr("sessions.manager.db.get_orphaned_user_messages", spy_wide)
    monkeypatch.setattr("sessions.manager.db.get_pending_user_message_ids", spy_narrow)
    return seen


def _queue(mgr, sid, text: str) -> int:
    """Persist a queued prompt the way prompt() does, and queue it."""
    meta = json.dumps({"delivery_status": "pending"})
    mid = db.add_message(sid, "user", text, metadata=meta)
    mgr.get(sid).pending_messages.append(PendingMessage(text, mid, False))
    return mid


def _start_turn(mgr, sid, text: str) -> int:
    mid = db.add_message(sid, "user", text, metadata=json.dumps({"delivery_status": "pending"}))
    session = mgr.get(sid)
    session.current_turn_user_msg_id = mid
    return mid


def _meta(mid: int) -> dict:
    return json.loads(db.get_message(mid)["metadata"] or "{}")


# ---------------------------------------------------------------------------
# The query
# ---------------------------------------------------------------------------


async def test_cancel_asks_for_ids_not_for_the_transcript(mgr, calls):
    sid = mgr.create_session(title="cancel query")
    running = _start_turn(mgr, sid, "the running turn")
    queued = _queue(mgr, sid, "queued-A")

    dropped = await mgr.drop_pending_for_cancel_async(mgr.get(sid))

    assert dropped == 1
    assert calls["wide"] == []
    assert calls["narrow"] == [sid]
    assert _meta(running)["cancelled"] is True
    assert _meta(queued)["cancelled"] is True


async def test_the_http_cancel_route_uses_the_narrow_query_too(mgr, calls):
    sid = mgr.create_session(title="route cancel query")
    _start_turn(mgr, sid, "the running turn")
    _queue(mgr, sid, "queued-A")

    assert await sessions_router.cancel_session(sid) == {"status": "cancelled"}

    assert calls["wide"] == []
    assert calls["narrow"] == [sid]


async def test_delete_skips_the_scan_entirely(mgr, calls):
    sid = mgr.create_session(title="delete skips")
    _start_turn(mgr, sid, "the running turn")
    _queue(mgr, sid, "queued-A")

    await mgr.delete_session_async(sid)

    assert calls["wide"] == []
    assert calls["narrow"] == []
    assert db.get_session(sid) is None


def test_the_narrow_query_finds_the_same_pending_rows_the_wide_one_did(mgr):
    """Equivalence, so the narrowing is a cost change and not a behaviour one."""
    sid = mgr.create_session(title="equivalence")
    pending = [
        db.add_message(sid, "user", f"pending-{n}", metadata=json.dumps({"delivery_status": "pending"}))
        for n in range(3)
    ]
    db.add_message(sid, "user", "consumed", metadata=json.dumps({"delivery_status": "consumed"}))
    db.add_message(sid, "assistant", "an answer")
    db.add_message(sid, "user", "no metadata at all")
    already = db.add_message(sid, "user", "already stamped", metadata=json.dumps({"delivery_status": "pending"}))
    db.mark_messages_cancelled([already])
    db.add_message(sid, "user", "broken metadata", metadata="{not json")

    wide = set()
    for row in db.get_orphaned_user_messages(sid):
        try:
            if json.loads(row.get("metadata") or "{}").get("delivery_status") == "pending":
                wide.add(row["id"])
        except ValueError:
            pass  # the old filter had no such guard — see the next test
    assert set(db.get_pending_user_message_ids(sid)) == wide == set(pending)


async def test_one_unparseable_row_no_longer_costs_the_whole_stamp(mgr):
    """The old filter ran json.loads inside a generator expression, so a row
    with malformed metadata raised into the surrounding except and the cancel
    stamped nothing at all. SQLite's json_valid() just skips the row."""
    sid = mgr.create_session(title="broken metadata")
    db.add_message(sid, "user", "broken", metadata="{not json")
    queued = _queue(mgr, sid, "queued-A")

    await mgr.drop_pending_for_cancel_async(mgr.get(sid))

    assert _meta(queued)["cancelled"] is True


async def test_the_scan_stays_off_the_event_loop(mgr, monkeypatch):
    """The narrow query is cheap, but "cheap" is not "on the loop"."""
    loop = asyncio.get_running_loop()
    ran_on: list[bool] = []
    real = db.get_pending_user_message_ids

    def spy(session_id):
        try:
            asyncio.get_running_loop()
            ran_on.append(True)
        except RuntimeError:
            ran_on.append(False)
        return real(session_id)

    monkeypatch.setattr("sessions.manager.db.get_pending_user_message_ids", spy)
    sid = mgr.create_session(title="off loop")
    _start_turn(mgr, sid, "the running turn")
    await mgr.drop_pending_for_cancel_async(mgr.get(sid))
    assert loop.is_running()
    assert ran_on == [False]


# ---------------------------------------------------------------------------
# The guard the scan used to bypass
# ---------------------------------------------------------------------------


async def test_a_turn_that_already_answered_is_not_stamped_cancelled(mgr):
    """A cancel landing between the assistant row and the delivery
    acknowledge: the row is still 'pending', so the scan returns it, but the
    turn has spoken and recovery leaves it alone. So must the stamp."""
    sid = mgr.create_session(title="answered turn")
    running = _start_turn(mgr, sid, "the running turn")
    db.add_message(sid, "assistant", "working on it...")  # saved; not yet acknowledged
    queued = _queue(mgr, sid, "queued-A")

    await mgr.drop_pending_for_cancel_async(mgr.get(sid))

    assert "cancelled" not in _meta(running)
    assert _meta(running)["delivery_status"] == "pending"
    # The queued row behind it is still dropped — the running turn's assistant
    # rows sit after it by id, so guarding it would exempt work that never ran.
    assert _meta(queued)["cancelled"] is True
    assert _meta(queued)["delivery_status"] == "cancelled"


async def test_an_unanswered_running_turn_is_still_stamped(mgr):
    """The half of the guard that must keep working: cancel during scout or
    the first stream leaves the orphan shape, and the prompt the user just
    stopped is the one that would come back."""
    sid = mgr.create_session(title="unanswered turn")
    running = _start_turn(mgr, sid, "the running turn")

    await mgr.drop_pending_for_cancel_async(mgr.get(sid))

    assert _meta(running)["cancelled"] is True
    assert db.get_orphaned_user_messages(sid) == []


def test_the_synchronous_form_applies_the_same_guard(mgr):
    """cancel_session and the worker cascade take the sync path; they must not
    be the ones that stamp an answered turn."""
    sid = mgr.create_session(title="sync guard")
    running = _start_turn(mgr, sid, "the running turn")
    db.add_message(sid, "assistant", "working on it...")

    mgr.cancel_session(mgr.get(sid))

    assert "cancelled" not in _meta(running)
