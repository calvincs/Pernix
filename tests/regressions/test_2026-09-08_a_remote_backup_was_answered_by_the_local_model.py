"""Harness review 2026-09-08 (S10): every failover the router performed was
answered by Ollama, whatever model the operator had configured as the backup.

`_fallback_chat` and `_fallback_stream` popped `tools`, took
`self._ollama_semaphore` and called `self._ollama` — three hard-coded
references to one provider inside a method whose entire input was a model
NAME. `settings.fallback_model` is a model, and config.py's three-role scheme
says plainly that it may live on any provider, so `openai/gpt-backup` was
dispatched to the local daemon that had never heard of it: the real provider
was never called, its semaphore never taken, its rate limits never respected.

The router intercepted the error before the model-aware ladder could see it
(chat_stream's three FALLBACK_REASONS branches), so the substitution was also
invisible above: `StreamOutcome.model` still named the primary, `tried_fallback`
stayed False, and the usage rows the ladder writes booked free local tokens
against the remote primary's price list. It also bypassed the sticky-failover
fix from 311e1e9 — that commit repaired `agent.py::_resolve_effective_model`
and said in its own message that the router had the same mismatch.

The worst of it was `kwargs.pop("tools", None)`. A tool-requiring round came
back as prose, `collected_tool_calls` was empty, and the agent loop ended the
turn exactly as it would for a model that had finished the task.

The fix gives failover one owner per call shape. Streaming has the ladder —
every caller of `router.chat_stream` reaches it through `stream_with_failover`
— so typed errors now propagate and the ladder, which re-derives the output
cap, re-normalizes for the backup's provider and keeps the tools, does the
switching. `chat()` mostly has nothing above it (twenty call sites reach
`LLMClient.chat` bare), so the router keeps that half — but resolves the
backup through the registry, waits on THAT provider's semaphore, derives the
backup's own output cap, keeps the tools and images the backup is capable of,
and returns its true model and provider. A backup that genuinely cannot take
tools no longer answers a tool-carrying request at all: the primary's error is
the honest reply.
"""

from __future__ import annotations

import pytest

from core.llm.client import LLMClient, chat_with_backup
from core.llm.errors import FailoverError, FailoverReason
from core.llm.router import ProviderRouter
from core.llm.stream_ladder import stream_with_failover
from core.llm.types import ModelInfo, StreamEventType, TokenUsage
from tests.faux_provider import FauxProvider, StubRegistry, raise_status, respond, stream_then_raise, stream_tokens

PRIMARY = "vendor/big-model"
REMOTE_BACKUP = "openai/gpt-backup"
LOCAL_BACKUP = "local-fallback"

TOOLS = [{"type": "function", "function": {"name": "bash", "description": "run a command"}}]


def _router(remote_steps, *, backup_steps=None, ollama_steps=None, infos=None) -> ProviderRouter:
    """A router whose three providers are scripted and whose registry knows
    which one owns each model — the question the shipped code never asked."""
    router = ProviderRouter()
    router._providers["openrouter"] = router._openrouter = FauxProvider("openrouter", steps=remote_steps)
    router._providers["openai"] = router._openai = FauxProvider("openai", steps=backup_steps or [respond("backup")])
    router._providers["ollama"] = router._ollama = FauxProvider("ollama", steps=ollama_steps or [respond("local")])
    router.registry = StubRegistry(
        {PRIMARY: "openrouter", REMOTE_BACKUP: "openai", LOCAL_BACKUP: "ollama"},
        infos,
    )
    return router


def _client(router: ProviderRouter) -> LLMClient:
    client = LLMClient()
    client.router = router
    return client


# ---------------------------------------------------------------------------
# The one-shot half: the router still owns it, and now does it honestly
# ---------------------------------------------------------------------------


async def test_a_remote_backup_is_called_on_its_own_provider(monkeypatch):
    """The whole finding in one assertion: the model the operator configured
    is the model that gets called, at the provider that owns it."""
    monkeypatch.setattr("config.settings.fallback_model", REMOTE_BACKUP)
    router = _router([raise_status(429, "rate limited")], backup_steps=[respond("backup answered")])

    resp = await router.chat([{"role": "user", "content": "hi"}], model=PRIMARY)

    assert resp.content == "backup answered"
    assert router._openai.chat_calls[0]["model"] == REMOTE_BACKUP
    assert router._ollama.chat_calls == [], "Ollama is not a synonym for 'the backup'"
    # And the caller can bill it correctly.
    assert (resp.model, resp.provider) == (REMOTE_BACKUP, "openai")


