"""Integration tests for core/agent.py: run_agent end-to-end."""

import asyncio
import json

import pytest

from core.agent import _build_resource_status, run_agent
from core.llm.types import StreamEvent, StreamEventType, TokenUsage, ToolCall
from core.scout.report import ScoutReport
from sessions.state import AgentSession


def _make_session(session_id="agent-test", session_type="normal") -> AgentSession:
    """Create a fresh AgentSession."""
    return AgentSession(session_id=session_id, session_type=session_type)


def _token_event(content: str) -> StreamEvent:
    return StreamEvent(type=StreamEventType.TOKEN, content=content)


def _done_event() -> StreamEvent:
    return StreamEvent(type=StreamEventType.DONE)


def _usage_event() -> StreamEvent:
    return StreamEvent(
        type=StreamEventType.USAGE,
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )


def _tool_event(name: str, args: dict, tc_id: str = "tc1") -> StreamEvent:
    return StreamEvent(
        type=StreamEventType.TOOL_CALL,
        tool_calls=[ToolCall(id=tc_id, name=name, arguments=json.dumps(args))],
    )


def _setup_fake_llm(monkeypatch, stream_events: list):
    """Monkeypatch get_llm_client to return a streaming fake."""
    from tests.conftest import FakeLLMClient

    fake = FakeLLMClient(stream_events=[stream_events])
    monkeypatch.setattr("core.agent.get_llm_client", lambda: fake)
    monkeypatch.setattr("core.llm.client._client", fake)
    return fake


def _setup_registry(monkeypatch, tools: dict | None = None):
    """Set up a minimal tool registry."""
    from core.tools.registry import ToolRegistry

    reg = ToolRegistry()
    if tools:
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


# ---------------------------------------------------------------------------
# _build_resource_status
# ---------------------------------------------------------------------------


def test_build_resource_status():
    from db import models as db

    sid = db.create_session(title="Resource Test")
    db.add_token_usage(sid, total_tokens=5000, prompt_tokens=3000, completion_tokens=2000)
    from core.context.tokens import get_estimator

    estimator = get_estimator()
    status = _build_resource_status(sid, estimator)
    assert isinstance(status, str)


# ---------------------------------------------------------------------------
# run_agent: text response (no tool calls)
# ---------------------------------------------------------------------------


async def test_run_agent_text_response(monkeypatch):
    """Agent with LLM returning text only saves message and emits done."""
    from db import models as db

    sid = db.create_session(title="Text Response")

    session = _make_session(sid)
    # Set up a ScoutReport so the agent has tools to use
    session.last_scout_report = ScoutReport(
        recommended_tools=["file_read"],
        approach_guidance="Be direct",
    )

    _setup_fake_llm(
        monkeypatch,
        [
            _token_event("Hello! I can help with that."),
            _usage_event(),
            _done_event(),
        ],
    )
    _setup_registry(monkeypatch, {"file_read": lambda path="": "content"})

    events = []
    session.subscribers.append(asyncio.Queue(maxsize=100))

    await run_agent(sid, "Hello", session)

    # Check that assistant message was saved
    messages = db.get_messages(sid)
    assistant_msgs = [m for m in messages if m["role"] == "assistant"]
    assert len(assistant_msgs) >= 1
    assert "Hello" in assistant_msgs[0]["content"] or "help" in assistant_msgs[0]["content"]


# ---------------------------------------------------------------------------
# run_agent: tool call then text response
# ---------------------------------------------------------------------------


async def test_run_agent_tool_call(monkeypatch):
    """Agent calls a tool then returns text response."""
    from db import models as db

    sid = db.create_session(title="Tool Call Test")
    session = _make_session(sid)
    session.last_scout_report = ScoutReport(recommended_tools=["echo_tool"])

    # First LLM response: tool call; second: text
    from tests.conftest import FakeLLMClient

    fake = FakeLLMClient(
        stream_events=[
            [
                _tool_event("echo_tool", {"message": "hello"}, "tc1"),
                _usage_event(),
                _done_event(),
            ],
            [
                _token_event("Done! The tool returned 'hello'."),
                _usage_event(),
                _done_event(),
            ],
        ]
    )
    monkeypatch.setattr("core.agent.get_llm_client", lambda: fake)

    def echo_tool(message=""):
        return f"Echo: {message}"

    reg = _setup_registry(monkeypatch, {"echo_tool": echo_tool})

    await run_agent(sid, "echo something", session)

    # Tool message should be saved
    messages = db.get_messages(sid)
    tool_msgs = [m for m in messages if m["role"] == "tool"]
    assert len(tool_msgs) >= 1


# ---------------------------------------------------------------------------
# run_agent: hallucinated tool
# ---------------------------------------------------------------------------


