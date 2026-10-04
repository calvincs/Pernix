"""Commands crossing turn boundaries must retain their delivery and outcome.

The audit found that corrections accepted during a final stream disappeared,
late pauses left a dead task busy, retry outcomes described the first attempt,
and rejected answers nevertheless closed their questions.
"""

import asyncio
import json
import sqlite3
from unittest.mock import AsyncMock

import pytest

from config import settings
from db import models as db
from db.database import connect_sessions
from sessions import state_v2 as sv2
from sessions.manager import SessionManager, TurnExecution
from sessions.state import PendingMessage


@pytest.fixture
def manager(monkeypatch):
    manager = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", manager)
    monkeypatch.setattr(settings, "goals_enabled", False)
    monkeypatch.setattr(settings, "reflect_max_retries", 3)
    monkeypatch.setattr(manager, "_run_post_hooks", AsyncMock())
    return manager


def make_run(manager):
    sid = manager.create_session(title="lifecycle regression")
    return manager.get(sid), TurnExecution(sid)


@pytest.mark.parametrize("failing_attempt,expected", [(1, "completed"), (2, "failed")])
async def test_execution_records_outcome_after_retry(manager, monkeypatch, failing_attempt, expected):
    session, execution = make_run(manager)
    attempts = 0

    async def pipeline(session, message, **kwargs):
        nonlocal attempts
        attempts += 1
        sv2.transition(session, sv2.S.PROCESSING, "scout-done")
        if attempts == failing_attempt:
            raise RuntimeError("attempt failed")
        session.termination_reason = "complete"

    async def hooks(session):
        if session.turn.reflect_count == 0:
            session.turn.reflect_count = 1
            session.turn.reflect_retry_requested = True

    monkeypatch.setattr(manager, "_run_scout_and_process", pipeline)
    monkeypatch.setattr(manager, "_run_post_hooks", hooks)
    session.task = asyncio.create_task(manager._run_agent_safe(session, "", "", execution=execution))
    await session.task
    assert attempts == 2
    assert execution.result == expected
    assert execution.error == session.error
    assert execution.termination_reason == session.termination_reason


async def test_cancel_during_verification_settles_cancel_and_restores_model(manager, monkeypatch):
    session, execution = make_run(manager)
    entered = asyncio.Event()

    async def pipeline(session, message, **kwargs):
        sv2.transition(session, sv2.S.PROCESSING, "scout-done")
        session.termination_reason = "complete"
        session._model_before_agent_switch = "baseline"
        session.model_override = "temporary"

    async def hooks(session):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(manager, "_run_scout_and_process", pipeline)
    monkeypatch.setattr(manager, "_run_post_hooks", hooks)
    session.task = asyncio.create_task(manager._run_agent_safe(session, "", "", execution=execution))
    await entered.wait()
    manager.cancel_session(session)
    await session.task
    assert execution.result == "cancelled"
    assert execution.termination_reason == "cancelled"
    assert session.model_override == "baseline"
    assert sv2._current_state(session) is sv2.S.IDLE_READY


async def test_next_run_cannot_change_previous_execution_outcome(manager, monkeypatch):
    session, execution = make_run(manager)
    queued = db.add_message(session.session_id, "user", "next")
    session.pending_messages.append(PendingMessage("next", "", True, 0, queued))
    observed = []

    async def pipeline(session, message, **kwargs):
        sv2.transition(session, sv2.S.PROCESSING, "scout-done")
        if message == "next":
            observed.append(execution.result)
            session.termination_reason = "complete"
        else:
            raise RuntimeError("first failed")

    monkeypatch.setattr(manager, "_run_scout_and_process", pipeline)
    first = session.task = asyncio.create_task(manager._run_agent_safe(session, "", "", execution=execution))
    await first
    await session.task
    assert observed == ["failed"]
    assert execution.error == "first failed"


