"""Pernix — FauxProvider: a scriptable provider for router-level tests.

Adaptation plan 1e. Complements (does not replace) conftest's FakeLLMClient:

- FakeLLMClient stubs the CLIENT — agent-loop tests that never need the
  router. Cheap, no scheduling, no failover.
- FauxProvider stands in for a PROVIDER inside a real ProviderRouter — it
  exercises the paths a client-level fake cannot reach: semaphore
  acquisition, typed failover classification, Ollama fallback, message
  sanitization.

Usage: build a real ProviderRouter, then swap providers via the name-keyed
map (the canonical structure since 1a):

    router = ProviderRouter()
    faux = FauxProvider("openrouter", steps=[raise_status(429), respond("ok")])
    router._providers["openrouter"] = router._openrouter = faux
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncGenerator

import httpx

from core.llm.types import ChatResponse, HealthStatus, ModelInfo, StreamEvent, StreamEventType, TokenUsage

# ---------------------------------------------------------------------------
# Step builders
# ---------------------------------------------------------------------------


def respond(content: str, model: str = "faux-model") -> dict:
    """Step: return a successful ChatResponse with this content."""
    return {"kind": "respond", "content": content, "model": model}


def raise_status(status: int, body: str = "") -> dict:
    """Step: raise httpx.HTTPStatusError with this status code."""
    return {"kind": "raise_status", "status": status, "body": body}


def raise_connect() -> dict:
    """Step: raise httpx.ConnectError (provider unreachable)."""
    return {"kind": "raise_connect"}


def stream_tokens(*tokens: str) -> dict:
    """Step: stream these TOKEN events then DONE."""
    return {"kind": "stream", "tokens": list(tokens)}


def stream_then_raise(tokens: list[str], status: int, body: str = "") -> dict:
    """Step: stream these TOKEN events, then fail mid-stream.

    The router must NOT fail over here — the caller has already accumulated
    the partial response and a fallback would be appended to it.
    """
    return {"kind": "stream_then_raise", "tokens": list(tokens), "status": status, "body": body}


def _http_error(status: int, body: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://faux/chat/completions")
    response = httpx.Response(status, text=body or f"faux {status}", request=request)
    return httpx.HTTPStatusError(f"faux {status}", request=request, response=response)


@dataclass
class FauxProvider:
    """Implements the full provider surface the router requires (the
    ProviderProtocol six plus `available` and `clear_models_cache`)."""

    name: str
    steps: list[dict] = field(default_factory=list)
    models: list[str] = field(default_factory=lambda: ["faux-model"])
    available: bool = True

    # Everything the provider was asked to do, for assertions.
    chat_calls: list[dict] = field(default_factory=list)
    stream_calls: list[dict] = field(default_factory=list)

    def _next_step(self) -> dict:
        if not self.steps:
            return respond("(faux default)")
        return self.steps.pop(0)

    async def chat(self, messages, **kwargs) -> ChatResponse:
        self.chat_calls.append({"messages": messages, **kwargs})
        step = self._next_step()
        if step["kind"] == "raise_status":
            raise _http_error(step["status"], step.get("body", ""))
        if step["kind"] == "raise_connect":
            raise httpx.ConnectError("faux connect error")
        return ChatResponse(
            content=step.get("content", ""),
            tool_calls=None,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            model=kwargs.get("model", step.get("model", "faux-model")),
            provider=self.name,
            finish_reason="stop",
        )

    async def chat_stream(self, messages, **kwargs) -> AsyncGenerator[StreamEvent, None]:
        self.stream_calls.append({"messages": messages, **kwargs})
        step = self._next_step()
        if step["kind"] == "raise_status":
            raise _http_error(step["status"], step.get("body", ""))
        if step["kind"] == "raise_connect":
            raise httpx.ConnectError("faux connect error")
        for token in step.get("tokens", [step.get("content", "")]):
            yield StreamEvent(type=StreamEventType.TOKEN, content=token)
        if step["kind"] == "stream_then_raise":
            raise _http_error(step["status"], step.get("body", ""))
        yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")

    async def get_model_info(self, model: str) -> ModelInfo:
        return ModelInfo(id=model, provider=self.name, context_length=128_000)

    async def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(id=m, provider=self.name, context_length=128_000) for m in self.models]

    async def check_health(self) -> HealthStatus:
        return HealthStatus(healthy=self.available)

    def clear_models_cache(self) -> None:
        pass

    async def close(self) -> None:
        pass


class StubRegistry:
    """Minimal registry stub: fixed model->provider mapping."""

    def __init__(self, mapping: dict[str, str]):
        self._mapping = mapping
        self.populated = True

    def resolve_provider(self, model: str) -> str:
        return self._mapping.get(model, "ollama")

    def resolve_model_id(self, model: str) -> str:
        return model

    def get_model_info(self, model: str):
        return None

    def all_models(self):
        return []
