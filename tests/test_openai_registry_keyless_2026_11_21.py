"""A keyless self-hosted server's models reach the registry (2026-11-21).

The OpenAI adapter sent "Authorization: Bearer " when no key was set. httpx
rejects the trailing space as an illegal header value, so every request to a
keyless vLLM failed before it left the process, /v1/models included
("Failed to fetch OpenAI models: Illegal header value b'Bearer '"). The
failed fetch was then cached as an empty list for the life of the process,
and the lazy repopulate in budget.ensure_model_known reuses that cache. The
served model (inkling-2-mini, max_model_len 262,144) was never in the
registry, and budgets fell back to the manual context_budget / max_tokens.
"""

from __future__ import annotations

import json

import httpx
import pytest

from config import settings
from core.llm.providers.openai import OpenAIProvider
from core.llm.types import ProviderConfig

SELF_HOSTED = "http://llm.example.internal:8000/v1"
MODEL = "inkling-2-mini"
WINDOW = 262_144


def _sse(*chunks: dict | str) -> bytes:
    return "".join(f"data: {c if isinstance(c, str) else json.dumps(c)}\n\n" for c in chunks).encode()


def _provider(handler=None, *, base_url: str = SELF_HOSTED, key: str = "") -> OpenAIProvider:
    p = OpenAIProvider(ProviderConfig(name="openai", base_url=base_url, api_key=key))
    if handler is not None:
        p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        p._quick_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return p


def test_no_api_key_sends_no_authorization_header():
    """`Bearer ` with nothing after it is an illegal header to httpx."""
    assert "Authorization" not in _provider(key="")._headers()
    assert _provider(key="sk-x")._headers()["Authorization"] == "Bearer sk-x"


# ---------------------------------------------------------------------------
# Registry: openai-provider models arrive with their real window
# ---------------------------------------------------------------------------


class _NoOllama:
    name = "ollama"

    async def list_models(self):
        return []


@pytest.mark.asyncio
async def test_registry_knows_a_keyless_self_hosted_model_and_its_window(monkeypatch):
    from core.llm.registry import ModelRegistry

    monkeypatch.setattr(settings, "openai_models", [])
    seen = {}

    def handler(request):
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json={"data": [{"id": MODEL, "max_model_len": WINDOW}]})

    reg = ModelRegistry()
    await reg.populate(_NoOllama(), _provider(handler))
    info = reg.get_model_info(MODEL)
    assert info is not None and info.provider == "openai" and info.context_length == WINDOW
    assert reg.resolve_provider(MODEL) == "openai"
    assert "authorization" not in seen["headers"]


@pytest.mark.asyncio
async def test_a_failed_model_fetch_is_retried_not_cached_forever(monkeypatch):
    """The lazy repopulate (budget.ensure_model_known) reuses provider caches,
    so caching [] after one failure kept the model unknown until restart."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="loading")
        return httpx.Response(200, json={"data": [{"id": MODEL, "max_model_len": WINDOW}]})

    p = _provider(handler)
    assert await p.list_models() == []
    assert await p.list_models() == []  # inside the retry window: no new request
    assert calls["n"] == 1
    p._models_retry_at = 0.0  # the window has passed
    assert [m.id for m in await p.list_models()] == [MODEL]
    assert (await p.get_model_info(MODEL)).context_length == WINDOW
