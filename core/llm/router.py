"""Pernix — Provider router with fallback logic."""

from __future__ import annotations

import logging

import httpx

from config import settings
from core.llm.errors import FALLBACK_REASONS, FailoverError, FailoverReason, classify_http_error
from core.llm.providers._shared import describe_exception
from core.llm.providers.ollama import OllamaProvider
from core.llm.providers.openrouter import OpenRouterProvider
from core.llm.registry import ModelRegistry
from core.llm.semaphore import SessionAwareLLMScheduler
from core.llm.types import ChatResponse, HealthStatus, ModelInfo, StreamEvent, StreamEventType, extract_tool_call_fields

logger = logging.getLogger("pernix.llm.router")

# Providers that speak strict OpenAI wire format and need
# normalize_for_openrouter() applied to compiled messages. Ollama is more
# permissive and gets the raw compile output.
OPENAI_FORMAT_PROVIDERS = frozenset({"openrouter", "openai"})


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
        from core.llm.providers.openai import OpenAIProvider

        self._ollama = OllamaProvider()
        self._openrouter = OpenRouterProvider()
        self._openai = OpenAIProvider()
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
        self._openai_semaphore = SessionAwareLLMScheduler(
            max_concurrent=settings.openai_max_concurrent,
            session_timeout=_timeout,
        )
        # Name-keyed maps are the canonical structure; the attributes above
        # remain as aliases for tests/diagnostics that reach in directly.
        self._providers = {
            "ollama": self._ollama,
            "openrouter": self._openrouter,
            "openai": self._openai,
        }
        self._semaphores = {
            "ollama": self._ollama_semaphore,
            "openrouter": self._openrouter_semaphore,
            "openai": self._openai_semaphore,
        }
        self._warned_downgrades: set[str] = set()

    def _fallback_eligible(self, provider) -> bool:
        """Transient remote-provider failures fall back to local Ollama."""
        return getattr(provider, "name", "ollama") != "ollama"

    def _can_fall_back(self, provider) -> bool:
        """Router-level failover needs a remote primary AND a configured
        fallback model. Without the model there is nowhere to go, and the
        right answer is the provider's own classified error — the one the
        ladder's backoff keys on — not a synthetic "no fallback" message that
        matches no retryable marker and turns a 503 into a hard stop.
        """
        return bool(settings.fallback_model) and self._fallback_eligible(provider)

    @staticmethod
    def _http_failover(provider, e: httpx.HTTPStatusError) -> tuple[FailoverReason, FailoverError]:
        """Classify an httpx status error and build the typed error whose text
        names the status ("openrouter 503: ...") so the ladder can retry it."""
        body = e.response.text if hasattr(e.response, "text") else ""
        status = e.response.status_code
        reason = classify_http_error(status, body)
        return reason, FailoverError(reason, f"{provider.name} {status}: {body[:500]}", original=e)

    @staticmethod
    def _connect_failover(provider, e: httpx.ConnectError) -> FailoverError:
        # describe_exception keeps the class name in the text: str(ConnectError)
        # can be empty, and "ConnectError" is the ladder's retry marker.
        return FailoverError(FailoverReason.UNKNOWN, f"{provider.name} {describe_exception(e)}", original=e)

    def get_provider(self, model: str = ""):
        """Select provider using the model registry."""
        model = model or settings.llm_model
        provider_name = self.registry.resolve_provider(model)
        provider = self._providers.get(provider_name)
        if provider is not None and provider_name != "ollama":
            if provider.available:
                return provider
            # This downgrade used to be silent, and it hid a whole outage:
            # every call for the model detoured to Ollama, 404'd there, and
            # failed over to the paid remote — with nothing in the log tying
            # cause to effect (2026-08-19, lost OPENAI_API_KEY). Warn once
            # per provider+model; availability is static within a process.
            key = f"{provider_name}:{model}"
            if key not in self._warned_downgrades:
                self._warned_downgrades.add(key)
                logger.warning(
                    "Provider '%s' resolved for model '%s' but reports unavailable "
                    "(missing API key?) — downgrading to Ollama, which likely cannot serve it",
                    provider_name,
                    model,
                )
        return self._ollama

    def get_semaphore(self, provider=None) -> SessionAwareLLMScheduler:
        """Return the semaphore for a provider instance."""
        return self._semaphores.get(getattr(provider, "name", "ollama"), self._ollama_semaphore)

    @property
    def semaphore_stats(self) -> dict:
        """Combined semaphore stats for diagnostics."""
        stats: dict = {
            "available": sum(s.available for s in self._semaphores.values()),
            "waiting": sum(s.waiting for s in self._semaphores.values()),
            "capacity": sum(s.capacity for s in self._semaphores.values()),
        }
        for name, sem in self._semaphores.items():
            stats[name] = sem.stats
        return stats

    def resolve_provider(self, model: str = "") -> str:
        """Return provider name ('ollama', 'openrouter', 'openai') for a model."""
        model = model or settings.llm_model
        return self.registry.resolve_provider(model)

    def _remote_providers(self) -> list:
        return [p for name, p in self._providers.items() if name != "ollama"]

    async def populate_registry(self) -> None:
        """Populate the model registry from provider APIs."""
        await self.registry.populate(self._ollama, *self._remote_providers())

    async def refresh_registry(self) -> None:
        """Re-populate the model registry (e.g. after model switch)."""
        await self.registry.refresh(self._ollama, *self._remote_providers())

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
            if fe.reason in FALLBACK_REASONS and self._can_fall_back(provider):
                sem.release()
                released = True
                return await self._fallback_chat(messages, sid, s_at, s_pri, **kwargs)
            raise
        except httpx.HTTPStatusError as e:
            reason, typed = self._http_failover(provider, e)
            if reason in FALLBACK_REASONS and self._can_fall_back(provider):
                sem.release()
                released = True
                return await self._fallback_chat(messages, sid, s_at, s_pri, **kwargs)
            raise typed from e
        except httpx.ConnectError as e:
            if self._can_fall_back(provider):
                sem.release()
                released = True
                return await self._fallback_chat(messages, sid, s_at, s_pri, **kwargs)
            raise self._connect_failover(provider, e) from e
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
        # Failing over is only safe before the first token reaches the caller.
        # agent.py accumulates every TOKEN into collected_content without a
        # reset signal, so restarting on a fallback provider mid-stream would
        # persist "<partial primary><complete fallback>" and bill both
        # (architecture review, Appendix C §2). The remote adapters already
        # refuse to raise past that point; this is the router-side guard for
        # providers that do.
        emitted_output = False
        try:
            async for event in provider.chat_stream(messages, **kwargs):
                if event.type in (StreamEventType.TOKEN, StreamEventType.TOOL_CALL):
                    emitted_output = True
                yield event
        except FailoverError as fe:
            if fe.reason in FALLBACK_REASONS and self._can_fall_back(provider):
                if emitted_output:
                    logger.error("%s %s after partial stream — not failing over", provider.name, fe.reason.value)
                    yield StreamEvent(type=StreamEventType.ERROR, error=fe.message)
                else:
                    logger.warning("%s %s, falling back to Ollama", provider.name, fe.reason.value)
                    sem.release()
                    released = True
                    async for event in self._fallback_stream(messages, sid, s_at, s_pri, **kwargs):
                        yield event
            else:
                raise  # let caller (agent loop) handle typed error
        except httpx.HTTPStatusError as e:
            reason, typed = self._http_failover(provider, e)
            if reason in FALLBACK_REASONS and self._can_fall_back(provider) and not emitted_output:
                logger.warning("%s %s, falling back to Ollama", provider.name, reason.value)
                sem.release()
                released = True
                async for event in self._fallback_stream(messages, sid, s_at, s_pri, **kwargs):
                    yield event
            elif emitted_output:
                logger.error("%s %s after partial stream — not failing over", provider.name, reason.value)
                yield StreamEvent(type=StreamEventType.ERROR, error=typed.message)
            else:
                raise typed from e
        except httpx.ConnectError as e:
            typed = self._connect_failover(provider, e)
            if self._can_fall_back(provider) and not emitted_output:
                sem.release()
                released = True
                async for event in self._fallback_stream(messages, sid, s_at, s_pri, **kwargs):
                    yield event
            elif emitted_output:
                yield StreamEvent(type=StreamEventType.ERROR, error=typed.message)
            else:
                raise typed from e
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

        whitelist = set(settings.openrouter_models or []) | set(settings.openai_models or [])
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
        for name, provider in self._providers.items():
            if name != "ollama" and provider.available:
                results[name] = await provider.check_health()
        return results

    async def close(self) -> None:
        for provider in self._providers.values():
            await provider.close()
