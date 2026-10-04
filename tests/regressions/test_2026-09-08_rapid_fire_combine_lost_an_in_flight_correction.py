"""A correction sent during a streaming answer was written down and never read.

Within the three-second rapid-fire window the manager folds a follow-up into
the running turn's user row instead of opening a turn for it —
`_can_absorb_rapid_fire` allowed that as long as the DB held no final answer
yet. Mid-tool-loop that is safe: the agent re-compiles the row at the top of
every round. But once the model has begun streaming a text-only answer there
is no next round: `run_agent` saved the answer it had computed against the old
snapshot and returned. The combined row then had an assistant message after
it, so `get_orphaned_user_messages` did not flag it either, and nothing —
not the turn, not post-turn recovery, not the next prompt — ever read the
correction. The transcript showed it; the model never saw it.

The row now carries a version the combiner bumps and the agent records at each
compile. A final answer that lands on a moved version buys one more pass.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from core.agent import run_agent
from core.llm.types import StreamEvent, StreamEventType, TokenUsage
from sessions import state_v2 as sv2
from sessions.manager import SessionManager
from tests.conftest import FakeLLMClient


def _answer(text: str) -> list[StreamEvent]:
    return [
        StreamEvent(type=StreamEventType.TOKEN, content=text),
        StreamEvent(
            type=StreamEventType.USAGE,
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        ),
        StreamEvent(type=StreamEventType.DONE),
    ]


class _BarrierClient(FakeLLMClient):
    """Records the compiled request, then holds the first stream open."""

    def __init__(self, streams):
        super().__init__(stream_events=streams)
        self.compiled = asyncio.Event()
        self.release = asyncio.Event()

    async def chat_stream(self, messages, tools=None, model="", **kwargs):
        self.calls.append({"messages": messages, "tools": tools, "model": model})
        index = self.call_count
        self.call_count += 1
        if index == 0:
            self.compiled.set()
            await self.release.wait()
        for event in self.stream_events[index % len(self.stream_events)]:
            yield event


def _history(call) -> str:
    return "\n".join(str(m.get("content") or "") for m in call["messages"])


@pytest.fixture
def running_turn(monkeypatch):
    """A session mid-turn on one persisted user message."""
    from db import models as db

    manager = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", manager)
    sid = db.create_session(title="t")
    session = manager.get_or_create(sid)
    session._state_v2 = sv2.SessionStateV2.PROCESSING

    running_id = db.add_message(sid, "user", "summarise report.txt in one line")
    session.last_user_msg_id = running_id
    session.current_turn_user_msg_id = running_id
    session.last_user_msg_at = time.monotonic()
    return manager, session, sid, running_id


def _install(monkeypatch, client):
    from core.tools.registry import ToolRegistry

    monkeypatch.setattr("core.agent.get_llm_client", lambda: client)
    monkeypatch.setattr("core.llm.client._client", client)
    monkeypatch.setattr("core.agent.get_registry", lambda: ToolRegistry())


async def test_a_correction_sent_during_the_answer_reaches_the_model(running_turn, monkeypatch):
    manager, session, sid, running_id = running_turn
    client = _BarrierClient([_answer("Here is the one-line summary."), _answer("Three lines, then.")])
    _install(monkeypatch, client)

    turn = asyncio.create_task(run_agent(sid, "summarise report.txt in one line", session, pre_saved=True))
    await asyncio.wait_for(client.compiled.wait(), 10)

    await manager.prompt(sid, "actually make it three lines")
    assert not session.pending_messages, "the combiner absorbed it — that is the shape under test"

    client.release.set()
    await asyncio.wait_for(turn, 10)

    assert client.call_count == 2, "the answer must not end the turn on a stale snapshot"
    assert "actually make it three lines" in _history(client.calls[1]), "the second request must carry the correction"
    assert "Here is the one-line summary." in _history(client.calls[1]), "and the answer it is amending"

    from db import models as db

    answers = [m["content"] for m in db.get_messages(sid) if m["role"] == "assistant"]
    assert answers == ["Here is the one-line summary.", "Three lines, then."]
    assert session.termination_reason == "complete"


async def test_an_uncorrected_answer_still_ends_the_turn(running_turn, monkeypatch):
    """Control: no combine, no extra pass."""
    manager, session, sid, running_id = running_turn
    client = _BarrierClient([_answer("Here is the one-line summary."), _answer("should never run")])
    _install(monkeypatch, client)

    turn = asyncio.create_task(run_agent(sid, "summarise report.txt in one line", session, pre_saved=True))
    await asyncio.wait_for(client.compiled.wait(), 10)
    client.release.set()
    await asyncio.wait_for(turn, 10)

    assert client.call_count == 1
    assert session.turn.user_row_version == 0


async def test_the_re_read_is_granted_once_not_per_correction(running_turn, monkeypatch):
    """A user who keeps typing gets one pass, not an unbounded loop."""
    manager, session, sid, running_id = running_turn
    client = _BarrierClient([_answer("first"), _answer("second"), _answer("third")])
    _install(monkeypatch, client)

    async def combine_again(*_a, **_kw):
        # Stand in for a correction landing during every stream.
        session.turn.user_row_version += 1

    turn = asyncio.create_task(run_agent(sid, "summarise report.txt in one line", session, pre_saved=True))
    await asyncio.wait_for(client.compiled.wait(), 10)
    await combine_again()
    client.release.set()
    await asyncio.wait_for(turn, 10)

    assert client.call_count == 2, "one extra pass, then the turn ends"


async def test_the_combiner_versions_only_the_running_turns_row(running_turn):
    """A queued entry is read when it pops; it needs no version bump."""
    from db import models as db
    from sessions.state import PendingMessage

    manager, session, sid, _running_id = running_turn
    queued_id = db.add_message(sid, "user", "later")
    session.last_user_msg_id = queued_id
    session.last_user_msg_at = time.monotonic()
    session.pending_messages.append(PendingMessage("later", "", True, 0.0, queued_id))

    await manager.prompt(sid, "and also this")

    assert "and also this" in db.get_message(queued_id)["content"]
    assert session.turn.user_row_version == 0
