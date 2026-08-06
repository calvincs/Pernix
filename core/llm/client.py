"""Pernix — LLM client facade (public API for application code)."""

from __future__ import annotations

import logging
from typing import AsyncGenerator

from config import settings
from core.llm.router import ProviderRouter
from core.llm.semaphore import PRIORITY_BACKGROUND
from core.llm.types import ChatResponse, HealthStatus, ModelInfo, StreamEvent

logger = logging.getLogger("pernix.llm.client")

# Module-level singletons
_router: ProviderRouter | None = None


def _get_router() -> ProviderRouter:
    global _router
    if _router is None:
        _router = ProviderRouter()
    return _router


async def reset_router() -> None:
    """Close and discard the cached router so the next request rebuilds it.

    Called after settings changes to llm_base_url or openrouter_base_url so
    providers pick up the new URL without requiring a server restart.
    """
    global _router, _client
    if _router is not None:
        try:
            await _router.close()
        except Exception:
            pass
        _router = None
    _client = None


def _get_semaphore_stats() -> dict:
    """Get combined semaphore stats from the router (for diagnostics)."""
    return _get_router().semaphore_stats


def _all_semaphores(router) -> list:
    """Every scheduler on the router — provider-map first, legacy attrs for
    stubbed/partial routers (tests, failed resets)."""
    sems = getattr(router, "_semaphores", None)
    if sems:
        return list(sems.values())
    return [
        s
        for s in (getattr(router, "_ollama_semaphore", None), getattr(router, "_openrouter_semaphore", None))
        if s is not None
    ]


def session_seconds_remaining(session_id: str) -> float:
    """Minimum remaining session-time budget across providers.

    A session may have acquired slots from both Ollama and OpenRouter
    schedulers (e.g. a model failover mid-turn). The constraining budget is
    whichever scheduler started counting earliest — that's the one that will
    fire LLMSessionTimeoutError first. Returns float('inf') if neither has
    started counting yet.
    """
    router = _get_router()
    remaining = [s.session_seconds_remaining(session_id) for s in _all_semaphores(router)]
    return min(remaining) if remaining else float("inf")


def extend_session_budget(session_id: str, additional_seconds: float) -> float:
    """Grant a session more LLM budget across both providers.

    Applied to both Ollama and OpenRouter schedulers because a session that
    starts on one provider may failover to the other mid-turn. Returns the
    new effective Ollama timeout (the typical primary) for diagnostics —
    both schedulers receive the same extension.
    """
    router = _get_router()
    ollama_sem = getattr(router, "_ollama_semaphore", None)
    result = 0.0
    for sem in _all_semaphores(router):
        val = sem.extend_session_budget(session_id, additional_seconds)
        if sem is ollama_sem or result == 0.0:
            result = val
    return result


def ensure_session_budget(session_id: str, min_remaining_seconds: float) -> float:
    """Ensure at least `min_remaining_seconds` of LLM budget remain, per provider.

    Unlike extend_session_budget (headroom relative to the base timeout —
    a no-op when re-granted within one turn), this measures each scheduler's
    elapsed clock and tops the cap up so the caller's full window is left.
    Use it before starting a long-running LLM-billed job (rlm_process) that
    may run back-to-back with earlier jobs in the same turn. Each scheduler
    computes its own deficit because each tracks its own first-acquire clock.
    Returns the new effective Ollama timeout for diagnostics.
    """
    router = _get_router()
    ollama_sem = getattr(router, "_ollama_semaphore", None)
    result = 0.0
    for sem in _all_semaphores(router):
        val = sem.ensure_session_budget(session_id, min_remaining_seconds)
        if sem is ollama_sem or result == 0.0:
            result = val
    return result


def reset_session_budget(session_id: str) -> None:
    """Reset a session's wall-clock LLM budget tracking on every provider.

    Called by SessionManager.prompt() at the start of a new user turn so
    each turn gets a fresh llm_session_timeout window. Without this, the
    cap accumulates from the session's first ever acquire and locks the
    session out after the wall-clock total exceeds the cap — even if most
    of that time was the user thinking, not the model running.
    """
    router = _get_router()
    for sem in _all_semaphores(router):
        sem.reset_session_budget(session_id)


class LLMClient:
    """High-level LLM client used by agent, evaluator, scout, etc.

    Provider routing and per-provider concurrency are managed by ProviderRouter.
    """

    def __init__(self):
        self.router = _get_router()

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str = "",
        max_tokens: int | None = None,
        temperature: float | None = None,
        session_id: str = "",
        session_created_at: float = float("inf"),
        session_priority: int = PRIORITY_BACKGROUND,
    ) -> ChatResponse:
        """Non-streaming chat. Semaphore managed per-provider by router."""
        model = model or settings.llm_model
        max_tokens = max_tokens or settings.max_tokens

        response = await self.router.chat(
            messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            _session_id=session_id,
            _session_created_at=session_created_at,
            _session_priority=session_priority,
        )
        return response

    async def chat_stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str = "",
        max_tokens: int | None = None,
        temperature: float | None = None,
        session_id: str = "",
        session_created_at: float = float("inf"),
        session_priority: int = PRIORITY_BACKGROUND,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Streaming chat. Semaphore managed per-provider by router."""
        model = model or settings.llm_model
        max_tokens = max_tokens or settings.max_tokens

        stream = None
        try:
            stream = self.router.chat_stream(
                messages,
                tools=tools,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                _session_id=session_id,
                _session_created_at=session_created_at,
                _session_priority=session_priority,
            )
            async for event in stream:
                yield event
        finally:
            # Explicitly close the inner stream to release HTTP connection
            if stream is not None:
                try:
                    await stream.aclose()
                except Exception:
                    pass

    async def get_model_info(self, model: str = "") -> ModelInfo:
        return await self.router.get_model_info(model)

    async def list_models(self) -> list[ModelInfo]:
        return await self.router.list_all_models()

    async def check_health(self) -> dict[str, HealthStatus]:
        return await self.router.check_all_health()

    def resolve_provider(self, model: str = "") -> str:
        """Return provider name ('ollama' or 'openrouter') for a model."""
        return self.router.resolve_provider(model)

    def has_capacity(self, model: str = "") -> bool:
        """Check if the provider for this model has an available semaphore slot."""
        model = model or settings.llm_model
        provider = self.router.get_provider(model)
        sem = self.router.get_semaphore(provider)
        return sem.available > 0

    async def populate_registry(self) -> None:
        """Populate the model registry from provider APIs."""
        await self.router.populate_registry()

    async def refresh_registry(self) -> None:
        """Re-populate the model registry (e.g. after model switch)."""
        await self.router.refresh_registry()

    def purge_session(self, session_id: str) -> None:
        """Remove session from LLM timeout tracking once it is fully reaped.

        Best-effort by design: this is teardown, called from remove() and
        delete_session(). A router without the expected schedulers (a stubbed
        one, or a partially constructed router after a failed reset) must not
        turn "delete this session" into an AttributeError.
        """
        for sem in _all_semaphores(self.router):
            try:
                sem.purge_session(session_id)
            except Exception as e:
                logger.debug("purge_session failed for %s: %s", session_id, e)

    async def close(self) -> None:
        await self.router.close()


# Convenience: module-level client
_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    """Get or create the singleton LLMClient."""
    global _client
    if _client is None:
        _client = LLMClient()
    return _client
