"""Pernix — Native OpenAI provider + router generalization (adaptation plan 1a)."""

import pytest

from core.llm.providers.openai import OpenAIProvider, _parse_usage
from core.llm.types import ProviderConfig


def _provider(key: str = "sk-test") -> OpenAIProvider:
    return OpenAIProvider(ProviderConfig(name="openai", base_url="https://api.openai.com/v1", api_key=key))


# ---------------------------------------------------------------------------
# Provider basics
# ---------------------------------------------------------------------------


def test_available_requires_key():
    assert _provider().available
    assert not _provider(key="").available


def test_parse_usage_openai_shape():
    usage = _parse_usage(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 50,
            "total_tokens": 1050,
            "prompt_tokens_details": {"cached_tokens": 768},
        }
    )
    assert usage.prompt_tokens == 1000
    assert usage.cache_read_tokens == 768
    assert usage.cache_write_tokens == 0


def test_parse_usage_anthropic_style_fallback():
    usage = _parse_usage(
        {
            "prompt_tokens": 500,
            "completion_tokens": 10,
            "total_tokens": 510,
            "cache_read_input_tokens": 400,
            "cache_creation_input_tokens": 100,
        }
    )
    assert usage.cache_read_tokens == 400
    assert usage.cache_write_tokens == 100


def test_build_payload_max_tokens_switching():
    p = _provider()
    payload = p._build_payload([], "gpt-4o", 4096, None, None, stream=False)
    assert payload["max_tokens"] == 4096
    assert "max_completion_tokens" not in payload
    assert "stream_options" not in payload

    p._needs_max_completion_tokens.add("gpt-4o")
    payload = p._build_payload([], "gpt-4o", 4096, None, None, stream=True)
    assert payload["max_completion_tokens"] == 4096
    assert "max_tokens" not in payload
    # Streaming must opt into usage chunks or OpenAI never sends them.
    assert payload["stream_options"] == {"include_usage": True}


def test_max_tokens_param_error_detection():
    p = _provider()
    body = "{'error': {'message': \"Unsupported parameter: 'max_tokens' is not supported. Use 'max_completion_tokens' instead.\"}}"
    assert p._is_max_tokens_param_error(400, body)
    assert not p._is_max_tokens_param_error(400, "some other error")
    assert not p._is_max_tokens_param_error(500, body)


def test_parse_response_tool_calls():
    p = _provider()
    data = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {"id": "call_1", "function": {"name": "bash", "arguments": '{"command": "ls"}'}},
                    ],
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    resp = p._parse_response(data, "gpt-4o")
    assert resp.finish_reason == "tool_calls"
    assert resp.tool_calls[0].name == "bash"
    assert resp.provider == "openai"


def test_model_info_hints():
    p = _provider()
    assert p._model_info("gpt-4.1").context_length == 1_047_576
    assert p._model_info("gpt-4o-mini").context_length == 128_000
    assert p._model_info("o3-mini").context_length == 200_000
    assert p._model_info("mystery-model").context_length == 128_000
    assert p._model_info("gpt-4o").supports_vision
    assert not p._model_info("gpt-3.5-turbo").supports_vision


# ---------------------------------------------------------------------------
# Registry routing
# ---------------------------------------------------------------------------


class _FakeProvider:
    def __init__(self, name, models, available=True):
        self.name = name
        self.available = available
        self._models = models

    async def list_models(self):
        from core.llm.types import ModelInfo

        return [ModelInfo(id=m, provider=self.name, context_length=128_000) for m in self._models]


@pytest.mark.asyncio
async def test_registry_populate_and_whitelist_routing(monkeypatch):
    from core.llm.registry import ModelRegistry

    monkeypatch.setattr("config.settings.openai_models", ["gpt-4o", "shared-model"])
    monkeypatch.setattr("config.settings.openrouter_models", ["anthropic/claude-sonnet-4"])

    reg = ModelRegistry()
    await reg.populate(
        _FakeProvider("ollama", ["llama3", "shared-model"]),
        _FakeProvider("openrouter", ["anthropic/claude-sonnet-4", "meta/llama-3-70b"]),
        _FakeProvider("openai", ["gpt-4o", "shared-model"]),
    )

    assert reg.resolve_provider("llama3") == "ollama"
    assert reg.resolve_provider("anthropic/claude-sonnet-4") == "openrouter"
    assert reg.resolve_provider("gpt-4o") == "openai"
    # Whitelisted for openai -> openai wins the collision with Ollama.
    assert reg.resolve_provider("shared-model") == "openai"
    # Non-whitelisted remote model still resolves to its provider.
    assert reg.resolve_provider("meta/llama-3-70b") == "openrouter"


@pytest.mark.asyncio
async def test_registry_bare_openai_name_routes_via_whitelist(monkeypatch):
    """Unpopulated registry: the '/'-heuristic would send 'gpt-4o' to Ollama;
    the whitelist step must win first."""
    from core.llm.registry import ModelRegistry

    monkeypatch.setattr("config.settings.openai_models", ["gpt-4o"])
    monkeypatch.setattr("config.settings.openrouter_models", [])
    reg = ModelRegistry()
    assert reg.resolve_provider("gpt-4o") == "openai"
    assert reg.resolve_provider("some-local-model") == "ollama"


# ---------------------------------------------------------------------------
# Router generalization
# ---------------------------------------------------------------------------


def test_router_has_three_providers_and_semaphores():
    from core.llm.router import ProviderRouter

    router = ProviderRouter()
    assert set(router._providers) == {"ollama", "openrouter", "openai"}
    assert set(router._semaphores) == {"ollama", "openrouter", "openai"}
    stats = router.semaphore_stats
    assert "openai" in stats and "ollama" in stats and "openrouter" in stats
    assert stats["capacity"] == sum(s.capacity for s in router._semaphores.values())


def test_router_fallback_eligible_and_semaphore_lookup():
    from core.llm.router import ProviderRouter

    router = ProviderRouter()
    assert not router._fallback_eligible(router._ollama)
    assert router._fallback_eligible(router._openrouter)
    assert router._fallback_eligible(router._openai)
    assert router.get_semaphore(router._openai) is router._openai_semaphore
    assert router.get_semaphore(router._ollama) is router._ollama_semaphore
    assert router.get_semaphore(None) is router._ollama_semaphore


def test_openai_format_providers_constant():
    from core.llm.router import OPENAI_FORMAT_PROVIDERS

    assert "openrouter" in OPENAI_FORMAT_PROVIDERS
    assert "openai" in OPENAI_FORMAT_PROVIDERS
    assert "ollama" not in OPENAI_FORMAT_PROVIDERS