@pytest.mark.parametrize("refusal", ["shutting_down", "queue_full", "no_agent_runner"])
async def test_rejected_answer_stays_open(manager, monkeypatch, refusal):
    from fastapi import HTTPException

    from api.routers.questions import answer_question

    session, _ = make_run(manager)
    session._state_v2 = sv2.S.AWAITING_USER
    qid = db.add_question(session.session_id, "Which file?")
    blocker = None
    if refusal == "shutting_down":
        manager.close_admission()
    elif refusal == "queue_full":
        monkeypatch.setattr(settings, "max_pending_messages", 1)
        session.pending_messages.append(PendingMessage("existing"))
        blocker = session.task = asyncio.create_task(asyncio.Event().wait())
    try:
        with pytest.raises(HTTPException) as error:
            await answer_question(qid, {"answer": "report.txt"})
        assert error.value.status_code == 409
        assert db.get_questions(session.session_id)[0]["id"] == qid
        assert not db.get_messages(session.session_id)
    finally:
        if blocker:
            blocker.cancel()
            await asyncio.gather(blocker, return_exceptions=True)


def test_answer_and_message_rollback_together(manager):
    session, _ = make_run(manager)
    qid = db.add_question(session.session_id, "Which file?")
    with connect_sessions() as conn:
        conn.execute("CREATE TRIGGER reject_message BEFORE INSERT ON messages BEGIN SELECT RAISE(ABORT, 'test'); END")
    with pytest.raises(sqlite3.IntegrityError):
        db.add_message(session.session_id, "user", "answer", question_id=qid, question_answer="report.txt")
    assert db.get_questions(session.session_id)[0]["id"] == qid


def test_invalid_edge_does_not_change_state_or_turn(manager):
    session, _ = make_run(manager)
    assert sv2.transition(session, sv2.S.FINALIZING, "prompt-arrived") is False
    assert sv2._current_state(session) is sv2.S.IDLE_READY
    assert session._turn_id == 0
    assert not session.events


