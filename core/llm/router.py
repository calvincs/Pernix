"""Pernix — Provider router with fallback logic."""

from __future__ import annotations

import logging
import os

import httpx

from config import settings
from core.llm.errors import FALLBACK_REASONS, FailoverError, classify_http_error
from core.llm.providers.ollama import OllamaProvider
from core.llm.providers.openrouter import OpenRouterProvider
from core.llm.registry import ModelRegistry
from core.llm.semaphore import FairLLMSemaphore, SessionAwareLLMScheduler
from core.llm.types import ChatResponse, HealthStatus, ModelInfo, StreamEvent, StreamEventType, extract_tool_call_fields

logger = logging.getLogger("pernix.llm.router")


def is_openrouter_model(model: str) -> bool:
    """OpenRouter models use org/model format and require an API key."""
    return "/" in model and bool(os.environ.get("OPENROUTER_API_KEY"))


def sanitize_for_fallback(messages: list[dict]) -> list[dict]:
    """Sanitize messages for Ollama fallback (strip vision, convert tool messages).

    - Tool-role messages → user messages with context
    - Assistant tool_calls → text description appended
    - Multimodal content → flatten to text
    - Mid-conversation system messages → removed
    """
    cleaned = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content") or ""

        # Flatten multimodal content
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        parts.append(part.get("text", ""))
                    elif part.get("type") == "image_url":
                        kind = part.get("_kind", "image")
                        parts.append(f"[{kind} omitted]")
                else:
                    parts.append(str(part))
            content = "\n".join(parts)

        if role == "tool":
            # Convert tool result to user context
            tool_id = msg.get("tool_call_id", "unknown")
            cleaned.append(
                {
                    "role": "user",
                    "content": f"[Tool result from {tool_id}]: {content}",
                }
            )
        elif role == "assistant" and msg.get("tool_calls"):
            # Keep content, append tool call descriptions
            tc_desc = []
            for tc in msg["tool_calls"]:
                if not isinstance(tc, dict):
                    continue
                _, name, args = extract_tool_call_fields(tc)
                name = name or "unknown"
                tc_desc.append(f"{name}({args[:100]})")
            text = content or ""
            if tc_desc:
                text += f"\n[Called tools: {', '.join(tc_desc)}]"
            cleaned.append({"role": "assistant", "content": text})
        elif role == "system" and cleaned:
            # Drop mid-conversation system messages
            continue
        elif role in ("system", "user", "assistant"):
            cleaned.append({"role": role, "content": content})
        # else: drop any non-standard internal roles (eval, model_divider, etc.)

    return cleaned


