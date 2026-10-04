"""Pernix — Router failover exercised end-to-end with FauxProvider (1e).

A scripted 429 from the remote provider must reach the configured BACKUP
model — on the backup's own provider, with the capabilities that model
actually has — the path FakeLLMClient (a client-level fake) can never reach.
"""

import pytest

from core.llm.errors import FailoverError, FailoverReason
from core.llm.router import ProviderRouter
from core.llm.stream_ladder import is_stream_retryable
from core.llm.types import ModelInfo
from tests.faux_provider import FauxProvider, StubRegistry, raise_connect, raise_status, respond

PRIMARY = "vendor/big-model"
LOCAL_BACKUP = "local-fallback"
REMOTE_BACKUP = "openai/gpt-backup"


def _router_with_fauxes(remote_steps, ollama_steps=None, *, openai_steps=None, infos=None):
    """A router whose three providers are all scripted.

    The registry maps each backup to a DIFFERENT provider than the primary's,
    including one that is not Ollama — the old stub sent every fallback model
    to "ollama", which is also where the buggy router sent them unconditionally,
    so the routing half of the failover was untestable by construction.
    """
    router = ProviderRouter()
    remote = FauxProvider("openrouter", steps=remote_steps)
    local = FauxProvider("ollama", steps=ollama_steps or [respond("local says hi")])
    remote_backup = FauxProvider("openai", steps=openai_steps or [respond("remote backup says hi")])
    router._providers["openrouter"] = router._openrouter = remote
    router._providers["ollama"] = router._ollama = local
    router._providers["openai"] = router._openai = remote_backup
    router.registry = StubRegistry(
        {PRIMARY: "openrouter", LOCAL_BACKUP: "ollama", REMOTE_BACKUP: "openai"},
        infos,
    )
    return router, remote, local


def _tool_conversation() -> list[dict]:
    return [
        {"role": "system", "content": "be helpful"},
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "function": {"name": "bash", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "file listing"},
    ]


@pytest.mark.asyncio
async def test_429_reaches_a_tool_capable_backup_with_its_tools(monkeypatch):
    """The contract this test used to certify the opposite of: a backup that
    can use tools is given them, and is given the tool history as tool history.
    Stripping both is how a turn that needed a tool came back as prose and the
    agent loop read the empty tool-call list as a finished task."""
    monkeypatch.setattr("config.settings.fallback_model", LOCAL_BACKUP)
    router, remote, local = _router_with_fauxes(
        [raise_status(429, "rate limited")],
        infos={LOCAL_BACKUP: ModelInfo(id=LOCAL_BACKUP, provider="ollama", context_length=32_000)},
    )

    tools = [{"type": "function", "function": {"name": "bash"}}]
    resp = await router.chat(_tool_conversation(), model=PRIMARY, tools=tools)

    assert resp.content == "local says hi"
    assert len(remote.chat_calls) == 1 and len(local.chat_calls) == 1
    fb = local.chat_calls[0]
    assert fb["model"] == LOCAL_BACKUP
    assert fb["tools"] == tools, "a tool-capable backup keeps the tools"
    roles = [m["role"] for m in fb["messages"]]
    assert "tool" in roles, "and keeps the tool results as tool results"
    assert any(m.get("tool_calls") for m in fb["messages"]), "and the assistant's calls"
    # Truthful identity for the caller's accounting.
    assert resp.model == LOCAL_BACKUP and resp.provider == "ollama"