def test_transition_snapshot_and_log_rollback_together(manager):
    session, _ = make_run(manager)
    with connect_sessions() as conn:
        conn.execute(
            "CREATE TRIGGER reject_state BEFORE UPDATE OF state_v2 ON sessions BEGIN SELECT RAISE(ABORT, 'test'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        sv2.transition(session, sv2.S.SCOUTING, "prompt-arrived")
    assert sv2._current_state(session) is sv2.S.IDLE_READY
    assert not db.get_state_log(session.session_id)
    assert not session.events


async def test_pause_and_resume_before_checkpoint_are_valid(manager):
    session, _ = make_run(manager)
    session._state_v2 = sv2.S.PROCESSING
    session.task = asyncio.current_task()
    assert manager.control_session(session.session_id, "pause")["status"] == "pause_requested"
    assert manager.control_session(session.session_id, "resume")["status"] == "resumed"
    assert sv2._current_state(session) is sv2.S.PROCESSING
    assert all(not row["reason"].startswith("invariant") for row in db.get_state_log(session.session_id))


@pytest.mark.parametrize("state", [sv2.S.SCOUTING, sv2.S.COMPACTING, sv2.S.IDLE_READY])
async def test_pause_endpoint_reports_actual_refusal(manager, state):
    from fastapi import HTTPException

    from api.routers.sessions import http_pause_session

    session, _ = make_run(manager)
    session._state_v2 = state
    session.task = asyncio.current_task()
    assert not manager.get_status(session.session_id)["capabilities"]["pause"]
    with pytest.raises(HTTPException) as error:
        await http_pause_session(session.session_id)
    assert error.value.status_code == 409
    assert session.pause_event.is_set()


def test_unread_correction_survives_answer_and_rehydration(manager):
    session, _ = make_run(manager)
    mid = db.add_message(
        session.session_id,
        "user",
        "correction",
        metadata=json.dumps({"injected": True, "delivery_kind": "correction", "delivery_status": "pending"}),
    )
    db.add_message(session.session_id, "assistant", "An answer compiled before the correction")
    restored = SessionManager()
    rows = restored._find_db_orphans(restored.get_or_create(session.session_id))
    assert mid in [row["id"] for row in rows]
    db.set_message_delivery(session.session_id, [mid], "consumed")
    assert not restored._find_db_orphans(restored.get(session.session_id))


@pytest.fixture
def streaming_run(manager, monkeypatch):
    from core.agent import run_agent
    from core.context.tokens import TokenEstimator
    from core.llm.types import StreamEvent, StreamEventType
    from core.tools.registry import ToolRegistry
    from tests.conftest import FakeLLMClient

    # The regression concerns delivery boundaries, not tokenizer downloads.
    estimator = TokenEstimator.__new__(TokenEstimator)
    estimator._enc = None
    monkeypatch.setattr("core.context.tokens._estimator", estimator)

    class Client(FakeLLMClient):
        def __init__(self):
            super().__init__()
            self.compiled = asyncio.Event()
            self.release = asyncio.Event()
            self.second_compiled = asyncio.Event()
            self.second_release = asyncio.Event()
            self.second_release.set()

        async def chat_stream(self, messages, **kwargs):
            self.calls.append({"messages": messages})
            self.call_count += 1
            if self.call_count == 1:
                self.compiled.set()
                await self.release.wait()
            if self.call_count == 2:
                self.second_compiled.set()
                await self.second_release.wait()
            yield StreamEvent(type=StreamEventType.TOKEN, content="Here is the answer.")
            yield StreamEvent(type=StreamEventType.DONE)

    client = Client()
    monkeypatch.setattr("core.agent.get_llm_client", lambda: client)
    monkeypatch.setattr("core.llm.client._client", client)
    monkeypatch.setattr("core.agent.get_registry", lambda: ToolRegistry())

    async def pipeline(session, message, **kwargs):
        sv2.transition(session, sv2.S.PROCESSING, "scout-done")
        await run_agent(session.session_id, message, session, pre_saved=True)

    monkeypatch.setattr(manager, "_run_scout_and_process", pipeline)
    session, execution = make_run(manager)
    mid = db.add_message(session.session_id, "user", "summarize the report")
    session.current_turn_user_msg_id = mid
    session.last_user_msg_id = mid
    return session, execution, client


async def start_stream(manager, streaming_run):
    session, execution, client = streaming_run
    session.task = asyncio.create_task(
        manager._run_agent_safe(session, "summarize the report", "", pre_saved=True, execution=execution)
    )
    await asyncio.wait_for(client.compiled.wait(), 10)
    return session, execution, client


async def test_browser_correction_during_final_answer_is_consumed(manager, streaming_run):
    from api.routers.chat import inject

    session, _, client = await start_stream(manager, streaming_run)
    result = await inject({"session_id": session.session_id, "message": "Actually use three lines"})
    client.release.set()
    await asyncio.wait_for(session.task, 10)
    assert client.call_count == 2
    assert "Actually use three lines" in str(client.calls[1]["messages"])
    assert json.loads(db.get_message(result["message_id"])["metadata"])["delivery_status"] == "consumed"
    assert not session.pending_messages


async def test_pause_during_final_answer_finishes_ready(manager, streaming_run):
    session, _, client = await start_stream(manager, streaming_run)
    manager.control_session(session.session_id, "pause")
    client.release.set()
    await asyncio.wait_for(session.task, 10)
    assert sv2._current_state(session) is sv2.S.IDLE_READY
    assert session.pause_event.is_set()
    assert not manager.get_status(session.session_id)["capabilities"]["resume"]


async def test_paused_correction_steers_same_run(manager, streaming_run):
    session, _, client = await start_stream(manager, streaming_run)
    manager.control_session(session.session_id, "pause")
    await manager.steer(session.session_id, "Actually use three lines")
    client.release.set()

    async def wait_paused():
        while sv2._current_state(session) is not sv2.S.PAUSED:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_paused(), 10)
    await manager.steer(session.session_id, "Include the totals")
    assert not session.pending_messages
    manager.control_session(session.session_id, "resume")
    await asyncio.wait_for(session.task, 10)
    assert session._turn_id == 1
    assert "Include the totals" in str(client.calls[-1]["messages"])


