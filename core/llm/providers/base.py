"""Pernix — Provider protocol that all LLM adapters implement."""

from __future__ import annotations

from typing import AsyncGenerator, Protocol, runtime_checkable

from core.llm.types import (
    ChatResponse,
    HealthStatus,
    ModelInfo,
    StreamEvent,
)


@runtime_checkable
class ProviderProtocol(Protocol):
    """Interface for LLM provider adapters.

    Each provider (Ollama, OpenRouter, future vLLM/Bedrock) implements this.
    The ProviderRouter selects the appropriate adapter based on model name.
    """

    name: str

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str = "",
        max_tokens: int = 4096,
        temperature: float | None = None,
    ) -> ChatResponse: ...

    async def chat_stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str = "",
        max_tokens: int = 4096,
        temperature: float | None = None,
    ) -> AsyncGenerator[StreamEvent, None]: ...

    async def get_model_info(self, model: str) -> ModelInfo: ...

    async def list_models(self) -> list[ModelInfo]: ...

    async def check_health(self) -> HealthStatus: ...

    async def close(self) -> None: ...
