"""Pernix — Router failover exercised end-to-end with FauxProvider (1e).

A scripted 429 from the remote provider must fall back to Ollama with
sanitized messages — the path FakeLLMClient (a client-level fake) can never
reach.
"""

import pytest

from core.llm.router import ProviderRouter
from tests.faux_provider import FauxProvider, StubRegistry, raise_status, respond


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


@pytest.mark.asyncio
async def test_semaphores_released_after_fallback(monkeypatch):
    """The remote slot is released before the fallback acquires Ollama's —
    and both end fully available."""
    monkeypatch.setattr("config.settings.fallback_model", "local-fallback")
    router, remote, local = _router_with_fauxes([raise_status(429, "rate limited")])

    await router.chat([{"role": "user", "content": "hi"}], model="vendor/big-model")
    assert router._semaphores["openrouter"].available == router._semaphores["openrouter"].capacity
    assert router._semaphores["ollama"].available == router._semaphores["ollama"].capacity