class ProviderRouter:
    """Routes LLM requests to the appropriate provider with fallback."""

    def __init__(self):
        self._ollama = OllamaProvider()
        self._openrouter = OpenRouterProvider()
        self.registry = ModelRegistry()
        _timeout = float(settings.llm_session_timeout) if settings.llm_session_timeout > 0 else float("inf")
        self._ollama_semaphore = SessionAwareLLMScheduler(
            max_concurrent=settings.llm_max_concurrent,
            session_timeout=_timeout,
        )
        self._openrouter_semaphore = SessionAwareLLMScheduler(
            max_concurrent=settings.openrouter_max_concurrent,
            session_timeout=_timeout,
        )

    def get_provider(self, model: str = ""):
        """Select provider using the model registry."""
        model = model or settings.llm_model
        provider_name = self.registry.resolve_provider(model)
        if provider_name == "openrouter" and self._openrouter.available:
            return self._openrouter
        return self._ollama

    def get_semaphore(self, provider=None) -> FairLLMSemaphore:
        """Return the semaphore for a provider instance."""
        if provider is self._openrouter:
            return self._openrouter_semaphore
        return self._ollama_semaphore

    @property
    def semaphore_stats(self) -> dict:
        """Combined semaphore stats for diagnostics."""
        oll = self._ollama_semaphore
        orr = self._openrouter_semaphore
        return {
            "available": oll.available + orr.available,
            "waiting": oll.waiting + orr.waiting,
            "capacity": oll.capacity + orr.capacity,
            "ollama": oll.stats,
            "openrouter": orr.stats,
        }

    def resolve_provider(self, model: str = "") -> str:
        """Return provider name ('ollama' or 'openrouter') for a model."""
        model = model or settings.llm_model
        return self.registry.resolve_provider(model)

    async def populate_registry(self) -> None:
        """Populate the model registry from provider APIs."""
        await self.registry.populate(self._ollama, self._openrouter)

    async def refresh_registry(self) -> None:
        """Re-populate the model registry (e.g. after model switch)."""
        await self.registry.refresh(self._ollama, self._openrouter)

    def _pop_session_kwargs(self, kwargs: dict) -> tuple[str, float, int]:
        """Extract and remove scheduling kwargs; returns (session_id, created_at, priority)."""
        from core.llm.semaphore import PRIORITY_BACKGROUND

        session_id = kwargs.pop("_session_id", "")
        session_created_at = kwargs.pop("_session_created_at", float("inf"))
        session_priority = kwargs.pop("_session_priority", PRIORITY_BACKGROUND)
        return session_id, session_created_at, session_priority

    async def chat(self, messages: list[dict], **kwargs) -> ChatResponse:
        """Route chat with per-provider semaphore and fallback on transient errors."""
        sid, s_at, s_pri = self._pop_session_kwargs(kwargs)
        model = kwargs.get("model", "") or settings.llm_model
        provider = self.get_provider(model)
        sem = self.get_semaphore(provider)

        await sem.acquire(session_id=sid, session_created_at=s_at, priority=s_pri)
        released = False
        try:
            return await provider.chat(messages, **kwargs)
        except FailoverError as fe:
            if fe.reason in FALLBACK_REASONS and provider is self._openrouter:
                sem.release()
                released = True
                return await self._fallback_chat(messages, sid, s_at, s_pri, **kwargs)
            raise
        except httpx.HTTPStatusError as e:
            body = e.response.text if hasattr(e.response, "text") else ""
            reason = classify_http_error(e.response.status_code, body)
            if reason in FALLBACK_REASONS and provider is self._openrouter:
                sem.release()
                released = True
                return await self._fallback_chat(messages, sid, s_at, s_pri, **kwargs)
            raise FailoverError(reason, str(e), original=e) from e
        except httpx.ConnectError:
            if provider is self._openrouter:
                sem.release()
                released = True
                return await self._fallback_chat(messages, sid, s_at, s_pri, **kwargs)
            raise
        finally:
            if not released:
                sem.release()

    async def chat_stream(self, messages: list[dict], **kwargs):
        """Route streaming chat with per-provider semaphore and fallback."""
        sid, s_at, s_pri = self._pop_session_kwargs(kwargs)
        model = kwargs.get("model", "") or settings.llm_model
        provider = self.get_provider(model)
        sem = self.get_semaphore(provider)

        await sem.acquire(session_id=sid, session_created_at=s_at, priority=s_pri)
        released = False
        try:
            async for event in provider.chat_stream(messages, **kwargs):
                yield event
        except FailoverError as fe:
            if fe.reason in FALLBACK_REASONS and provider is self._openrouter:
                logger.warning("OpenRouter %s, falling back to Ollama", fe.reason.value)
                sem.release()
                released = True
                async for event in self._fallback_stream(messages, sid, s_at, s_pri, **kwargs):
                    yield event
            else:
                raise  # let caller (agent loop) handle typed error
        except httpx.HTTPStatusError as e:
            body = e.response.text if hasattr(e.response, "text") else ""
            reason = classify_http_error(e.response.status_code, body)
            if reason in FALLBACK_REASONS and provider is self._openrouter:
                logger.warning("OpenRouter %s, falling back to Ollama", reason.value)
                sem.release()
                released = True
                async for event in self._fallback_stream(messages, sid, s_at, s_pri, **kwargs):
                    yield event
            else:
                raise FailoverError(reason, str(e), original=e) from e
        except httpx.ConnectError as e:
            if provider is self._openrouter:
                sem.release()
                released = True
                async for event in self._fallback_stream(messages, sid, s_at, s_pri, **kwargs):
                    yield event
            else:
                yield StreamEvent(type=StreamEventType.ERROR, error=str(e))
        finally:
            if not released:
                sem.release()

    async def _fallback_chat(
        self,
        messages: list[dict],
        session_id: str,
        session_created_at: float,
        session_priority: int,
        **kwargs,
    ) -> ChatResponse:
        """Fallback to Ollama with its own semaphore, preserving session scheduling."""
        fallback_model = settings.fallback_model
        if not fallback_model:
            raise RuntimeError("No fallback model configured")
        logger.warning("Falling back to Ollama model: %s", fallback_model)
        clean = sanitize_for_fallback(messages)
        kwargs["model"] = fallback_model
        kwargs.pop("tools", None)
        await self._ollama_semaphore.acquire(
            session_id=session_id,
            session_created_at=session_created_at,
            priority=session_priority,
        )
        try:
            return await self._ollama.chat(clean, **kwargs)
        finally:
            self._ollama_semaphore.release()

    async def _fallback_stream(
        self,
        messages: list[dict],
        session_id: str,
        session_created_at: float,
        session_priority: int,
        **kwargs,
    ):
        """Fallback stream to Ollama with its own semaphore, preserving session scheduling."""
        fallback_model = settings.fallback_model
        if not fallback_model:
            yield StreamEvent(type=StreamEventType.ERROR, error="No fallback model configured")
            return
        logger.warning("Falling back to Ollama model: %s", fallback_model)
        clean = sanitize_for_fallback(messages)
        kwargs["model"] = fallback_model
        kwargs.pop("tools", None)
        await self._ollama_semaphore.acquire(
            session_id=session_id,
            session_created_at=session_created_at,
            priority=session_priority,
        )
        try:
            async for event in self._ollama.chat_stream(clean, **kwargs):
                yield event
        finally:
            self._ollama_semaphore.release()

    async def get_model_info(self, model: str = "") -> ModelInfo:
        return await self.get_provider(model).get_model_info(model or settings.llm_model)

    async def list_all_models(self) -> list[ModelInfo]:
        """List models from all available providers.

        Ollama is always queried live (local, fast) so newly-pulled models
        appear in the dropdown without restarting. OpenRouter uses the cached
        registry to avoid a remote API call on every settings open.
        """
        try:
            ollama_live = await self._ollama.list_models()
        except Exception:
            ollama_live = []

        if self.registry.populated:
            cached_or = {m.id: m for m in self.registry.all_models() if m.provider != "ollama"}
        elif self._openrouter.available:
            cached_or = {m.id: m for m in await self._openrouter.list_models()}
        else:
            cached_or = {}

        whitelist = set(settings.openrouter_models or [])
        result: dict[str, ModelInfo] = dict(cached_or)
        for m in ollama_live:
            if m.id in result and m.id in whitelist:
                continue  # user explicitly wants this model from OpenRouter
            result[m.id] = m  # Ollama wins on collision
        return list(result.values())

    async def check_all_health(self) -> dict[str, HealthStatus]:
        """Check health of all providers."""
        results = {}
        results["ollama"] = await self._ollama.check_health()
        if self._openrouter.available:
            results["openrouter"] = await self._openrouter.check_health()
        return results

    async def close(self) -> None:
        await self._ollama.close()
        await self._openrouter.close()