async def test_run_agent_hallucinated_tool(monkeypatch):
    """Agent calling a nonexistent tool gets an error message."""
    from db import models as db

    sid = db.create_session(title="Hallucinated Tool")
    session = _make_session(sid)

    from tests.conftest import FakeLLMClient

    fake = FakeLLMClient(
        stream_events=[
            [
                _tool_event("nonexistent_magic_tool", {}, "tc1"),
                _usage_event(),
                _done_event(),
            ],
            [
                _token_event("OK I'll use a different approach."),
                _usage_event(),
                _done_event(),
            ],
        ]
    )
    monkeypatch.setattr("core.agent.get_llm_client", lambda: fake)
    _setup_registry(monkeypatch)  # empty registry

    await run_agent(sid, "do something", session)

    messages = db.get_messages(sid)
    tool_msgs = [m for m in messages if m["role"] == "tool"]
    # Should have an error message about the nonexistent tool
    assert any("does not exist" in m.get("content", "") for m in tool_msgs)


# ---------------------------------------------------------------------------
# run_agent: malformed JSON arguments
# ---------------------------------------------------------------------------


async def test_run_agent_malformed_args(monkeypatch):
    """Agent with malformed tool args gets an error and continues."""
    from db import models as db

    sid = db.create_session(title="Malformed Args")
    session = _make_session(sid)

    from tests.conftest import FakeLLMClient

    bad_call = StreamEvent(
        type=StreamEventType.TOOL_CALL,
        tool_calls=[ToolCall(id="tc1", name="echo_tool", arguments="{invalid json}")],
    )
    fake = FakeLLMClient(
        stream_events=[
            [bad_call, _usage_event(), _done_event()],
            [_token_event("Corrected."), _usage_event(), _done_event()],
        ]
    )
    monkeypatch.setattr("core.agent.get_llm_client", lambda: fake)
    _setup_registry(monkeypatch, {"echo_tool": lambda message="": "ok"})

    await run_agent(sid, "test malformed", session)

    messages = db.get_messages(sid)
    tool_msgs = [m for m in messages if m["role"] == "tool"]
    assert any("parse" in m.get("content", "").lower() or "JSON" in m.get("content", "") for m in tool_msgs)


# ---------------------------------------------------------------------------
# run_agent: cancel requested
# ---------------------------------------------------------------------------


async def test_run_agent_cancel(monkeypatch):
    """Agent stops when cancel_requested is set."""
    from db import models as db

    sid = db.create_session(title="Cancel Test")
    session = _make_session(sid)
    session.cancel_requested = True  # Set before agent starts

    _setup_fake_llm(monkeypatch, [_token_event("Should not be reached"), _done_event()])
    _setup_registry(monkeypatch)

    # Should return immediately
    await run_agent(sid, "do something", session)

    # No assistant messages should be saved (cancelled before LLM call)
    messages = db.get_messages(sid)
    assistant_msgs = [m for m in messages if m["role"] == "assistant"]
    assert len(assistant_msgs) == 0


# ---------------------------------------------------------------------------
# run_agent: retry mode (is_retry=True)
# ---------------------------------------------------------------------------


async def test_run_agent_retry_skips_user_message(monkeypatch):
    """In retry mode, user message is not added again."""
    from db import models as db

    sid = db.create_session(title="Retry Test")
    db.add_message(sid, "user", "Original request")  # Already in DB
    session = _make_session(sid)

    _setup_fake_llm(
        monkeypatch,
        [
            _token_event("Retry response."),
            _usage_event(),
            _done_event(),
        ],
    )
    _setup_registry(monkeypatch)

    msg_count_before = len([m for m in db.get_messages(sid) if m["role"] == "user"])
    await run_agent(sid, "Original request", session, is_retry=True)
    msg_count_after = len([m for m in db.get_messages(sid) if m["role"] == "user"])

    # No new user message should be added on retry
    assert msg_count_after == msg_count_before


# ---------------------------------------------------------------------------
# Alias note persistence (C1): when a hallucinated tool name is rewritten
# to an active alias, the note MUST survive normalize_for_openrouter — it
# rides on the tool-role result, not a mid-conversation system message.
# ---------------------------------------------------------------------------


