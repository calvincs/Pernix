"""Live debug 2026-10-31: a model that repeats one call gets an answer forced."""

import asyncio
import json
from types import SimpleNamespace

import core.agent as agent
from core.agent import LOOP_BREAK_AT, StuckDetector, _handle_stuck_signals, run_agent


class _Registry:
    def __init__(self, polls=()):
        self._polls = polls

    def exists(self, name):
        return True

    def get(self, name):
        return SimpleNamespace(idempotent=name not in self._polls)


def _call(name="bash", **args):
    return {"name": name, "arguments": json.dumps(args)}


def _send(stuck, call, registry=None, times=1):
    for _ in range(times):
        stuck.evaluate("", [call], {}, registry or _Registry())


def _handle(stuck, tools, monkeypatch):
    saved = []
    monkeypatch.setattr(agent.db, "add_message", lambda *a, **k: saved.append(a[2]))
    action, _ = asyncio.run(
        _handle_stuck_signals(
            session_id="s",
            score=0.4,
            repeats=1,
            tool_calls=[_call()],
            active_tools=tools,
            nudges_used=0,
            nudge_limit=3,
            stuck=stuck,
        )
    )
    return action, saved


def test_third_identical_call_turns_tools_off(monkeypatch):
    stuck = StuckDetector()
    _send(stuck, _call(command="ls"), times=LOOP_BREAK_AT)
    action, saved = _handle(stuck, ["bash", "ask_user"], monkeypatch)
    assert action == "nudge-and-retry"
    assert stuck.force_text
    assert "Tools are off" in saved[0]


def test_two_identical_calls_do_not_break(monkeypatch):
    stuck = StuckDetector()
    _send(stuck, _call(command="ls"), times=LOOP_BREAK_AT - 1)
    assert stuck.identical_calls == LOOP_BREAK_AT - 1
    assert _handle(stuck, ["bash", "ask_user"], monkeypatch)[0] == "proceed"
    assert not stuck.force_text


def test_a_change_between_runs_starts_the_count_again():
    stuck = StuckDetector()
    _send(stuck, _call(command="pytest"), times=2)
    stuck.mutation_epoch += 1
    _send(stuck, _call(command="pytest"))
    assert stuck.identical_calls == 1


def test_waiting_commands_and_poll_tools_never_count():
    stuck = StuckDetector()
    _send(stuck, _call(command="sleep 30 && tail log"), times=5)
    assert stuck.identical_calls == 0
    _send(stuck, _call("job_status", job_id="1"), _Registry(polls=("job_status",)), times=5)
    assert stuck.identical_calls == 0


def test_a_session_without_a_user_keeps_its_tools(monkeypatch):
    stuck = StuckDetector()
    _send(stuck, _call(command="ls"), times=LOOP_BREAK_AT)
    _handle(stuck, ["bash"], monkeypatch)
    assert not stuck.force_text


def test_the_break_fires_once_per_turn(monkeypatch):
    stuck = StuckDetector()
    _send(stuck, _call(command="ls"), times=LOOP_BREAK_AT)
    _handle(stuck, ["bash", "ask_user"], monkeypatch)
    stuck.force_text = False
    _send(stuck, _call(command="ls"))
    _handle(stuck, ["bash", "ask_user"], monkeypatch)
    assert not stuck.force_text


async def test_a_repeating_model_is_made_to_answer_through_the_real_loop(monkeypatch):
    from core.llm.types import StreamEvent, StreamEventType, TokenUsage, ToolCall
    from core.scout.report import ScoutReport
    from core.tools.registry import ToolRegistry
    from db import models as db
    from sessions.state import AgentSession
    from tests.conftest import FakeLLMClient

    def _call_round(n):
        return [
            StreamEvent(
                type=StreamEventType.TOOL_CALL,
                tool_calls=[ToolCall(id=f"tc{n}", name="bash", arguments='{"command": "ls"}')],
            ),
            StreamEvent(type=StreamEventType.DONE),
        ]

    answer = [
        StreamEvent(type=StreamEventType.TOKEN, content="The folder never changed."),
        StreamEvent(type=StreamEventType.USAGE, usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2)),
        StreamEvent(type=StreamEventType.DONE),
    ]
    runs = []
    fake = FakeLLMClient(stream_events=[_call_round(n) for n in range(4)] + [answer])
    tools_seen = []
    real_stream = fake.chat_stream

    def _stream(*args, **kwargs):
        tools_seen.append(kwargs.get("tools"))
        return real_stream(*args, **kwargs)

    fake.chat_stream = _stream
    monkeypatch.setattr("core.agent.get_llm_client", lambda: fake)
    monkeypatch.setattr("core.llm.client._client", fake)

    reg = ToolRegistry()

    def _bash(command=""):
        runs.append(command)
        return "same output"

    reg.register(
        name="bash",
        func=_bash,
        description="bash",
        parameters={"type": "object", "properties": {"command": {"type": "string"}}},
        parallel_safe=True,
        timeout=5,
        idempotent=False,
    )
    reg.register(
        name="ask_user",
        func=lambda question="": "ok",
        description="ask",
        parameters={"type": "object", "properties": {}},
        parallel_safe=True,
        timeout=5,
    )
    monkeypatch.setattr("core.agent.get_registry", lambda: reg)

    sid = db.create_session(title="Loop break")
    session = AgentSession(session_id=sid, session_type="normal")
    session.last_scout_report = ScoutReport(recommended_tools=["bash", "ask_user"])
    await run_agent(sid, "list the folder", session)

    assert len(runs) == 3
    assert tools_seen[-1] is None
    assert all(t for t in tools_seen[:-1])
    assert session.termination_reason == "stuck_loop"
    final = [m for m in db.get_messages(sid) if m["role"] == "assistant"][-1]
    assert final["content"] == "The folder never changed."