async def test_the_backups_own_semaphore_is_the_one_that_is_held(monkeypatch):
    """A backup dispatched to Ollama also took Ollama's concurrency slot, so
    the remote's limit was never enforced and the local queue was crowded by
    work that never ran there."""
    monkeypatch.setattr("config.settings.fallback_model", REMOTE_BACKUP)
    router = _router([raise_status(429, "rate limited")])
    seen: dict[str, int] = {}

    async def watching_chat(messages, **kwargs):
        seen.update({name: sem.available for name, sem in router._semaphores.items()})
        return await FauxProvider.chat(router._openai, messages, **kwargs)

    router._openai.chat = watching_chat

    await router.chat([{"role": "user", "content": "hi"}], model=PRIMARY)

    openai_sem = router._semaphores["openai"]
    assert seen["openai"] == openai_sem.capacity - 1, "the backup's provider must be the one waiting in line"
    assert seen["ollama"] == router._semaphores["ollama"].capacity, "the local queue is not involved"
    assert seen["openrouter"] == router._semaphores["openrouter"].capacity, "released before the backup ran"
    for name, sem in router._semaphores.items():
        assert sem.available == sem.capacity, f"{name} slot leaked"


async def test_a_tool_requiring_request_does_not_come_back_as_a_finished_answer(monkeypatch):
    """`kwargs.pop("tools", None)` produced a response with no tool calls, and
    an empty tool-call list is precisely what the agent loop treats as 'the
    model is done'. A capable backup keeps the tools; an incapable one is not
    allowed to answer at all."""
    monkeypatch.setattr("config.settings.fallback_model", REMOTE_BACKUP)
    capable = {REMOTE_BACKUP: ModelInfo(id=REMOTE_BACKUP, provider="openai", context_length=128_000)}
    router = _router([raise_status(429, "rate limited")], infos=capable)

    await router.chat([{"role": "user", "content": "list the files"}], model=PRIMARY, tools=TOOLS)
    assert router._openai.chat_calls[0]["tools"] == TOOLS

    tool_less = {
        REMOTE_BACKUP: ModelInfo(id=REMOTE_BACKUP, provider="openai", context_length=128_000, supports_tools=False)
    }
    router = _router([raise_status(429, "rate limited")], infos=tool_less)

    with pytest.raises(FailoverError) as exc:
        await router.chat([{"role": "user", "content": "list the files"}], model=PRIMARY, tools=TOOLS)

    assert exc.value.reason == FailoverReason.RATE_LIMIT, "the primary's real error, not a silent text answer"
    assert router._openai.chat_calls == []


async def test_the_backup_is_not_handed_the_primarys_output_reservation(monkeypatch):
    """LLMClient.chat derives max_tokens for the model it was asked for, and
    the fallback overwrote only `model` — so a 32,000-token reservation
    computed for a large remote followed the request onto a small backup."""
    monkeypatch.setattr("config.settings.fallback_model", LOCAL_BACKUP)
    monkeypatch.setattr("config.settings.context_auto", True)
    monkeypatch.setattr("config.settings.max_tokens", 32_000)
    small = {LOCAL_BACKUP: ModelInfo(id=LOCAL_BACKUP, provider="ollama", context_length=32_000, max_output_tokens=4096)}
    router = _router([raise_status(429, "rate limited")], infos=small)

    await router.chat([{"role": "user", "content": "hi"}], model=PRIMARY, max_tokens=32_000)

    assert router._ollama.chat_calls[0]["max_tokens"] == 4096


async def test_a_failed_backup_is_not_retried_by_the_layer_above(monkeypatch):
    """chat_with_backup retries settings.fallback_model itself when a call
    raises. With the router silently trying the backup first, one bad minute
    on the backup was paid for twice."""
    monkeypatch.setattr("config.settings.fallback_model", REMOTE_BACKUP)
    router = _router(
        [raise_status(429, "rate limited")],
        backup_steps=[raise_status(503, "backup down"), raise_status(503, "backup down again")],
    )

    with pytest.raises(Exception):
        await chat_with_backup(_client(router), model=PRIMARY, messages=[{"role": "user", "content": "hi"}])

    assert len(router._openai.chat_calls) == 1, "the backup had its turn inside the router"


