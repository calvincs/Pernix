"""A replayed answer could not say which model wrote it, or what it cost.

Both facts existed at save time — the round's resolved model id (which is
not always the session default: a failover swaps it mid-turn) and the round
latency — and both were dropped on the floor for the assistant row that
carries the answer. Reopening a transcript, or scrolling back a day later,
left no way to tell a primary-model reply from a fallback one.

They now ride along in the row's `metadata` JSON, which the transcript reads
back to draw the per-message model/latency chip. No schema change: the
column has been there all along.
"""

import json

import pytest

from core.agent import run_agent
from core.llm.types import StreamEvent, StreamEventType, TokenUsage, ToolCall
from core.scout.report import ScoutReport
from sessions.state import AgentSession


def _token(content):
    return StreamEvent(type=StreamEventType.TOKEN, content=content)


def _usage():
    return StreamEvent(
        type=StreamEventType.USAGE,
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )


def _done():
    return StreamEvent(type=StreamEventType.DONE)


def _tool(name, args, tc_id="tc1"):
    return StreamEvent(
        type=StreamEventType.TOOL_CALL,
        tool_calls=[ToolCall(id=tc_id, name=name, arguments=json.dumps(args))],
    )


def _registry(monkeypatch, tools):
    from core.tools.registry import ToolRegistry

    reg = ToolRegistry()
    for name, fn in tools.items():
        reg.register(
            name=name,
            func=fn,
            description=name,
            parameters={"type": "object", "properties": {}},
            parallel_safe=True,
            timeout=5,
        )
    monkeypatch.setattr("core.agent.get_registry", lambda: reg)
    return reg


def _assistant_rows(sid):
    from db import models as db

    return [m for m in db.get_messages(sid) if m["role"] == "assistant"]


@pytest.mark.asyncio
async def test_final_answer_row_carries_model_and_latency(monkeypatch):
    from db import models as db
    from tests.conftest import FakeLLMClient

    monkeypatch.setattr("config.settings.llm_model", "chip-model-1")
    sid = db.create_session(title="Chip")
    session = AgentSession(session_id=sid)
    session.last_scout_report = ScoutReport(recommended_tools=["file_read"])

    fake = FakeLLMClient(stream_events=[[_token("Here you go."), _usage(), _done()]])
    monkeypatch.setattr("core.agent.get_llm_client", lambda: fake)
    monkeypatch.setattr("core.llm.client._client", fake)
    _registry(monkeypatch, {"file_read": lambda path="": "content"})

    await run_agent(sid, "hello", session)

    answers = [m for m in _assistant_rows(sid) if (m["content"] or "").strip()]
    assert answers, "no assistant answer was saved"
    meta = json.loads(answers[-1]["metadata"] or "{}")
    assert meta["model"] == "chip-model-1"
    assert isinstance(meta["latency_ms"], int)
    assert meta["latency_ms"] >= 0
    # The pre-existing turn tagging must survive the addition.
    assert "parent_user_msg_id" in meta


@pytest.mark.asyncio
async def test_tool_round_row_also_carries_the_model(monkeypatch):
    """The row that holds the tool_calls is an assistant row too."""
    from db import models as db
    from tests.conftest import FakeLLMClient

    monkeypatch.setattr("config.settings.llm_model", "chip-model-2")
    sid = db.create_session(title="Chip rounds")
    session = AgentSession(session_id=sid)
    session.last_scout_report = ScoutReport(recommended_tools=["echo_tool"])

    fake = FakeLLMClient(
        stream_events=[
            [_tool("echo_tool", {"message": "hi"}), _usage(), _done()],
            [_token("Done."), _usage(), _done()],
        ]
    )
    monkeypatch.setattr("core.agent.get_llm_client", lambda: fake)
    monkeypatch.setattr("core.llm.client._client", fake)
    _registry(monkeypatch, {"echo_tool": lambda message="": f"Echo: {message}"})

    await run_agent(sid, "echo hi", session)

    rows = _assistant_rows(sid)
    assert rows, "no assistant rows saved"
    models = [json.loads(r["metadata"] or "{}").get("model") for r in rows]
    assert models and all(m == "chip-model-2" for m in models), models
    # A tool row is not an assistant row and must not grow a model key.
    tool_rows = [m for m in db.get_messages(sid) if m["role"] == "tool"]
    for r in tool_rows:
        assert "model" not in json.loads(r["metadata"] or "{}")