async def test_run_agent_alias_note_on_tool_result(monkeypatch):
    """Aliased call → tool result content begins with the rewrite note."""
    from core.context.compiler import normalize_for_openrouter
    from db import models as db

    sid = db.create_session(title="Alias Note")
    session = _make_session(sid)
    # Scout includes get_worker_result in active_tools (enables alias).
    session.last_scout_report = ScoutReport(recommended_tools=["get_worker_result"])

    from tests.conftest import FakeLLMClient

    fake = FakeLLMClient(
        stream_events=[
            # Model hallucinates get_worker_output — aliased to get_worker_result.
            [_tool_event("get_worker_output", {"worker_id": "abc"}, "tc1"), _usage_event(), _done_event()],
            # Final text response after the tool ran.
            [_token_event("Done."), _usage_event(), _done_event()],
        ]
    )
    monkeypatch.setattr("core.agent.get_llm_client", lambda: fake)

    def get_worker_result(worker_id="", _context=None):
        return "worker output here"

    _setup_registry(monkeypatch, {"get_worker_result": get_worker_result})

    await run_agent(sid, "go", session)

    tool_msgs = [m for m in db.get_messages(sid) if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    content = tool_msgs[0]["content"]
    # The note is on the tool result, not a separate system message.
    assert content.startswith("[note: tool name aliased get_worker_output → get_worker_result]")
    assert "worker output here" in content

    # Crucially: the note must survive OpenRouter's mid-system strip.
    # Build a synthetic message list including the tool row and check that
    # normalization keeps the tool content intact.
    msgs_in = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "tc1", "type": "function", "function": {"name": "get_worker_result", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "tc1", "content": content},
    ]
    out = normalize_for_openrouter(msgs_in)
    # Tool message preserved with alias note intact.
    tool_out = [m for m in out if m.get("role") == "tool"]
    assert len(tool_out) == 1
    assert "[note: tool name aliased" in tool_out[0]["content"]


async def test_run_agent_alias_skipped_when_target_not_active(monkeypatch):
    """If the aliased-target isn't in active_tools, alias must NOT fire — the
    hallucinated name falls through to the hint path instead."""
    from core.tools.registry import ToolRegistry
    from db import models as db

    sid = db.create_session(title="Alias Gated")
    session = _make_session(sid)
    # Scout recommended nothing; `get_worker_result` is registered as an
    # extension (not builtin) so it is NOT auto-promoted into active_tools.
    session.last_scout_report = ScoutReport(recommended_tools=[])

    from tests.conftest import FakeLLMClient

    fake = FakeLLMClient(
        stream_events=[
            [_tool_event("get_worker_output", {"worker_id": "abc"}, "tc1"), _usage_event(), _done_event()],
            [_token_event("ok"), _usage_event(), _done_event()],
        ]
    )
    monkeypatch.setattr("core.agent.get_llm_client", lambda: fake)

    def get_worker_result(worker_id="", _context=None):
        return "worker output here"

    reg = ToolRegistry()
    reg.register(
        name="get_worker_result",
        func=get_worker_result,
        description="get worker output",
        parameters={"type": "object", "properties": {}},
        parallel_safe=True,
        timeout=5,
        source="extension",  # extension, not builtin — no auto-promotion
    )
    monkeypatch.setattr("core.agent.get_registry", lambda: reg)

    await run_agent(sid, "go", session)

    tool_msgs = [m for m in db.get_messages(sid) if m["role"] == "tool"]
    # Alias did NOT fire — the tool was treated as hallucinated, we got a
    # hint message (or an error) instead of the real tool output.
    assert len(tool_msgs) == 1
    content = tool_msgs[0]["content"]
    assert "does not exist" in content
    assert "worker output here" not in content


async def test_run_agent_termination_reason_complete(monkeypatch):
    """Natural exit (text response, no tool calls) sets reason=complete."""
    from db import models as db

    sid = db.create_session(title="Termination Complete")
    session = _make_session(sid)
    _setup_fake_llm(monkeypatch, [_token_event("answer"), _usage_event(), _done_event()])
    _setup_registry(monkeypatch)
    await run_agent(sid, "hi", session)
    assert session.termination_reason == "complete"


async def test_run_agent_termination_reason_cancelled(monkeypatch):
    """Pre-turn cancel sets reason=cancelled."""
    from db import models as db

    sid = db.create_session(title="Termination Cancelled")
    session = _make_session(sid)
    session.cancel_requested = True
    _setup_fake_llm(monkeypatch, [_done_event()])
    _setup_registry(monkeypatch)
    await run_agent(sid, "hi", session)
    assert session.termination_reason == "cancelled"


# ---------------------------------------------------------------------------
# Bug C — _tried_fallback must be sticky across rounds
# ---------------------------------------------------------------------------


