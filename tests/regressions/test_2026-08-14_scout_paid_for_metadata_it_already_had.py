"""Regression: per-turn latency spent on model metadata already in memory.

Measured on box.ventibean.com, 2026-08-14, on a warm process: 20.4s from
message to reply, 18.3s of it scout, and 4.7s of *that* was scout's model
listing — `/api/tags` plus an `/api/show` per uncached model — rendered into
an AVAILABLE MODELS text block whose every field (id, provider,
context_length, vision) the registry already held. It was also the slowest
gatherer, so it set the floor for the whole gather phase. Cold, the same
call took ~20s.

The reason models stayed uncached is the second half of this: a failed
`/api/show` was neither cached nor logged. The 128K fallback is correctly
never stored (it must never become a `num_ctx`), but the *failure* wasn't
either, so every subsequent `list_models()` retried the same models, with
35 requests through a 5-connection pool re-creating the timeouts that
caused the misses. Three back-to-back `/api/models` calls measured 23.4s →
2.0s → 0.38s as the cache slowly filled in.

Fixes: scout reads the registry catalog; `/api/show` failures are
negative-cached for a few minutes and logged at warning; the quick client's
pool is sized to the fan-out.
"""

from types import SimpleNamespace

import httpx
import pytest

from core.llm.providers.ollama import OllamaProvider
from core.llm.types import ModelInfo

# --- scout reads the registry, not the provider -----------------------------


async def test_scout_model_block_comes_from_the_registry(monkeypatch):
    from core.llm.registry import ModelRegistry
    from core.scout import runner

    registry = ModelRegistry()
    registry._models = {
        "local:9b": ModelInfo(id="local:9b", provider="ollama", context_length=65_536),
        "remote/big": ModelInfo(id="remote/big", provider="openrouter", context_length=200_000, supports_vision=True),
    }
    registry._populated = True

    live_calls = {"n": 0}

    async def list_models():
        live_calls["n"] += 1
        return []

    client = SimpleNamespace(router=SimpleNamespace(registry=registry), list_models=list_models)
    monkeypatch.setattr("core.llm.client.get_llm_client", lambda: client)

    block = await runner.build_model_catalog_block()

    assert "- local:9b (ollama, ctx=65,536)" in block
    assert "- remote/big (openrouter, ctx=200,000 [vision])" in block
    assert live_calls["n"] == 0, "the registry already holds every field this block renders"


async def test_scout_still_falls_back_to_a_live_listing(monkeypatch):
    """An unpopulated registry must not silently drop the models block."""
    from core.llm.registry import ModelRegistry
    from core.scout import runner

    registry = ModelRegistry()  # never populated

    async def list_models():
        return [ModelInfo(id="live:1b", provider="ollama", context_length=8_192)]

    client = SimpleNamespace(router=SimpleNamespace(registry=registry), list_models=list_models)
    monkeypatch.setattr("core.llm.client.get_llm_client", lambda: client)

    block = await runner.build_model_catalog_block()
    assert "live:1b" in block


# --- /api/show failures are remembered ---------------------------------------


@pytest.fixture
def provider():
    return OllamaProvider()


async def test_failed_show_is_not_retried_immediately(provider, monkeypatch, caplog):
    calls = {"n": 0}

    class _Client:
        async def post(self, *a, **kw):
            calls["n"] += 1
            raise httpx.ReadTimeout("too slow")

    monkeypatch.setattr(provider, "_get_quick_client", lambda: _Client())

    with caplog.at_level("WARNING", logger="pernix.llm.ollama"):
        first = await provider.get_model_info("slow:70b")
    second = await provider.get_model_info("slow:70b")

    assert calls["n"] == 1, "the second lookup must be served by the negative cache"
    # Both still answer with the safe default…
    assert first.context_length == 128_000
    assert second.context_length == 128_000
    # …which is never cached as real metadata, so no num_ctx is ever guessed.
    assert "slow:70b" not in provider._info_cache
    assert await provider._effective_num_ctx("slow:70b") is None
    assert any("api/show failed" in r.getMessage() for r in caplog.records), "silence is what hid this"


async def test_negative_cache_expires(provider, monkeypatch):
    calls = {"n": 0}

    class _Client:
        async def post(self, *a, **kw):
            calls["n"] += 1
            raise httpx.ReadTimeout("too slow")

    monkeypatch.setattr(provider, "_get_quick_client", lambda: _Client())

    await provider.get_model_info("slow:70b")
    provider._info_failed_until["slow:70b"] = 0.0  # pretend the TTL elapsed
    await provider.get_model_info("slow:70b")

    assert calls["n"] == 2, "a model that was merely mid-pull must recover without a restart"


async def test_success_still_caches_and_clears_the_way(provider, monkeypatch):
    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"model_info": {"qwen3.context_length": 262_144}, "details": {}}

    class _Client:
        async def post(self, *a, **kw):
            return _Resp()

    monkeypatch.setattr(provider, "_get_quick_client", lambda: _Client())
    monkeypatch.setattr("config.settings.ollama_num_ctx_cap", 0)

    info = await provider.get_model_info("qwen3.8:27b")

    assert info.context_length == 262_144
    assert provider._info_cache["qwen3.8:27b"].context_length == 262_144
