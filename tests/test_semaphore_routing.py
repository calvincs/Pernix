"""Tests for per-provider semaphore routing.

Validates that:
1. Ollama and OpenRouter requests use separate semaphores
2. Concurrency limits are enforced independently per provider
3. Fallback from OpenRouter to Ollama acquires the Ollama semaphore
4. Semaphore stats correctly report per-provider state
5. Stream semaphore is held for the full stream and released on completion
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.llm.semaphore import FairLLMSemaphore, LLMConcurrencyError
from core.llm.types import ChatResponse, StreamEvent, StreamEventType, TokenUsage

# ---------------------------------------------------------------------------
# FairLLMSemaphore unit tests
# ---------------------------------------------------------------------------


class TestFairLLMSemaphore:
    """Unit tests for the semaphore itself."""

    @pytest.mark.asyncio
    async def test_basic_acquire_release(self):
        sem = FairLLMSemaphore(max_concurrent=2)
        assert sem.available == 2
        assert sem.capacity == 2

        await sem.acquire()
        assert sem.available == 1

        await sem.acquire()
        assert sem.available == 0

        sem.release()
        assert sem.available == 1

        sem.release()
        assert sem.available == 2

    @pytest.mark.asyncio
    async def test_timeout_raises(self):
        sem = FairLLMSemaphore(max_concurrent=1)
        await sem.acquire()

        with pytest.raises(LLMConcurrencyError):
            await sem.acquire(timeout=0.05)

        sem.release()

    @pytest.mark.asyncio
    async def test_stats(self):
        sem = FairLLMSemaphore(max_concurrent=3)
        stats = sem.stats
        assert stats == {"available": 3, "waiting": 0, "capacity": 3}


# ---------------------------------------------------------------------------
# ProviderRouter per-provider semaphore tests
# ---------------------------------------------------------------------------


def _make_chat_response(text="ok"):
    return ChatResponse(
        content=text,
        tool_calls=[],
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5),
        model="test",
        provider="test",
        finish_reason="stop",
    )


async def _make_stream_events():
    yield StreamEvent(type=StreamEventType.TOKEN, content="hello")
    yield StreamEvent(type=StreamEventType.DONE)


class TestRouterSemaphores:
    """Test that ProviderRouter uses per-provider semaphores."""

    def _make_router(self, ollama_max=1, openrouter_max=1):
        """Create a ProviderRouter with mocked providers."""
        from core.llm.router import ProviderRouter

        with patch.object(ProviderRouter, "__init__", lambda self: None):
            router = ProviderRouter()

        router._ollama = MagicMock()
        router._ollama.name = "ollama"  # get_semaphore/_fallback_eligible key off .name
        router._ollama.chat = AsyncMock(return_value=_make_chat_response("ollama"))
        router._ollama.chat_stream = MagicMock(return_value=_make_stream_events())
        router._ollama.available = True

        router._openrouter = MagicMock()
        router._openrouter.name = "openrouter"
        router._openrouter.chat = AsyncMock(return_value=_make_chat_response("openrouter"))
        router._openrouter.chat_stream = MagicMock(return_value=_make_stream_events())
        router._openrouter.available = True

        router.registry = MagicMock()
        router._ollama_semaphore = FairLLMSemaphore(max_concurrent=ollama_max)
        router._openrouter_semaphore = FairLLMSemaphore(max_concurrent=openrouter_max)
        # Name-keyed maps are the canonical router structure (1a); the
        # attribute aliases above are kept for direct assertions.
        router._providers = {"ollama": router._ollama, "openrouter": router._openrouter}
        router._semaphores = {"ollama": router._ollama_semaphore, "openrouter": router._openrouter_semaphore}

        return router

    @pytest.mark.asyncio
    async def test_ollama_uses_ollama_semaphore(self):
        router = self._make_router(ollama_max=1, openrouter_max=1)
        router.registry.resolve_provider.return_value = "ollama"

        # Before: both available
        assert router._ollama_semaphore.available == 1
        assert router._openrouter_semaphore.available == 1

        resp = await router.chat([{"role": "user", "content": "hi"}], model="llama3")

        # After: both released
        assert resp.content == "ollama"
        assert router._ollama_semaphore.available == 1
        assert router._openrouter_semaphore.available == 1  # untouched

    @pytest.mark.asyncio
    async def test_openrouter_uses_openrouter_semaphore(self):
        router = self._make_router(ollama_max=1, openrouter_max=1)
        router.registry.resolve_provider.return_value = "openrouter"

        resp = await router.chat([{"role": "user", "content": "hi"}], model="anthropic/claude-sonnet-4")

        assert resp.content == "openrouter"
        assert router._openrouter_semaphore.available == 1
        assert router._ollama_semaphore.available == 1  # untouched

    @pytest.mark.asyncio
    async def test_independent_concurrency_limits(self):
        """Ollama and OpenRouter can run concurrently up to their own limits."""
        router = self._make_router(ollama_max=2, openrouter_max=2)

        # Track which semaphore slots are consumed during concurrent calls
        ollama_min_available = [2]
        openrouter_min_available = [2]

        original_ollama_chat = router._ollama.chat
        original_openrouter_chat = router._openrouter.chat

        async def slow_ollama_chat(*args, **kwargs):
            ollama_min_available[0] = min(ollama_min_available[0], router._ollama_semaphore.available)
            await asyncio.sleep(0.05)
            return _make_chat_response("ollama")

        async def slow_openrouter_chat(*args, **kwargs):
            openrouter_min_available[0] = min(openrouter_min_available[0], router._openrouter_semaphore.available)
            await asyncio.sleep(0.05)
            return _make_chat_response("openrouter")

        router._ollama.chat = slow_ollama_chat
        router._openrouter.chat = slow_openrouter_chat

        def resolve(model):
            return "openrouter" if "/" in model else "ollama"

        router.registry.resolve_provider.side_effect = resolve

        # Fire 2 Ollama + 2 OpenRouter concurrently
        results = await asyncio.gather(
            router.chat([{"role": "user", "content": "1"}], model="llama3"),
            router.chat([{"role": "user", "content": "2"}], model="qwen"),
            router.chat([{"role": "user", "content": "3"}], model="anthropic/claude"),
            router.chat([{"role": "user", "content": "4"}], model="x-ai/grok"),
        )

        assert len(results) == 4
        # All semaphores released
        assert router._ollama_semaphore.available == 2
        assert router._openrouter_semaphore.available == 2

    @pytest.mark.asyncio
    async def test_ollama_limit_blocks_ollama_not_openrouter(self):
        """When Ollama semaphore is full, OpenRouter requests still proceed."""
        router = self._make_router(ollama_max=1, openrouter_max=1)

        def resolve(model):
            return "openrouter" if "/" in model else "ollama"

        router.registry.resolve_provider.side_effect = resolve

        blocked = asyncio.Event()
        release = asyncio.Event()

        async def blocking_ollama_chat(*args, **kwargs):
            blocked.set()
            await release.wait()
            return _make_chat_response("ollama")

        router._ollama.chat = blocking_ollama_chat

        # Start a blocking Ollama call
        ollama_task = asyncio.create_task(router.chat([{"role": "user", "content": "slow"}], model="llama3"))
        await blocked.wait()

        # Ollama semaphore is full
        assert router._ollama_semaphore.available == 0
        # But OpenRouter should still work
        assert router._openrouter_semaphore.available == 1

        or_result = await router.chat([{"role": "user", "content": "fast"}], model="anthropic/claude")
        assert or_result.content == "openrouter"

        # Clean up
        release.set()
        await ollama_task

    @pytest.mark.asyncio
    async def test_stream_holds_and_releases_semaphore(self):
        router = self._make_router(ollama_max=1)
        router.registry.resolve_provider.return_value = "ollama"

        events = []
        async for event in router.chat_stream([{"role": "user", "content": "hi"}], model="test"):
            events.append(event)
            # During stream, semaphore is held
            # (can't check mid-yield easily, but we verify release after)

        assert len(events) >= 1
        assert router._ollama_semaphore.available == 1  # released

    @pytest.mark.asyncio
    async def test_semaphore_stats_combined(self):
        router = self._make_router(ollama_max=3, openrouter_max=2)
        stats = router.semaphore_stats

        assert stats["capacity"] == 5
        assert stats["available"] == 5
        assert stats["waiting"] == 0
        assert stats["ollama"]["capacity"] == 3
        assert stats["openrouter"]["capacity"] == 2

    @pytest.mark.asyncio
    async def test_fallback_acquires_ollama_semaphore(self):
        """When OpenRouter fails and falls back to Ollama,
        the Ollama semaphore is acquired for the fallback call."""
        from core.llm.errors import FailoverError, FailoverReason

        router = self._make_router(ollama_max=1, openrouter_max=1)
        router.registry.resolve_provider.return_value = "openrouter"

        # OpenRouter raises a rate limit error
        router._openrouter.chat = AsyncMock(side_effect=FailoverError(FailoverReason.RATE_LIMIT, "429"))

        # Patch settings for fallback
        with patch("core.llm.router.settings") as mock_settings:
            mock_settings.llm_model = "test"
            mock_settings.fallback_model = "llama3"

            resp = await router.chat([{"role": "user", "content": "hi"}], model="anthropic/claude")

        # Fallback used Ollama
        assert resp.content == "ollama"
        # Both semaphores released
        assert router._openrouter_semaphore.available == 1
        assert router._ollama_semaphore.available == 1

    @pytest.mark.asyncio
    async def test_fallback_releases_primary_semaphore_before_ollama(self):
        """OpenRouter semaphore must be FREE when Ollama fallback executes.
        This prevents nested lock contention / resource starvation."""
        from core.llm.errors import FailoverError, FailoverReason

        router = self._make_router(ollama_max=1, openrouter_max=1)
        router.registry.resolve_provider.return_value = "openrouter"

        router._openrouter.chat = AsyncMock(side_effect=FailoverError(FailoverReason.RATE_LIMIT, "429"))

        # Track OpenRouter semaphore state at the moment Ollama is called
        or_available_during_fallback = []

        original_ollama_chat = router._ollama.chat

        async def spy_ollama_chat(*args, **kwargs):
            or_available_during_fallback.append(router._openrouter_semaphore.available)
            return _make_chat_response("ollama")

        router._ollama.chat = spy_ollama_chat

        with patch("core.llm.router.settings") as mock_settings:
            mock_settings.llm_model = "test"
            mock_settings.fallback_model = "llama3"

            resp = await router.chat([{"role": "user", "content": "hi"}], model="anthropic/claude")

        assert resp.content == "ollama"
        # Critical: OpenRouter semaphore was released BEFORE Ollama call
        assert or_available_during_fallback == [1], (
            f"OpenRouter semaphore should be available=1 during fallback, " f"got {or_available_during_fallback}"
        )

    @pytest.mark.asyncio
    async def test_fallback_stream_releases_primary_semaphore_before_ollama(self):
        """Streaming fallback: OpenRouter semaphore must be FREE during Ollama stream."""
        from core.llm.errors import FailoverError, FailoverReason

        router = self._make_router(ollama_max=1, openrouter_max=1)
        router.registry.resolve_provider.return_value = "openrouter"

        async def failing_stream(*args, **kwargs):
            raise FailoverError(FailoverReason.RATE_LIMIT, "429")
            yield  # make it an async generator  # noqa: F704

        router._openrouter.chat_stream = MagicMock(side_effect=failing_stream)

        or_available_during_fallback = []

        async def spy_ollama_stream(*args, **kwargs):
            or_available_during_fallback.append(router._openrouter_semaphore.available)
            yield StreamEvent(type=StreamEventType.TOKEN, content="fallback")
            yield StreamEvent(type=StreamEventType.DONE)

        router._ollama.chat_stream = MagicMock(side_effect=spy_ollama_stream)

        with patch("core.llm.router.settings") as mock_settings:
            mock_settings.llm_model = "test"
            mock_settings.fallback_model = "llama3"

            events = []
            async for event in router.chat_stream([{"role": "user", "content": "hi"}], model="anthropic/claude"):
                events.append(event)

        assert len(events) >= 1
        # Critical: OpenRouter semaphore was released BEFORE Ollama stream
        assert or_available_during_fallback == [1], (
            f"OpenRouter semaphore should be available=1 during fallback stream, " f"got {or_available_during_fallback}"
        )
        # Both semaphores fully released after
        assert router._openrouter_semaphore.available == 1
        assert router._ollama_semaphore.available == 1

    @pytest.mark.asyncio
    async def test_semaphore_released_on_provider_error(self):
        """Semaphore is released even if the provider raises an unexpected error."""
        router = self._make_router(ollama_max=1)
        router.registry.resolve_provider.return_value = "ollama"
        router._ollama.chat = AsyncMock(side_effect=RuntimeError("boom"))

        with pytest.raises(RuntimeError, match="boom"):
            await router.chat([{"role": "user", "content": "hi"}], model="test")

        assert router._ollama_semaphore.available == 1  # released despite error

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reason_name", ["TIMEOUT", "UNKNOWN"])
    async def test_fallback_on_transient_failover_reasons(self, reason_name):
        """OpenRouter timeouts and unknown errors fall back to Ollama."""
        from core.llm.errors import FailoverError, FailoverReason

        reason = FailoverReason[reason_name]
        router = self._make_router(ollama_max=1, openrouter_max=1)
        router.registry.resolve_provider.return_value = "openrouter"
        router._openrouter.chat = AsyncMock(side_effect=FailoverError(reason, "fail"))

        with patch("core.llm.router.settings") as mock_settings:
            mock_settings.llm_model = "test"
            mock_settings.fallback_model = "llama3"

            resp = await router.chat([{"role": "user", "content": "hi"}], model="anthropic/claude")

        assert resp.content == "ollama"  # served by fallback
        assert router._openrouter_semaphore.available == 1
        assert router._ollama_semaphore.available == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reason_name", ["AUTH", "MODEL_NOT_FOUND", "CONTEXT_OVERFLOW", "FORMAT_ERROR"])
    async def test_no_fallback_on_config_or_logic_errors(self, reason_name):
        """Config/logic errors must surface, not be masked by a silent fallback."""
        from core.llm.errors import FailoverError, FailoverReason

        reason = FailoverReason[reason_name]
        router = self._make_router(ollama_max=1, openrouter_max=1)
        router.registry.resolve_provider.return_value = "openrouter"
        router._openrouter.chat = AsyncMock(side_effect=FailoverError(reason, "config"))

        with patch("core.llm.router.settings") as mock_settings:
            mock_settings.llm_model = "test"
            mock_settings.fallback_model = "llama3"

            with pytest.raises(FailoverError) as exc_info:
                await router.chat([{"role": "user", "content": "hi"}], model="anthropic/claude")

        assert exc_info.value.reason == reason
        assert router._openrouter_semaphore.available == 1
        assert router._ollama_semaphore.available == 1  # never acquired


# ---------------------------------------------------------------------------
# Integration: _get_semaphore_stats
# ---------------------------------------------------------------------------


class TestSemaphoreStatsAPI:
    """Test that _get_semaphore_stats returns the router's combined stats."""

    @pytest.mark.asyncio
    async def test_stats_from_client_module(self):
        from core.llm.client import _get_semaphore_stats

        stats = _get_semaphore_stats()
        assert "available" in stats
        assert "capacity" in stats
        assert "ollama" in stats
        assert "openrouter" in stats


