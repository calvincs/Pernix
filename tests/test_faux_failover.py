"""Pernix — Router failover exercised end-to-end with FauxProvider (1e).

A scripted 429 from the remote provider must fall back to Ollama with
sanitized messages — the path FakeLLMClient (a client-level fake) can never
reach.
"""

import pytest

from core.llm.errors import FailoverError, FailoverReason
from core.llm.router import ProviderRouter
from core.llm.stream_ladder import is_stream_retryable
from tests.faux_provider import FauxProvider, StubRegistry, raise_connect, raise_status, respond


def _router_with_fauxes(remote_steps, ollama_steps=None):
    router = ProviderRouter()
    remote = FauxProvider("openrouter", steps=remote_steps)
    local = FauxProvider("ollama", steps=ollama_steps or [respond("local says hi")])
    router._providers["openrouter"] = router._openrouter = remote
    router._providers["ollama"] = router._ollama = local
    router.registry = StubRegistry({"vendor/big-model": "openrouter", "local-fallback": "ollama"})
    return router, remote, local


@pytest.mark.asyncio
async def test_429_falls_back_to_ollama_with_sanitized_messages(monkeypatch):
    monkeypatch.setattr("config.settings.fallback_model", "local-fallback")
    router, remote, local = _router_with_fauxes([raise_status(429, "rate limited")])

    messages = [
        {"role": "system", "content": "be helpful"},
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "function": {"name": "bash", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "file listing"},
    ]
    resp = await router.chat(messages, model="vendor/big-model", tools=[{"type": "function"}])

    assert resp.content == "local says hi"
    assert len(remote.chat_calls) == 1 and len(local.chat_calls) == 1
    # Fallback must sanitize: tool role converted to user context, tools kwarg
    # dropped, fallback model substituted.
    fb = local.chat_calls[0]
    assert fb["model"] == "local-fallback"
    assert "tools" not in fb
    roles = [m["role"] for m in fb["messages"]]
    assert "tool" not in roles
    assert any("Tool result from c1" in m["content"] for m in fb["messages"])


@pytest.mark.asyncio
async def test_500_does_not_fall_back(monkeypatch):
    """AUTH/not-found/format errors are hard failures; only transient reasons
    (RATE_LIMIT/OVERLOADED/TIMEOUT/UNKNOWN) fall back. 401 -> AUTH -> raise."""
    from core.llm.errors import FailoverError, FailoverReason

    monkeypatch.setattr("config.settings.fallback_model", "local-fallback")
    router, remote, local = _router_with_fauxes([raise_status(401, "bad key")])

    with pytest.raises(FailoverError) as exc:
        await router.chat([{"role": "user", "content": "hi"}], model="vendor/big-model")
    assert exc.value.reason == FailoverReason.AUTH
    assert local.chat_calls == []  # never touched Ollama


@pytest.mark.asyncio
async def test_ollama_errors_never_fall_back(monkeypatch):
    """A local-provider failure has nowhere to fall back to."""
    import httpx

    monkeypatch.setattr("config.settings.fallback_model", "local-fallback")
    router, remote, local = _router_with_fauxes([], ollama_steps=[raise_status(429, "busy")])

    with pytest.raises(Exception):
        await router.chat([{"role": "user", "content": "hi"}], model="some-local")
    assert remote.chat_calls == []


# ---------------------------------------------------------------------------
# No fallback model configured: the classified error must come back as-is
# ---------------------------------------------------------------------------


async def _drain(stream) -> list:
    return [event async for event in stream]


@pytest.mark.asyncio
async def test_no_fallback_model_chat_raises_the_classified_error(monkeypatch):
    """With no fallback configured the router used to replace a 503 with
    RuntimeError("No fallback model configured") — text that matches no
    retryable marker, so the ladder's 5/10/15s backoff never ran."""
    monkeypatch.setattr("config.settings.fallback_model", "")
    router, remote, local = _router_with_fauxes([raise_status(503, "upstream overloaded")])

    with pytest.raises(FailoverError) as exc:
        await router.chat([{"role": "user", "content": "hi"}], model="vendor/big-model")

    assert exc.value.reason == FailoverReason.OVERLOADED
    assert "503" in exc.value.message and "upstream overloaded" in exc.value.message
    assert is_stream_retryable(exc.value.message)
    assert local.chat_calls == []
    assert router._semaphores["openrouter"].available == router._semaphores["openrouter"].capacity


@pytest.mark.asyncio
async def test_no_fallback_model_stream_raises_the_classified_error(monkeypatch):
    """Streaming variant: the router yielded ERROR("No fallback model
    configured") instead of raising the typed 503."""
    monkeypatch.setattr("config.settings.fallback_model", "")
    router, remote, local = _router_with_fauxes([raise_status(503, "upstream overloaded")])

    with pytest.raises(FailoverError) as exc:
        await _drain(router.chat_stream([{"role": "user", "content": "hi"}], model="vendor/big-model"))

    assert exc.value.reason == FailoverReason.OVERLOADED
    assert is_stream_retryable(exc.value.message)
    assert local.stream_calls == []
    assert router._semaphores["openrouter"].available == router._semaphores["openrouter"].capacity


@pytest.mark.asyncio
async def test_no_fallback_model_connect_error_stays_retryable(monkeypatch):
    """A ConnectError with no fallback must reach the ladder with its class
    name in the text — that name is the retry marker, and str(ConnectError)
    can be empty."""
    monkeypatch.setattr("config.settings.fallback_model", "")
    router, remote, local = _router_with_fauxes([raise_connect()])

    with pytest.raises(FailoverError) as exc:
        await _drain(router.chat_stream([{"role": "user", "content": "hi"}], model="vendor/big-model"))

    assert "ConnectError" in exc.value.message
    assert is_stream_retryable(exc.value.message)
    assert local.stream_calls == []


@pytest.mark.asyncio
async def test_semaphores_released_after_fallback(monkeypatch):
    """The remote slot is released before the fallback acquires Ollama's —
    and both end fully available."""
    monkeypatch.setattr("config.settings.fallback_model", "local-fallback")
    router, remote, local = _router_with_fauxes([raise_status(429, "rate limited")])

    await router.chat([{"role": "user", "content": "hi"}], model="vendor/big-model")
    assert router._semaphores["openrouter"].available == router._semaphores["openrouter"].capacity
    assert router._semaphores["ollama"].available == router._semaphores["ollama"].capacity
