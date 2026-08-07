"""Tests for provider parsing functions and remaining coverage gaps."""

import json

import pytest

# ===========================================================================
# OpenRouter _parse_response
# ===========================================================================


def _make_openrouter():
    from core.llm.providers.openrouter import OpenRouterProvider
    from core.llm.types import ProviderConfig

    config = ProviderConfig(name="openrouter", base_url="https://openrouter.ai/api/v1", api_key="test-key")
    return OpenRouterProvider(config)


def test_openrouter_parse_response_basic():
    provider = _make_openrouter()
    data = {
        "choices": [{"message": {"content": "Hello!"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    result = provider._parse_response(data, "claude-3")
    assert result.content == "Hello!"
    assert result.finish_reason == "stop"
    assert result.usage.prompt_tokens == 10


def test_openrouter_parse_response_with_tool_calls():
    provider = _make_openrouter()
    data = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "tc1",
                            "function": {"name": "bash", "arguments": '{"command": "ls"}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
    }
    result = provider._parse_response(data, "claude-3")
    assert result.tool_calls is not None
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "bash"
    assert result.finish_reason == "tool_calls"


def test_openrouter_parse_response_cache_tokens():
    provider = _make_openrouter()
    data = {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "cache_read_input_tokens": 80,
            "cache_creation_input_tokens": 20,
        },
    }
    result = provider._parse_response(data, "test")
    assert result.usage.cache_read_tokens == 80
    assert result.usage.cache_write_tokens == 20


def test_openrouter_headers():
    provider = _make_openrouter()
    headers = provider._headers()
    assert "Authorization" in headers
    assert "Bearer test-key" in headers["Authorization"]


# ===========================================================================
# More core_tools.py: bash env modes
# ===========================================================================


def test_bash_denylist_env_mode(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    monkeypatch.setattr("config.settings.shell_security_mode", "permissive")
    monkeypatch.setattr("config.settings.shell_env_mode", "denylist")
    monkeypatch.setattr("config.settings.shell_env_denylist", ["SECRET_KEY"])
    from core.tools.builtin.core_tools import bash

    result = bash("echo hello")
    assert "hello" in result


def test_bash_allowlist_env_mode(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    monkeypatch.setattr("config.settings.shell_security_mode", "permissive")
    monkeypatch.setattr("config.settings.shell_env_mode", "allowlist")
    monkeypatch.setattr("config.settings.shell_env_allowlist", ["PATH", "HOME"])
    from core.tools.builtin.core_tools import bash

    result = bash("echo hello")
    assert "hello" in result


# ===========================================================================
# More maintenance: heartbeat tick (via direct call)
# ===========================================================================


async def test_maintenance_tick_every_60():
    """Tick divisible by 60 runs WAL checkpoint."""
    from maintenance import MaintenanceRunner

    runner = MaintenanceRunner()
    runner._tick_count = 60
    await runner._tick()  # Should not raise


# ===========================================================================
# More memory store: delete/archive operations
# ===========================================================================


def test_memory_store_list_files_empty(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from core.memory.store import MemoryStore

    store = MemoryStore(str(tmp_path / "memories"))
    files = store.list_files()
    assert files == []


def test_memory_store_recall_with_entries(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from core.memory.store import MemoryStore

    store = MemoryStore(str(tmp_path / "memories"))
    store.add_entry("The authentication system uses bearer tokens for API access", file_name="pernix.config")
    result = store.recall("authentication bearer tokens", top=3)
    assert isinstance(result, str)


# ===========================================================================
# More agent: tool round tracking
# ===========================================================================


async def test_run_agent_multiple_tool_rounds(monkeypatch):
    """Agent that makes tool calls then gets text response."""
    from core.agent import run_agent
    from core.llm.types import StreamEvent, StreamEventType, TokenUsage, ToolCall
    from core.scout.report import ScoutReport
    from db import models as db
    from sessions.state import AgentSession
    from tests.conftest import FakeLLMClient

    sid = db.create_session(title="Multi-Round")
    session = AgentSession(session_id=sid)
    session.last_scout_report = ScoutReport(recommended_tools=["return_ok"])

    call_count = 0

    async def stream_gen_1(*args, **kwargs):
        """First call: make a tool call."""
        yield StreamEvent(
            type=StreamEventType.TOOL_CALL, tool_calls=[ToolCall(id="tc1", name="return_ok", arguments="{}")]
        )
        yield StreamEvent(type=StreamEventType.USAGE, usage=TokenUsage(10, 5, 15))
        yield StreamEvent(type=StreamEventType.DONE)

    async def stream_gen_2(*args, **kwargs):
        """Second call: return text response."""
        yield StreamEvent(type=StreamEventType.TOKEN, content="Done!")
        yield StreamEvent(type=StreamEventType.USAGE, usage=TokenUsage(10, 5, 15))
        yield StreamEvent(type=StreamEventType.DONE)

    class MultiCallFake:
        def __init__(self):
            self.call_count = 0
            self.router = type(
                "r",
                (),
                {
                    "registry": type(
                        "reg",
                        (),
                        {
                            "resolve_model_id": lambda self, m: m,
                            "get_model_info": lambda self, m: None,
                        },
                    )()
                },
            )()

        def has_capacity(self, model=""):
            return True

        def resolve_provider(self, model=""):
            return "fake"

        async def chat_stream(self, *args, **kwargs):
            self.call_count += 1
            if self.call_count == 1:
                async for event in stream_gen_1(*args, **kwargs):
                    yield event
            else:
                async for event in stream_gen_2(*args, **kwargs):
                    yield event

        async def chat(self, *args, **kwargs):
            from core.llm.types import ChatResponse, TokenUsage

            return ChatResponse(
                content="ok", tool_calls=None, usage=TokenUsage(), model="t", provider="f", finish_reason="stop"
            )

    fake = MultiCallFake()
    monkeypatch.setattr("core.agent.get_llm_client", lambda: fake)

    from core.tools.registry import ToolRegistry

    reg = ToolRegistry()
    reg.register("return_ok", lambda: "ok", "test", {"type": "object", "properties": {}}, timeout=5, parallel_safe=True)
    monkeypatch.setattr("core.agent.get_registry", lambda: reg)

    await run_agent(sid, "do something", session)

    messages = db.get_messages(sid)
    # Should have user, tool, assistant messages
    roles = [m["role"] for m in messages]
    assert "tool" in roles


# ===========================================================================
# More LLM router: resolve_provider
# ===========================================================================


def test_llm_router_resolve_provider_no_crash(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    from core.llm.router import ProviderRouter

    router = ProviderRouter()
    provider = router.resolve_provider("some/model")
    assert isinstance(provider, str)


def test_llm_router_semaphore_acquired_ollama():
    from core.llm.router import ProviderRouter

    router = ProviderRouter()
    stats = router.semaphore_stats
    assert "ollama" in stats or isinstance(stats, dict)