async def test_fallback_sticky_across_rounds(monkeypatch):
    """Once the primary model fails and we fall back in round 0, the fallback
    flag must persist for the rest of the turn. Subsequent rounds must go
    directly to the fallback model without re-attempting the failing primary.

    Before the fix: _tried_fallback = False was reset inside the outer
    while tool_round loop, so every round re-attempted the primary.
    After the fix: _tried_fallback lives outside the loop and is sticky.
    """
    from core.llm.types import StreamEvent, StreamEventType, ToolCall
    from core.scout.report import ScoutReport
    from db import models as db
    from tests.conftest import FakeLLMClient

    # "vendor/primary" resolves to openrouter; "local-fallback" to ollama.
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-for-fallback-test")
    monkeypatch.setattr("config.settings.llm_model", "vendor/primary-model")
    monkeypatch.setattr("config.settings.fallback_model", "local-fallback")
    monkeypatch.setattr("config.settings.max_tool_rounds", 10)

    # Track every (model, attempt_type) call to verify fallback stickiness.
    models_called: list[str] = []
    fallback_round = 0

    class FallbackTrackingClient(FakeLLMClient):
        async def chat_stream(self, messages, tools=None, model="", **kwargs):
            nonlocal fallback_round
            models_called.append(model)
            self.calls.append({"messages": messages, "tools": tools, "model": model})
            self.call_count += 1

            if "/" in model:
                # Primary / OpenRouter model → non-retryable error (429-style).
                # _is_stream_retryable returns False for this, so agent goes
                # straight to fallback without sleeping.
                yield StreamEvent(type=StreamEventType.ERROR, error="429 rate limit exceeded")
            else:
                # Fallback (local) model.
                fallback_round += 1
                if fallback_round == 1:
                    # Round 0's fallback: emit a tool call to trigger a second round.
                    yield StreamEvent(
                        type=StreamEventType.TOOL_CALL,
                        tool_calls=[ToolCall(id="tc1", name="noop_tool", arguments="{}")],
                    )
                else:
                    # Round 1's fallback: emit a final text response to end the loop.
                    yield StreamEvent(type=StreamEventType.TOKEN, content="all done")
                yield StreamEvent(type=StreamEventType.DONE)

        def resolve_provider(self, model=""):
            return "openrouter" if "/" in model else "ollama"

    fake = FallbackTrackingClient()
    monkeypatch.setattr("core.agent.get_llm_client", lambda: fake)

    # Register a no-op tool so the agent can actually call it.
    from core.tools.registry import ToolRegistry

    reg = ToolRegistry()

    def noop_tool() -> str:
        return "ok"

    reg.register(
        name="noop_tool",
        func=noop_tool,
        description="no-op",
        parameters={"type": "object", "properties": {}},
        parallel_safe=True,
        timeout=5,
    )
    monkeypatch.setattr("core.agent.get_registry", lambda: reg)

    sid = db.create_session(title="Fallback Sticky Test")
    session = _make_session(sid)
    session.last_scout_report = ScoutReport(recommended_tools=["noop_tool"])

    await run_agent(sid, "go", session)

    primary_calls = [m for m in models_called if "/" in m]
    fallback_calls = [m for m in models_called if "/" not in m]

    assert len(primary_calls) == 1, (
        f"Primary model must be attempted exactly ONCE (round 0 only); "
        f"got {len(primary_calls)}. Full call sequence: {models_called}"
    )
    assert len(fallback_calls) >= 2, (
        f"Fallback model must handle all subsequent rounds; "
        f"got {len(fallback_calls)}. Full call sequence: {models_called}"
    )


async def test_fallback_fires_on_same_provider(monkeypatch):
    """A fallback model on the SAME provider is still a viable failover
    target (model-specific failures, per-model rate buckets). The old gate
    required a different provider, so an Ollama-primary/Ollama-fallback
    config silently had no failover at all (audit P1i)."""
    from core.llm.types import StreamEvent, StreamEventType
    from core.scout.report import ScoutReport
    from db import models as db
    from tests.conftest import FakeLLMClient

    monkeypatch.setattr("config.settings.llm_model", "local-primary")
    monkeypatch.setattr("config.settings.fallback_model", "local-fallback")

    models_called: list[str] = []

    class SameProviderClient(FakeLLMClient):
        async def chat_stream(self, messages, tools=None, model="", **kwargs):
            models_called.append(model)
            self.call_count += 1
            if model == "local-primary":
                yield StreamEvent(type=StreamEventType.ERROR, error="429 rate limit exceeded")
            else:
                yield StreamEvent(type=StreamEventType.TOKEN, content="rescued by sibling model")
                yield StreamEvent(type=StreamEventType.DONE)

        def resolve_provider(self, model=""):
            return "ollama"  # both models resolve to the same provider

    fake = SameProviderClient()
    monkeypatch.setattr("core.agent.get_llm_client", lambda: fake)

    sid = db.create_session(title="Same Provider Fallback Test")
    session = _make_session(sid)
    session.last_scout_report = ScoutReport(recommended_tools=[])

    await run_agent(sid, "go", session)

    assert "local-fallback" in models_called, (
        f"Same-provider fallback must be attempted after the primary fails; "
        f"call sequence: {models_called}"
    )
    msgs = db.get_messages(sid)
    assert any(
        m["role"] == "assistant" and "rescued by sibling model" in (m["content"] or "") for m in msgs
    ), "Fallback response must be persisted as the assistant turn"