# ---------------------------------------------------------------------------
# Scheduler budget cleanup on session teardown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_session_purges_scheduler_budget():
    """remove() purged the scheduler; delete_session() did not, so the
    per-provider budget maps grew for the life of the process — and a
    recycled session id would inherit a stale wall-clock clock."""
    from core.llm.client import get_llm_client
    from sessions.manager import SessionManager

    mgr = SessionManager()
    sid = mgr.create_session(title="Budget Purge")

    router = get_llm_client().router
    for sem in (router._ollama_semaphore, router._openrouter_semaphore):
        await sem.acquire(session_id=sid)
        assert sid in sem._session_first_active
        sem.extend_session_budget(sid, 60)
        assert sid in sem._session_timeout_override

    mgr.delete_session(sid)

    for sem in (router._ollama_semaphore, router._openrouter_semaphore):
        assert sid not in sem._session_first_active
        assert sid not in sem._session_timeout_override
        sem.release()


def test_purge_session_tolerates_a_stubbed_router():
    """Teardown must never raise. A stubbed or partially built router (test
    doubles, or a router mid-reset_router) previously produced an
    AttributeError out of delete_session/remove."""
    from core.llm.client import LLMClient

    client = LLMClient.__new__(LLMClient)
    client.router = object()  # no _ollama_semaphore / _openrouter_semaphore
    client.purge_session("whatever")  # must not raise