# ---------------------------------------------------------------------------
# The streaming half: the ladder owns it, and can see what it did
# ---------------------------------------------------------------------------


async def _run_ladder(client, *, tools=None, model=PRIMARY):
    events: list[dict] = []
    return (
        await stream_with_failover(
            client=client,
            session_id="s-s10",
            emit=events.append,
            messages=[{"role": "user", "content": "hi"}],
            base_messages=[{"role": "user", "content": "hi"}],
            static_prefix_chars=0,
            tools=tools,
            model=model,
            max_output_cap=0,
            goal_id=None,
            sched_created_at=float("inf"),
            sched_priority=0,
        ),
        events,
    )


async def test_the_ladder_learns_which_model_answered(monkeypatch):
    """The router's substitution happened below the ladder, so the outcome the
    turn reported — and persisted on the assistant row — named a model that had
    not produced a single token of it."""
    monkeypatch.setattr("config.settings.fallback_model", LOCAL_BACKUP)
    router = _router([raise_status(429, "rate limited")], ollama_steps=[stream_tokens("local answer")])
    outcome, events = await _run_ladder(_client(router), tools=TOOLS)

    assert outcome.error is None
    assert outcome.content == "local answer"
    assert outcome.model == LOCAL_BACKUP and outcome.tried_fallback is True
    # The ladder announces the switch; the router's version was silent.
    assert {"type": "stream.fallback", "model": LOCAL_BACKUP} in events
    # And it hands the backup the tools the round needs.
    assert router._ollama.stream_calls[0]["tools"] == TOOLS
    assert router._ollama.stream_calls[0]["model"] == LOCAL_BACKUP


async def test_usage_is_booked_against_the_model_that_actually_served_it(monkeypatch):
    """Verified on real rows: a local answer recorded under vendor/big-model /
    openrouter, priced from the primary's rate card."""
    monkeypatch.setattr("config.settings.fallback_model", LOCAL_BACKUP)
    monkeypatch.setattr("config.settings.model_prices", {PRIMARY: {"in": 3.0, "out": 15.0}})
    usage = TokenUsage(prompt_tokens=1000, completion_tokens=500, total_tokens=1500)
    router = _router([raise_status(429, "rate limited")], ollama_steps=[stream_tokens("local", usage=usage)])

    booked: list[dict] = []
    monkeypatch.setattr("db.models.add_token_usage", lambda **kw: booked.append(kw))

    outcome, _events = await _run_ladder(_client(router))

    assert outcome.error is None
    assert len(booked) == 1
    assert booked[0]["model"] == LOCAL_BACKUP
    assert booked[0]["provider"] == "ollama"
    assert not booked[0]["cost_estimate"], "the local model is not on the primary's price list"


async def test_a_partial_stream_is_still_never_restarted(monkeypatch):
    """The one rule that does not move: once a token has reached the caller,
    the request is not re-run underneath it. The router reports the failure on
    the stream it already opened rather than raising into a caller that would
    treat it as a request that never ran."""
    monkeypatch.setattr("config.settings.fallback_model", LOCAL_BACKUP)
    router = _router([stream_then_raise(["half an "], 429, "rate limited")])

    events = [e async for e in router.chat_stream([{"role": "user", "content": "hi"}], model=PRIMARY)]

    assert router._ollama.stream_calls == []
    assert [e.type for e in events][-1] == StreamEventType.ERROR
    assert "".join(e.content or "" for e in events if e.type == StreamEventType.TOKEN) == "half an "
    for name, sem in router._semaphores.items():
        assert sem.available == sem.capacity, f"{name} slot leaked"


async def test_the_stream_fails_over_exactly_once(monkeypatch):
    """Two layers each entitled to one failover is two failovers. With the
    router out of that business, a backup that also fails ends the ladder
    instead of starting another round of substitutions."""
    monkeypatch.setattr("config.settings.fallback_model", LOCAL_BACKUP)
    router = _router(
        [raise_status(429, "rate limited")],
        ollama_steps=[raise_status(429, "backup rate limited")],
    )

    outcome, events = await _run_ladder(_client(router))

    assert outcome.error and "429" in outcome.error
    assert outcome.model == LOCAL_BACKUP
    assert len(router._openrouter.stream_calls) == 1
    assert len(router._ollama.stream_calls) == 1
    assert [e for e in events if e.get("type") == "stream.fallback"] == [
        {"type": "stream.fallback", "model": LOCAL_BACKUP}
    ]
