"""Tests for context-auto: provider-derived context windows and output caps.

Covers the 2026-08 context-auto work: the Ollama registry holding REAL
window sizes from /api/show (not the old 128K hardcode), num_ctx pinned on
Ollama requests, the ollama_num_ctx_cap VRAM guard, and the shared budget
derivation in core/llm/budget.py.
"""

from types import SimpleNamespace

from core.llm.budget import derive_max_output, derive_model_budget
from core.llm.providers.ollama import OllamaProvider
from core.llm.registry import ModelRegistry
from core.llm.types import ModelInfo

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data

    def raise_for_status(self):
        pass


class _FakeOllamaClient:
    """Stands in for both the quick and main httpx clients."""

    is_closed = False

    def __init__(self, tags=None, shows=None, fail_show=False, chat_response=None):
        self.tags = tags or []
        self.shows = shows or {}
        self.fail_show = fail_show
        self.chat_response = chat_response or {"message": {"content": "hi"}, "done": True}
        self.posted_payloads = []

    async def get(self, url):
        return _FakeResp({"models": self.tags})

    async def post(self, url, json=None):
        if url.endswith("/api/show"):
            if self.fail_show:
                raise RuntimeError("show unavailable")
            return _FakeResp(self.shows[json["name"]])
        self.posted_payloads.append(json)
        return _FakeResp(self.chat_response)


def _provider_with(fake):
    provider = OllamaProvider()
    provider._client = fake
    provider._quick_client = fake
    return provider


_QWEN_SHOW = {
    "model_info": {"qwen3.context_length": 262_144},
    "details": {},
}
_SMALL_SHOW = {
    "model_info": {"llama.context_length": 8_192},
    "details": {},
}


# ---------------------------------------------------------------------------
# Ollama: real windows from /api/show
# ---------------------------------------------------------------------------


async def test_list_models_uses_real_context_length(monkeypatch):
    """The registry-population path must report /api/show windows, not 128K."""
    monkeypatch.setattr("config.settings.ollama_num_ctx_cap", 0)
    fake = _FakeOllamaClient(
        tags=[{"name": "qwen-big"}, {"name": "tiny"}],
        shows={"qwen-big": _QWEN_SHOW, "tiny": _SMALL_SHOW},
    )
    provider = _provider_with(fake)
    models = {m.id: m for m in await provider.list_models()}
    assert models["qwen-big"].context_length == 262_144
    assert models["tiny"].context_length == 8_192


async def test_num_ctx_cap_applied(monkeypatch):
    monkeypatch.setattr("config.settings.ollama_num_ctx_cap", 65_536)
    fake = _FakeOllamaClient(shows={"qwen-big": _QWEN_SHOW})
    provider = _provider_with(fake)
    info = await provider.get_model_info("qwen-big")
    assert info.context_length == 65_536
    # Cap is applied at read time: raising it takes effect without a refetch.
    monkeypatch.setattr("config.settings.ollama_num_ctx_cap", 100_000)
    info = await provider.get_model_info("qwen-big")
    assert info.context_length == 100_000


async def test_show_failure_not_cached(monkeypatch):
    """A transient /api/show failure degrades to 128K but is retried later."""
    monkeypatch.setattr("config.settings.ollama_num_ctx_cap", 0)
    fake = _FakeOllamaClient(shows={"m": _QWEN_SHOW}, fail_show=True)
    provider = _provider_with(fake)
    info = await provider.get_model_info("m")
    assert info.context_length == 128_000
    assert "m" not in provider._info_cache
    fake.fail_show = False
    info = await provider.get_model_info("m")
    assert info.context_length == 262_144


async def test_effective_num_ctx(monkeypatch):
    monkeypatch.setattr("config.settings.context_auto", True)
    monkeypatch.setattr("config.settings.ollama_num_ctx_cap", 65_536)
    fake = _FakeOllamaClient(shows={"qwen-big": _QWEN_SHOW})
    provider = _provider_with(fake)
    assert await provider._effective_num_ctx("qwen-big") == 65_536

    # context_auto off → never pin the server window
    monkeypatch.setattr("config.settings.context_auto", False)
    assert await provider._effective_num_ctx("qwen-big") is None

    # No authoritative data → never send a guess
    monkeypatch.setattr("config.settings.context_auto", True)
    failing = _provider_with(_FakeOllamaClient(fail_show=True))
    assert await failing._effective_num_ctx("unknown") is None


