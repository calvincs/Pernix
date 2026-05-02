"""Pernix — LLM type definitions shared across providers."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class StreamEventType(Enum):
    TOKEN = "token"
    TOOL_CALL = "tool_call"
    USAGE = "usage"
    DONE = "done"
    ERROR = "error"


@dataclass
class ToolCall:
    """Normalized tool call from any provider."""

    id: str
    name: str
    arguments: str  # always JSON string


@dataclass
class TokenUsage:
    """Token counts from provider response."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    extra: dict = field(default_factory=dict)


@dataclass
class ChatResponse:
    """Normalized non-streaming response."""

    content: str
    tool_calls: list[ToolCall] | None
    usage: TokenUsage
    model: str
    provider: str
    finish_reason: str  # stop | tool_calls | length


@dataclass
class StreamEvent:
    """Normalized streaming event."""

    type: StreamEventType
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    usage: TokenUsage | None = None
    error: str | None = None
    finish_reason: str | None = None  # populated on DONE: stop | tool_calls | length | None


@dataclass
class ModelInfo:
    """Model capabilities and metadata."""

    id: str
    provider: str
    context_length: int
    supports_vision: bool = False
    supports_tools: bool = True
    supports_streaming: bool = True
    max_output_tokens: int | None = None


@dataclass
class ProviderConfig:
    """Configuration for a single LLM provider."""

    name: str
    base_url: str
    api_key: str = ""
    max_concurrent: int = 2
    timeout: float = 600.0
    connect_timeout: float = 30.0
    max_connections: int = 10
    max_keepalive: int = 5
    retry_delays: list[float] = field(default_factory=lambda: [2.0, 5.0, 10.0])


@dataclass
class HealthStatus:
    """Provider health check result."""

    healthy: bool
    latency_ms: int = 0
    error: str | None = None
    models_available: int = 0


def extract_tool_call_fields(tc: dict) -> tuple[str, str, str]:
    """Extract (id, name, arguments) from either flat or nested tool call format.

    Flat:   {"id": "...", "name": "...", "arguments": "..."}
    Nested: {"id": "...", "type": "function", "function": {"name": "...", "arguments": "..."}}
    """
    tc_id = tc.get("id", "")
    func = tc.get("function", {}) if isinstance(tc.get("function"), dict) else {}
    name = func.get("name") or tc.get("name", "")
    arguments = func.get("arguments") or tc.get("arguments", "{}")
    return tc_id, name, arguments
