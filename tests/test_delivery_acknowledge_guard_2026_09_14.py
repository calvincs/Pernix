"""A bookkeeping write could fail a round the model had already answered.

`core/agent.py` awaited `_acknowledge_delivery` unguarded at both of its call
sites — after a successful tool-round stream, and after the final synthesis.
Inside, `db.set_message_delivery` runs in a thread, so a transient sqlite
error (a lock past busy_timeout, a disk hiccup) propagated out of the await,
out of `run_agent`, and into `_run_agent_safe`'s except block. That
classified the turn `agent-error` and threw away the round's tool calls —
over a stamp that says which queued rows the request happened to carry.

Every other side-channel write in that region is best-effort. This one is
now too: the failure is a WARNING naming the ids, the rows stay `pending`
(which is the truth, and which orphan recovery already reads), and the turn
keeps its outcome.
"""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from config import settings
from core.agent import _acknowledge_delivery, _stream_final_answer
from db import models as db
from sessions import state_v2 as sv2
from sessions.manager import SessionManager, TurnExecution


@pytest.fixture
def manager(monkeypatch):
    mgr = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", mgr)
    monkeypatch.setattr(settings, "goals_enabled", False)
    monkeypatch.setattr(mgr, "_run_post_hooks", AsyncMock())
    return mgr


def make_run(mgr):
    sid = mgr.create_session(title="delivery acknowledge")
    return mgr.get(sid), TurnExecution(sid)


def pending_row(session_id: str, text: str = "correction") -> int:
    return db.add_message(
        session_id,
        "user",
        text,
        metadata=json.dumps({"delivery_kind": "correction", "delivery_status": "pending"}),
    )


def capture_events(session, monkeypatch) -> list[dict]:
    seen: list[dict] = []
    monkeypatch.setattr(session, "emit_event", seen.append)
    return seen


def break_the_write(monkeypatch) -> None:
    """Fail exactly the acknowledge's write.

    `core.agent.db` is `db.models`, so a blanket patch would also break the
    turn-settlement write in the manager's finally and confuse what is being
    asserted. 'consumed' is the acknowledge's status and nothing else's.
    """
    real = db.set_message_delivery

    def boom(session_id, message_ids, status):
        if status == "consumed":
            raise RuntimeError("database is locked")
        return real(session_id, message_ids, status)

    monkeypatch.setattr("core.agent.db.set_message_delivery", boom)


# ---------------------------------------------------------------------------
# The helper itself
# ---------------------------------------------------------------------------


async def test_a_failed_acknowledge_is_a_warning_not_an_exception(manager, monkeypatch, caplog):
    session, _ = make_run(manager)
    mid = pending_row(session.session_id)
    events = capture_events(session, monkeypatch)
    break_the_write(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="pernix.agent"):
        await _acknowledge_delivery(session, SimpleNamespace(delivered_message_ids=(mid,)))

    assert any(str(mid) in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    # The row keeps the truth: nothing was written, so nothing was consumed.
    assert json.loads(db.get_message(mid)["metadata"])["delivery_status"] == "pending"
    assert [e for e in events if e.get("type") == "message.consumed"] == []


async def test_a_successful_acknowledge_still_stamps_and_emits(manager, monkeypatch):
    session, _ = make_run(manager)
    mid = pending_row(session.session_id)
    events = capture_events(session, monkeypatch)

    await _acknowledge_delivery(session, SimpleNamespace(delivered_message_ids=(mid,)))

    assert json.loads(db.get_message(mid)["metadata"])["delivery_status"] == "consumed"
    assert [e["message_ids"] for e in events if e["type"] == "message.consumed"] == [[mid]]


async def test_cancellation_is_still_allowed_through(manager, monkeypatch):
    """Best-effort must not mean swallowing a cancel: a cancelled turn is not
    a failed write, and eating CancelledError here would make the turn
    uncancellable at this point."""
    session, _ = make_run(manager)
    mid = pending_row(session.session_id)

    def cancelled(*_a, **_kw):
        raise asyncio.CancelledError()

    monkeypatch.setattr("core.agent.db.set_message_delivery", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await _acknowledge_delivery(session, SimpleNamespace(delivered_message_ids=(mid,)))


# ---------------------------------------------------------------------------
# The call sites: the round keeps its outcome
# ---------------------------------------------------------------------------


async def test_the_final_synthesis_survives_a_failed_acknowledge(manager, monkeypatch):
    from core.llm.stream_ladder import StreamOutcome

    session, _ = make_run(manager)
    mid = pending_row(session.session_id)
    payload = SimpleNamespace(
        messages=[], static_prefix_chars=0, effective_max_output=100, delivered_message_ids=(mid,)
    )
    monkeypatch.setattr("core.agent.compile_context", lambda **kwargs: payload)
    monkeypatch.setattr(
        "core.agent.stream_with_failover",
        AsyncMock(return_value=StreamOutcome(content="the answer", error=None, model="test")),
    )
    saved = AsyncMock()
    events = capture_events(session, monkeypatch)
    break_the_write(monkeypatch)

    await _stream_final_answer(
        session=session,
        session_id=session.session_id,
        client=SimpleNamespace(resolve_provider=lambda model: "test"),
        scout_text="",
        resource_status="",
        supports_vision=False,
        supports_audio=False,
        context_budget=1000,
        max_output=100,
        model="test",
        turn_user_msg_id=mid,
        save_turn_msg=saved,
        sched_created_at=0,
        sched_priority=0,
        tried_fallback=False,
        last_usage=None,
    )

    # The answer is saved and the turn is closed out — the failed stamp
    # changed nothing about either.
    saved.assert_awaited_with("assistant", "the answer")
    assert session.error is None
    assert session.termination_reason is None
    assert [e["type"] for e in events if e["type"] == "stream.done"] == ["stream.done"]
    assert json.loads(db.get_message(mid)["metadata"])["delivery_status"] == "pending"


async def test_the_turn_is_not_reclassified_as_an_agent_error(manager, monkeypatch):
    """The whole point: the exception used to reach _run_agent_safe, which
    graded the turn agent-error and discarded the round's tool calls."""
    session, execution = make_run(manager)
    mid = pending_row(session.session_id)
    break_the_write(monkeypatch)

    async def pipeline(session, message, **kwargs):
        sv2.transition(session, sv2.S.PROCESSING, "scout-done")
        await _acknowledge_delivery(session, SimpleNamespace(delivered_message_ids=(mid,)))
        session.termination_reason = "complete"

    monkeypatch.setattr(manager, "_run_scout_and_process", pipeline)
    # The row stays pending, which is what makes the orphan sweep want to
    # re-queue it — correct downstream, and noise for this assertion.
    monkeypatch.setattr(manager, "_find_db_orphans", lambda session: [])
    session.task = asyncio.create_task(manager._run_agent_safe(session, "", "", execution=execution))
    await asyncio.wait_for(session.task, 10)

    assert session.error is None
    assert session.termination_reason == "complete"
    assert execution.result == "completed"
    assert execution.error is None
    assert sv2._current_state(session) is sv2.S.IDLE_READY
    log = db.get_state_log(session.session_id)
    assert "agent-error" not in {r["reason"] for r in log}
    assert json.loads(db.get_message(mid)["metadata"])["delivery_status"] == "pending"