@pytest.mark.asyncio
async def test_a_remote_backup_is_served_by_its_own_provider(monkeypatch):
    """settings.fallback_model is a MODEL, and config.py says it may live on
    any provider. It resolves through the registry like any other model and
    waits on that provider's semaphore — it does not go to Ollama because
    Ollama is where the fallback code happened to be written."""
    monkeypatch.setattr("config.settings.fallback_model", REMOTE_BACKUP)
    router, remote, local = _router_with_fauxes([raise_status(429, "rate limited")])

    resp = await router.chat([{"role": "user", "content": "hi"}], model=PRIMARY)

    assert resp.content == "remote backup says hi"
    assert len(router._openai.chat_calls) == 1
    assert router._openai.chat_calls[0]["model"] == REMOTE_BACKUP
    assert local.chat_calls == [], "the local daemon has never heard of this model"
    assert resp.provider == "openai"
    for name in ("openrouter", "openai", "ollama"):
        sem = router._semaphores[name]
        assert sem.available == sem.capacity, f"{name} semaphore leaked"


@pytest.mark.asyncio
async def test_a_vision_backup_keeps_the_images_a_text_backup_loses_them(monkeypatch):
    """`[image omitted]` is the right degrade for a text-only local model and
    the wrong one for a vision-capable backup — the operator configured that
    model precisely so the turn survives with its attachments."""
    seeing = ModelInfo(id=REMOTE_BACKUP, provider="openai", context_length=128_000, supports_vision=True)
    blind = ModelInfo(id=LOCAL_BACKUP, provider="ollama", context_length=32_000)
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "what is this"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}, "_kind": "image"},
            ],
        }
    ]

    monkeypatch.setattr("config.settings.fallback_model", REMOTE_BACKUP)
    router, _remote, _local = _router_with_fauxes([raise_status(429, "rate limited")], infos={REMOTE_BACKUP: seeing})
    await router.chat(list(messages), model=PRIMARY)
    kept = router._openai.chat_calls[0]["messages"][0]["content"]
    assert isinstance(kept, list) and kept[1]["image_url"] == {"url": "data:image/png;base64,AAAA"}
    assert "_kind" not in kept[1], "our own bookkeeping does not go on the wire"

    monkeypatch.setattr("config.settings.fallback_model", LOCAL_BACKUP)
    router, _remote, local = _router_with_fauxes([raise_status(429, "rate limited")], infos={LOCAL_BACKUP: blind})
    await router.chat(list(messages), model=PRIMARY)
    assert local.chat_calls[0]["messages"][0]["content"] == "what is this\n[image omitted]"


@pytest.mark.asyncio
async def test_a_tool_less_backup_surfaces_the_primary_error_instead(monkeypatch):
    """When the backup genuinely cannot take tools, the honest answer to a
    tool-carrying request is the primary's error. A text answer to a request
    that needed tools is indistinguishable, to every caller, from a finished
    task."""
    monkeypatch.setattr("config.settings.fallback_model", LOCAL_BACKUP)
    router, remote, local = _router_with_fauxes(
        [raise_status(429, "rate limited")],
        infos={LOCAL_BACKUP: ModelInfo(id=LOCAL_BACKUP, provider="ollama", context_length=8_000, supports_tools=False)},
    )

    with pytest.raises(FailoverError) as exc:
        await router.chat(_tool_conversation(), model=PRIMARY, tools=[{"type": "function"}])

    assert exc.value.reason == FailoverReason.RATE_LIMIT
    assert local.chat_calls == []
    # …but the same backup still answers a request that never wanted tools.
    router, remote, local = _router_with_fauxes(
        [raise_status(429, "rate limited")],
        infos={LOCAL_BACKUP: ModelInfo(id=LOCAL_BACKUP, provider="ollama", context_length=8_000, supports_tools=False)},
    )
    resp = await router.chat(_tool_conversation(), model=PRIMARY)
    assert resp.content == "local says hi"
    fb = local.chat_calls[0]
    assert "tools" not in fb
    assert "tool" not in [m["role"] for m in fb["messages"]]
    assert any("Tool result from c1" in m["content"] for m in fb["messages"])