def test_purge_session_survives_a_failing_scheduler():
    from core.llm.client import LLMClient

    class _Boom:
        def purge_session(self, sid):
            raise RuntimeError("scheduler exploded")

    class _Router:
        _ollama_semaphore = _Boom()
        _openrouter_semaphore = _Boom()

    client = LLMClient.__new__(LLMClient)
    client.router = _Router()
    client.purge_session("whatever")  # must not raise


# ---------------------------------------------------------------------------
# Scheduler heap hygiene
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timed_out_waiters_are_reaped_from_the_heap():
    """A cancelled/timed-out acquire leaves its _WaitItem behind.
    _wake_next_or_free skips dead futures, so this was never a correctness
    bug — but under repeated timeouts against a saturated provider the heap
    grew without bound and every wake paid to pop through the corpses."""
    from core.llm.semaphore import LLMConcurrencyError, SessionAwareLLMScheduler

    sem = SessionAwareLLMScheduler(max_concurrent=1)
    await sem.acquire(session_id="holder")  # saturate
    assert sem.available == 0

    for i in range(20):
        with pytest.raises(LLMConcurrencyError):
            await sem.acquire(session_id=f"waiter{i}", timeout=0.01)

    assert sem.waiting == 0
    assert len(sem._heap) == 0, f"dead waiters accumulated: {len(sem._heap)}"

    sem.release()
    assert sem.available == 1


