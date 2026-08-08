"""Regression: the streaming providers swallowed every failover signal.

Shipped defect (architecture review 2026-08-07, Appendix C §2): the blanket
`except Exception` at the bottom of OpenRouterProvider.chat_stream /
OpenAIProvider.chat_stream sat on the try that enclosed `raise_for_status()`
AND the deliberate `raise FailoverError(CONTEXT_OVERFLOW, ...)`. Every one of
them was downgraded to a StreamEvent(ERROR), so:

  - ProviderRouter.chat_stream's whole failover block never fired — streaming
    fallback to Ollama, documented as layer 1 of 3, did not exist;
  - agent.py's `except FailoverError ... CONTEXT_OVERFLOW -> compact and
    retry` was unreachable from any streaming call, so an overflow burned the
    turn's fallback model instead of compacting.

The existing router test mocked the *router's* input with a generator that
raised before its first yield, so it never touched the provider's swallowing.
These tests drive the real provider stream function over a mocked transport.

Fix: FailoverError and classified transport errors are re-raised while
nothing has been yielded yet; once a TOKEN or TOOL_CALL has reached the
caller the stream terminates with an ERROR event instead, because failing
over mid-stream would persist "<partial primary><complete fallback>" and bill
both providers. The router enforces the same fence for providers that raise.
"""

import httpx
import pytest

from core.llm.errors import FailoverError, FailoverReason
from core.llm.providers.openai import OpenAIProvider
from core.llm.providers.openrouter import OpenRouterProvider
from core.llm.types import ProviderConfig, StreamEventType

_OVERFLOW_MSG = "This endpoint's maximum context length is 8192 tokens"


def _sse(*chunks: str) -> bytes:
    return "".join(f"data: {c}\n\n" for c in chunks).encode()


def _with_transport(provider, handler):
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return provider


def _openrouter(handler) -> OpenRouterProvider:
    config = ProviderConfig(name="openrouter", base_url="https://openrouter.test/api/v1", api_key="k")
    return _with_transport(OpenRouterProvider(config), handler)


def _openai(handler) -> OpenAIProvider:
    config = ProviderConfig(name="openai", base_url="https://openai.test/v1", api_key="k")
    return _with_transport(OpenAIProvider(config), handler)


async def _drain(stream) -> list:
    return [event async for event in stream]


# ---------------------------------------------------------------------------
# Pre-stream: the provider must raise, not yield ERROR
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("factory", [_openrouter, _openai])
async def test_http_error_before_first_token_raises_classified(factory):
    provider = factory(lambda request: httpx.Response(429, text="rate limited"))

    with pytest.raises(FailoverError) as exc:
        await _drain(provider.chat_stream([{"role": "user", "content": "hi"}], model="m"))

    assert exc.value.reason == FailoverReason.RATE_LIMIT


@pytest.mark.parametrize("factory", [_openrouter, _openai])
async def test_context_overflow_chunk_before_first_token_raises(factory):
    """The 200-with-error-body path: this is what reaches agent.py's
    compact-and-retry handler, and it used to become a generic ERROR event."""
    body = _sse('{"error": {"message": "%s"}}' % _OVERFLOW_MSG)
    provider = factory(lambda request: httpx.Response(200, content=body))

    with pytest.raises(FailoverError) as exc:
        await _drain(provider.chat_stream([{"role": "user", "content": "hi"}], model="m"))

    assert exc.value.reason == FailoverReason.CONTEXT_OVERFLOW


@pytest.mark.parametrize("factory", [_openrouter, _openai])
async def test_connect_error_before_first_token_raises_failover(factory):
    def _handler(request):
        raise httpx.ConnectError("no route to host")

    provider = factory(_handler)

    with pytest.raises(FailoverError) as exc:
        await _drain(provider.chat_stream([{"role": "user", "content": "hi"}], model="m"))

    assert exc.value.reason in FailoverReason


@pytest.mark.parametrize("factory", [_openrouter, _openai])
async def test_failing_stream_emits_no_done_event(factory):
    """The finally block used to yield DONE unconditionally, which would hand
    the router a clean end-of-stream and surface the exception one __anext__
    too late."""
    provider = factory(lambda request: httpx.Response(500, text="upstream boom"))

    seen = []
    with pytest.raises(FailoverError):
        async for event in provider.chat_stream([{"role": "user", "content": "hi"}], model="m"):
            seen.append(event)

    assert seen == []


# ---------------------------------------------------------------------------
# Mid-stream: tokens already delivered — terminate, never fail over
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("factory", [_openrouter, _openai])
async def test_overflow_after_tokens_terminates_instead_of_raising(factory):
    body = _sse(
        '{"choices": [{"delta": {"content": "partial answer"}}]}',
        '{"error": {"message": "%s"}}' % _OVERFLOW_MSG,
    )
    provider = factory(lambda request: httpx.Response(200, content=body))

    events = await _drain(provider.chat_stream([{"role": "user", "content": "hi"}], model="m"))

    kinds = [e.type for e in events]
    assert StreamEventType.TOKEN in kinds
    assert StreamEventType.ERROR in kinds
    # Terminal, not a failover: the caller keeps its partial content and the
    # DONE closes the stream normally.
    assert kinds[-1] == StreamEventType.DONE


# ---------------------------------------------------------------------------
# Router: the failover block is now reachable
# ---------------------------------------------------------------------------


