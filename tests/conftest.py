"""Pernix — Shared test fixtures."""

import asyncio
import os
import tempfile

import pytest

# Override data paths before importing anything
_tmpdir = tempfile.mkdtemp(prefix="pernix_test_")
os.environ.setdefault("CAI_TEST", "1")


@pytest.fixture(autouse=True)
def isolate_data(tmp_path, monkeypatch):
    """Isolate each test with temp data directories."""
    monkeypatch.setattr("config.settings.db_path", str(tmp_path / "sessions.db"))
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path / "workspace"))
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    monkeypatch.setattr("config.settings.telos_dir", str(tmp_path / "telos"))

    # Redirect settings.json writes to the temp directory so tests never
    # pollute data/settings.json with test values (e.g. "test-model-123").
    import config

    monkeypatch.setattr("config.SETTINGS_PATH", tmp_path / "settings.json")

    # Init DB for each test
    from db.database import init_db

    init_db()


# ---------------------------------------------------------------------------
# Fake LLM client for mocking LLM calls
# ---------------------------------------------------------------------------


class _FakeRegistry:
    """Stub for client.router.registry used by the agent."""

    def resolve_model_id(self, model_id: str) -> str:
        return model_id  # passthrough

    def get_model_info(self, model: str):
        return None  # unknown model — agent defaults to no vision


class _FakeRouter:
    """Stub for client.router used by the agent."""

    def __init__(self):
        self.registry = _FakeRegistry()


class FakeLLMClient:
    """Configurable mock for LLMClient.

    Usage:
        client = FakeLLMClient(responses=[ChatResponse(...)])
        client = FakeLLMClient(stream_events=[[StreamEvent(...), ...]])
    """

    def __init__(self, responses=None, stream_events=None):
        self.responses = responses or []
        self.stream_events = stream_events or []
        self.call_count = 0
        self.calls = []  # record (messages, kwargs) for assertions
        self.router = _FakeRouter()  # for agent: client.router.registry.resolve_model_id()

    async def chat(
        self,
        messages,
        tools=None,
        model="",
        max_tokens=None,
        temperature=None,
        session_id="",
        session_created_at=float("inf"),
        session_priority=2,
    ):
        self.calls.append(
            {
                "messages": messages,
                "tools": tools,
                "model": model,
                "session_id": session_id,
                "session_created_at": session_created_at,
                "session_priority": session_priority,
            }
        )
        if self.responses:
            resp = self.responses[self.call_count % len(self.responses)]
            self.call_count += 1
            return resp
        from core.llm.types import ChatResponse, TokenUsage

        self.call_count += 1
        return ChatResponse(
            content="fake response",
            tool_calls=None,
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            model=model or "test-model",
            provider="fake",
            finish_reason="stop",
        )

    async def chat_stream(
        self,
        messages,
        tools=None,
        model="",
        max_tokens=None,
        temperature=None,
        session_id="",
        session_created_at=float("inf"),
        session_priority=2,
    ):
        self.calls.append({"messages": messages, "tools": tools, "model": model})
        if self.stream_events:
            events = self.stream_events[self.call_count % len(self.stream_events)]
            self.call_count += 1
            for event in events:
                yield event
        else:
            from core.llm.types import StreamEvent, StreamEventType, TokenUsage

            self.call_count += 1
            yield StreamEvent(type=StreamEventType.TOKEN, content="fake response")
            yield StreamEvent(
                type=StreamEventType.USAGE,
                usage=TokenUsage(
                    prompt_tokens=10,
                    completion_tokens=5,
                    total_tokens=15,
                ),
            )
            yield StreamEvent(type=StreamEventType.DONE)

    async def get_model_info(self, model=""):
        from core.llm.types import ModelInfo

        return ModelInfo(id=model or "test-model", provider="fake", context_length=128000)

    async def list_models(self):
        return []

    async def check_health(self):
        return {}

    def resolve_provider(self, model=""):
        return "fake"

    def has_capacity(self, model=""):
        return True

    async def populate_registry(self):
        pass

    async def refresh_registry(self):
        pass

    async def close(self):
        pass


@pytest.fixture
def fake_llm():
    """Returns a FakeLLMClient instance (configure before use)."""
    return FakeLLMClient()


@pytest.fixture
def mock_llm_client(monkeypatch, fake_llm):
    """Patches get_llm_client to return the fake_llm fixture."""
    monkeypatch.setattr("core.llm.client._client", fake_llm)
    monkeypatch.setattr("core.llm.client.get_llm_client", lambda: fake_llm)
    return fake_llm


# ---------------------------------------------------------------------------
# Session + message factory helpers
# ---------------------------------------------------------------------------


def make_session(title="Test Session", **kwargs):
    """Create a DB session and return its ID."""
    from db import models as db

    return db.create_session(title=title, **kwargs)


def make_message(session_id, role="user", content="hello", **kwargs):
    """Add a message to a session and return its msg_id."""
    from db import models as db

    return db.add_message(session_id, role, content, **kwargs)


@pytest.fixture
def session_factory():
    """Returns the make_session helper."""
    return make_session


@pytest.fixture
def message_factory():
    """Returns the make_message helper."""
    return make_message


# ---------------------------------------------------------------------------
# Tool registry with builtins
# ---------------------------------------------------------------------------


@pytest.fixture
def tool_registry():
    """Fresh ToolRegistry preloaded with builtin tools."""
    from core.tools.builtin import load_builtin_tools
    from core.tools.registry import ToolRegistry

    reg = ToolRegistry()
    load_builtin_tools(reg)
    return reg


# ---------------------------------------------------------------------------
# Mock scout
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_scout(monkeypatch):
    """Patches run_scout to return a canned ScoutReport."""
    from core.scout.report import ScoutReport

    default_report = ScoutReport(
        identity="Be helpful",
        rules="Follow instructions",
        recommended_tools=["bash", "file_read", "file_write", "glob", "grep"],
        approach_guidance="Test approach",
    )

    async def _fake_run_scout(*args, **kwargs):
        return default_report

    monkeypatch.setattr("core.scout.runner.run_scout", _fake_run_scout)
    return default_report