async def test_chat_payload_pins_num_ctx(monkeypatch):
    monkeypatch.setattr("config.settings.context_auto", True)
    monkeypatch.setattr("config.settings.ollama_num_ctx_cap", 65_536)
    fake = _FakeOllamaClient(shows={"qwen-big": _QWEN_SHOW})
    provider = _provider_with(fake)
    await provider.chat([{"role": "user", "content": "hi"}], model="qwen-big", max_tokens=512)
    payload = fake.posted_payloads[-1]
    assert payload["options"]["num_ctx"] == 65_536
    assert payload["options"]["num_predict"] == 512


async def test_chat_payload_omits_num_ctx_without_data(monkeypatch):
    """Server default must rule when /api/show gave us nothing."""
    monkeypatch.setattr("config.settings.context_auto", True)
    fake = _FakeOllamaClient(fail_show=True)
    provider = _provider_with(fake)
    await provider.chat([{"role": "user", "content": "hi"}], model="mystery", max_tokens=512)
    assert "num_ctx" not in fake.posted_payloads[-1]["options"]


# ---------------------------------------------------------------------------
# Budget derivation (shared by agent loop + context introspection)
# ---------------------------------------------------------------------------


def _stub_registry(monkeypatch, models):
    registry = ModelRegistry()
    registry._models = models
    registry._populated = True
    stub = SimpleNamespace(router=SimpleNamespace(registry=registry))
    monkeypatch.setattr("core.llm.client.get_llm_client", lambda: stub)
    return registry


def test_derive_model_budget(monkeypatch):
    monkeypatch.setattr("config.settings.context_auto", True)
    _stub_registry(
        monkeypatch,
        {"qwen-big": ModelInfo(id="qwen-big", provider="ollama", context_length=65_536)},
    )
    assert derive_model_budget("qwen-big") == int(65_536 * 0.9)
    assert derive_model_budget("unknown-model") is None

    monkeypatch.setattr("config.settings.context_auto", False)
    assert derive_model_budget("qwen-big") is None


def test_derive_max_output(monkeypatch):
    monkeypatch.setattr("config.settings.context_auto", True)
    monkeypatch.setattr("config.settings.max_tokens", 32_000)
    _stub_registry(
        monkeypatch,
        {
            "capped/model": ModelInfo(
                id="capped/model", provider="openrouter", context_length=200_000, max_output_tokens=8_192
            ),
            "roomy/model": ModelInfo(
                id="roomy/model", provider="openrouter", context_length=200_000, max_output_tokens=128_000
            ),
            "local": ModelInfo(id="local", provider="ollama", context_length=65_536),
        },
    )
    # Provider cap below the settings ceiling wins…
    assert derive_max_output("capped/model") == 8_192
    # …but the settings ceiling still rules when the provider allows more.
    assert derive_max_output("roomy/model") == 32_000
    # No reported cap → settings value.
    assert derive_max_output("local") == 32_000

    monkeypatch.setattr("config.settings.context_auto", False)
    assert derive_max_output("capped/model") == 32_000


# ---------------------------------------------------------------------------
# OpenRouter: per-model output cap replaces the blanket 16K clamp
# ---------------------------------------------------------------------------


def test_openrouter_output_cap():
    from core.llm.providers.openrouter import OpenRouterProvider
    from core.llm.types import ProviderConfig

    provider = OpenRouterProvider(
        ProviderConfig(name="openrouter", base_url="https://openrouter.ai/api/v1", api_key="k")
    )
    provider._models_cache = [
        {"id": "big/model", "top_provider": {"max_completion_tokens": 128_000}},
        {"id": "no-cap/model", "top_provider": {}},
    ]
    assert provider._output_cap("big/model") == 128_000
    # Unknown model / missing cap → conservative legacy clamp.
    assert provider._output_cap("no-cap/model") == 16_000
    assert provider._output_cap("absent/model") == 16_000
    provider._models_cache = None
    assert provider._output_cap("big/model") == 16_000