def _router_with_real_openrouter(handler, monkeypatch):
    from core.llm.router import ProviderRouter
    from tests.faux_provider import FauxProvider, StubRegistry, respond, stream_tokens

    monkeypatch.setattr("config.settings.fallback_model", "local-fallback")
    router = ProviderRouter()
    remote = _openrouter(handler)
    local = FauxProvider("ollama", steps=[stream_tokens("local "), respond("local")])
    router._providers["openrouter"] = router._openrouter = remote
    router._providers["ollama"] = router._ollama = local
    router.registry = StubRegistry({"vendor/big-model": "openrouter", "local-fallback": "ollama"})
    return router, local


async def test_router_streaming_failover_reaches_ollama(monkeypatch):
    """End-to-end proof the swallowing is gone: a 429 from the real provider
    stream now falls back to Ollama. Before the fix the router saw a clean
    generator and this fell through with a single ERROR event."""

    def _rate_limited(request):
        return httpx.Response(429, text="rate limited")

    router, local = _router_with_real_openrouter(_rate_limited, monkeypatch)

    events = await _drain(router.chat_stream([{"role": "user", "content": "hi"}], model="vendor/big-model"))

    assert len(local.stream_calls) == 1
    assert "local " in "".join(e.content or "" for e in events if e.type == StreamEventType.TOKEN)


async def test_router_does_not_fail_over_on_context_overflow(monkeypatch):
    """CONTEXT_OVERFLOW is excluded from FALLBACK_REASONS on purpose: it must
    reach agent.py so the turn compacts instead of burning a fallback model."""
    body = _sse('{"error": {"message": "%s"}}' % _OVERFLOW_MSG)
    router, local = _router_with_real_openrouter(lambda request: httpx.Response(200, content=body), monkeypatch)

    with pytest.raises(FailoverError) as exc:
        await _drain(router.chat_stream([{"role": "user", "content": "hi"}], model="vendor/big-model"))

    assert exc.value.reason == FailoverReason.CONTEXT_OVERFLOW
    assert local.stream_calls == []


async def test_router_refuses_midstream_failover(monkeypatch):
    """A provider that raises after streaming tokens must not be failed over —
    agent.py accumulates TOKENs unconditionally, so the saved assistant
    message would be the partial primary plus the complete fallback."""
    from core.llm.router import ProviderRouter
    from tests.faux_provider import FauxProvider, StubRegistry, respond, stream_then_raise, stream_tokens

    monkeypatch.setattr("config.settings.fallback_model", "local-fallback")
    router = ProviderRouter()
    remote = FauxProvider("openrouter", steps=[stream_then_raise(["half an "], 429, "rate limited")])
    local = FauxProvider("ollama", steps=[stream_tokens("local "), respond("local")])
    router._providers["openrouter"] = router._openrouter = remote
    router._providers["ollama"] = router._ollama = local
    router.registry = StubRegistry({"vendor/big-model": "openrouter", "local-fallback": "ollama"})

    events = await _drain(router.chat_stream([{"role": "user", "content": "hi"}], model="vendor/big-model"))

    assert local.stream_calls == [], "mid-stream failover would duplicate content"
    assert [e.type for e in events][-1] == StreamEventType.ERROR
    assert "".join(e.content or "" for e in events if e.type == StreamEventType.TOKEN) == "half an "


# ---------------------------------------------------------------------------
# Usage parsing: one parser, both cache-token dialects
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("factory", [_openrouter, _openai])
async def test_streaming_usage_reads_openai_cache_shape(factory):
    """openrouter.py read only cache_read_input_tokens, so every openai/*
    model routed through OpenRouter reported zero cache reads and the cost
    tooltip presented it as "no caching happening"."""
    body = _sse(
        '{"usage": {"prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105,'
        ' "prompt_tokens_details": {"cached_tokens": 80}}}'
    )
    provider = factory(lambda request: httpx.Response(200, content=body))

    events = await _drain(provider.chat_stream([{"role": "user", "content": "hi"}], model="m"))
    usage = next(e.usage for e in events if e.type == StreamEventType.USAGE)

    assert usage.cache_read_tokens == 80


@pytest.mark.parametrize("factory", [_openrouter, _openai])
async def test_streaming_usage_reads_anthropic_cache_shape(factory):
    body = _sse(
        '{"usage": {"prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105,'
        ' "cache_read_input_tokens": 70, "cache_creation_input_tokens": 20}}'
    )
    provider = factory(lambda request: httpx.Response(200, content=body))

    events = await _drain(provider.chat_stream([{"role": "user", "content": "hi"}], model="m"))
    usage = next(e.usage for e in events if e.type == StreamEventType.USAGE)

    assert usage.cache_read_tokens == 70
    assert usage.cache_write_tokens == 20


def test_non_streaming_usage_shared_between_providers():
    data = {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "prompt_tokens_details": {"cached_tokens": 90},
        },
    }
    orc = OpenRouterProvider(ProviderConfig(name="openrouter", base_url="https://x/api/v1", api_key="k"))
    oai = OpenAIProvider(ProviderConfig(name="openai", base_url="https://y/v1", api_key="k"))

    assert orc._parse_response(data, "openai/gpt-4o").usage.cache_read_tokens == 90
    assert oai._parse_response(data, "gpt-4o").usage.cache_read_tokens == 90