@pytest.mark.asyncio
async def test_cancelled_waiters_are_reaped_from_the_heap():
    from core.llm.semaphore import SessionAwareLLMScheduler

    sem = SessionAwareLLMScheduler(max_concurrent=1)
    await sem.acquire(session_id="holder")

    tasks = [asyncio.create_task(sem.acquire(session_id=f"w{i}", timeout=30)) for i in range(10)]
    await asyncio.sleep(0.05)
    assert sem.waiting == 10

    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    assert sem.waiting == 0
    assert len(sem._heap) == 0

    sem.release()
    assert sem.available == 1


@pytest.mark.asyncio
async def test_live_waiters_are_never_dropped():
    """The reaper must only fire when nothing is genuinely waiting."""
    from core.llm.semaphore import SessionAwareLLMScheduler

    sem = SessionAwareLLMScheduler(max_concurrent=1)
    await sem.acquire(session_id="holder")

    live = asyncio.create_task(sem.acquire(session_id="live", timeout=30))
    await asyncio.sleep(0.05)

    # A second waiter times out while `live` is still queued.
    from core.llm.semaphore import LLMConcurrencyError

    with pytest.raises(LLMConcurrencyError):
        await sem.acquire(session_id="doomed", timeout=0.01)

    assert sem.waiting == 1, "the live waiter must still be counted"
    assert len(sem._heap) >= 1, "the live waiter must not be dropped"

    sem.release()
    await asyncio.wait_for(live, timeout=2)
    sem.release()