def test_pending_correction_survives_compaction_and_later_prompts_stay_queued(manager, streaming_run):
    from core.context.compiler import compile_context

    session, _, _ = streaming_run
    correction = db.add_message(
        session.session_id,
        "user",
        "Unread correction before compaction",
        metadata=json.dumps({"injected": True, "delivery_status": "pending", "delivery_kind": "correction"}),
    )
    db.add_message(
        session.session_id,
        "compaction",
        "Earlier conversation summary",
        metadata=json.dumps({"compacted_up_to": correction}),
    )
    queued = db.add_message(
        session.session_id,
        "user",
        "For the next turn",
        metadata=json.dumps({"delivery_status": "pending", "delivery_kind": "user"}),
    )
    payload = compile_context(session.session_id, turn_user_msg_id=session.current_turn_user_msg_id)
    assert correction in payload.delivered_message_ids
    assert queued not in payload.delivered_message_ids
    assert "Unread correction before compaction" in str(payload.messages)
    assert "For the next turn" not in str(payload.messages)
    assert all(not key.startswith("_") for message in payload.messages for key in message)


async def test_cancel_retires_unread_injections(manager):
    session, _ = make_run(manager)
    session._state_v2 = sv2.S.PROCESSING
    session.task = asyncio.current_task()
    result = await manager.steer(session.session_id, "Do this instead")
    manager.drop_pending_for_cancel(session)
    assert not manager._find_db_orphans(session)
    assert json.loads(db.get_message(result["message_id"])["metadata"])["cancelled"] is True


async def test_answer_is_admitted_once_and_closed_atomically(manager, monkeypatch):
    from fastapi import HTTPException

    from api.routers.questions import answer_question

    session, _ = make_run(manager)
    session._state_v2 = sv2.S.AWAITING_USER
    qid = db.add_question(session.session_id, "Which file?")
    manager._agent_runner = AsyncMock()
    monkeypatch.setattr(manager, "_run_agent_safe", AsyncMock())
    await answer_question(qid, {"answer": "report.txt"})
    await session.task
    assert not db.get_questions(session.session_id)
    assert len([m for m in db.get_messages(session.session_id) if m["role"] == "user"]) == 1
    with pytest.raises(HTTPException):
        await answer_question(qid, {"answer": "report.txt"})


@pytest.mark.parametrize("error", [None, "final provider failed"])
async def test_final_synthesis_acknowledges_only_successful_delivery(manager, monkeypatch, error):
    from types import SimpleNamespace

    from core.agent import _stream_final_answer
    from core.llm.stream_ladder import StreamOutcome

    session, _ = make_run(manager)
    mid = db.add_message(
        session.session_id,
        "user",
        "correction",
        metadata=json.dumps({"delivery_kind": "correction", "delivery_status": "pending"}),
    )
    payload = SimpleNamespace(
        messages=[], static_prefix_chars=0, effective_max_output=100, delivered_message_ids=(mid,)
    )
    monkeypatch.setattr("core.agent.compile_context", lambda **kwargs: payload)
    monkeypatch.setattr(
        "core.agent.stream_with_failover", AsyncMock(return_value=StreamOutcome(error=error, model="test"))
    )
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
        save_turn_msg=AsyncMock(),
        sched_created_at=0,
        sched_priority=0,
        tried_fallback=False,
        last_usage=None,
    )
    assert json.loads(db.get_message(mid)["metadata"])["delivery_status"] == ("pending" if error else "consumed")
    assert session.error == error
    if error:
        assert session.termination_reason == "error"


def test_timeline_map_covers_the_enforced_transition_graph():
    import re
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "static/js/components/modals/timeline.js").read_text()
    edges = re.findall(r"\{ from: '([^']+)', to: '([^']+)'", source)
    assert len(edges) == len(set(edges))
    assert set(edges) == {(start.value, end.value) for (start, _), end in sv2.TRANSITIONS.items()}


async def test_correction_after_extra_pass_is_recovered_as_next_turn(manager, streaming_run):
    session, execution, client = await start_stream(manager, streaming_run)
    first = session.task
    client.second_release.clear()
    await manager.steer(session.session_id, "First correction")
    client.release.set()
    await asyncio.wait_for(client.second_compiled.wait(), 10)
    result = await manager.steer(session.session_id, "Correction after the extra pass")
    client.second_release.set()
    await asyncio.wait_for(first, 10)
    assert execution.result == "completed"
    assert session.task is not first
    await asyncio.wait_for(session.task, 10)
    assert client.call_count == 3
    assert "Correction after the extra pass" in str(client.calls[-1]["messages"])
    assert json.loads(db.get_message(result["message_id"])["metadata"])["delivery_status"] == "settled"
    assert not manager._find_db_orphans(session)