@pytest.mark.asyncio
async def test_the_backup_gets_an_output_cap_derived_for_itself(monkeypatch):
    """max_tokens arrives derived for the PRIMARY (LLMClient.chat computes it
    before the router is reached). Handing a 32k reservation to a backup that
    tops out at 4k is a 400 the caller never asked for."""
    monkeypatch.setattr("config.settings.fallback_model", LOCAL_BACKUP)
    monkeypatch.setattr("config.settings.context_auto", True)
    monkeypatch.setattr("config.settings.max_tokens", 32_000)
    router, _remote, local = _router_with_fauxes(
        [raise_status(429, "rate limited")],
        infos={
            LOCAL_BACKUP: ModelInfo(id=LOCAL_BACKUP, provider="ollama", context_length=8_000, max_output_tokens=4096)
        },
    )

    await router.chat([{"role": "user", "content": "hi"}], model=PRIMARY, max_tokens=32_000)
    assert local.chat_calls[0]["max_tokens"] == 4096

    # And never above what the caller asked for.
    router, _remote, local = _router_with_fauxes(
        [raise_status(429, "rate limited")],
        infos={
            LOCAL_BACKUP: ModelInfo(id=LOCAL_BACKUP, provider="ollama", context_length=8_000, max_output_tokens=4096)
        },
    )
    await router.chat([{"role": "user", "content": "hi"}], model=PRIMARY, max_tokens=512)
    assert local.chat_calls[0]["max_tokens"] == 512


@pytest.mark.asyncio
async def test_a_backup_equal_to_the_primary_is_not_a_failover(monkeypatch):
    """Re-running the model that just failed on the provider that just failed
    is a retry wearing a failover's clothes — and it spends the one fallback
    the caller had."""
    monkeypatch.setattr("config.settings.fallback_model", PRIMARY)
    router, remote, local = _router_with_fauxes([raise_status(429, "rate limited")])

    with pytest.raises(FailoverError):
        await router.chat([{"role": "user", "content": "hi"}], model=PRIMARY)

    assert len(remote.chat_calls) == 1
    assert local.chat_calls == []


@pytest.mark.asyncio
async def test_500_does_not_fall_back(monkeypatch):
    """AUTH/not-found/format errors are hard failures; only transient reasons
    (RATE_LIMIT/OVERLOADED/TIMEOUT/UNKNOWN) fall back. 401 -> AUTH -> raise."""
    from core.llm.errors import FailoverError, FailoverReason

    monkeypatch.setattr("config.settings.fallback_model", LOCAL_BACKUP)
    router, remote, local = _router_with_fauxes([raise_status(401, "bad key")])

    with pytest.raises(FailoverError) as exc:
        await router.chat([{"role": "user", "content": "hi"}], model=PRIMARY)
    assert exc.value.reason == FailoverReason.AUTH
    assert local.chat_calls == []  # never touched Ollama


@pytest.mark.asyncio
async def test_ollama_errors_never_fall_back(monkeypatch):
    """A local-provider failure has nowhere to fall back to."""
    import httpx

    monkeypatch.setattr("config.settings.fallback_model", LOCAL_BACKUP)
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
        await router.chat([{"role": "user", "content": "hi"}], model=PRIMARY)

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
        await _drain(router.chat_stream([{"role": "user", "content": "hi"}], model=PRIMARY))

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
        await _drain(router.chat_stream([{"role": "user", "content": "hi"}], model=PRIMARY))

    assert "ConnectError" in exc.value.message
    assert is_stream_retryable(exc.value.message)
    assert local.stream_calls == []


@pytest.mark.asyncio
async def test_semaphores_released_after_fallback(monkeypatch):
    """The remote slot is released before the fallback acquires Ollama's —
    and both end fully available."""
    monkeypatch.setattr("config.settings.fallback_model", LOCAL_BACKUP)
    router, remote, local = _router_with_fauxes([raise_status(429, "rate limited")])

    await router.chat([{"role": "user", "content": "hi"}], model=PRIMARY)
    assert router._semaphores["openrouter"].available == router._semaphores["openrouter"].capacity
    assert router._semaphores["ollama"].available == router._semaphores["ollama"].capacity
