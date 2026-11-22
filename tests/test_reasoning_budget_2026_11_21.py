"""Output budget and reasoning_effort for a self-hosted reasoning model (2026-11-21).

max_tokens is shared between reasoning and the answer. The adapter clamped
every request to 16,000 tokens and never sent reasoning_effort, so a hard
turn's thinking could consume the whole budget and a small one-shot budget
(scout, titles, JSON helpers) came back empty. A self-hosted request now asks
for the caller's budget plus model-card headroom for the effort, bounded by
the model window (vLLM 400s on prompt + max_tokens > max_model_len).
api.openai.com keeps its old clamp.
"""

from __future__ import annotations

import httpx
import pytest

from config import settings
from core.llm import reasoning
from core.llm.providers.openai import OpenAIProvider
from core.llm.types import ProviderConfig

SELF_HOSTED = "http://llm.example.internal:8000/v1"
MODEL = "inkling-2-mini"
WINDOW = 262_144


def _provider(handler=None, *, base_url: str = SELF_HOSTED, key: str = "") -> OpenAIProvider:
    p = OpenAIProvider(ProviderConfig(name="openai", base_url=base_url, api_key=key))
    if handler is not None:
        p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        p._quick_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return p


@pytest.fixture(autouse=True)
def _roles(monkeypatch):
    monkeypatch.setattr(settings, "llm_model", MODEL)
    monkeypatch.setattr(settings, "background_model", "")
    monkeypatch.setattr(settings, "reasoning_effort", "")
    monkeypatch.setattr(settings, "reasoning_effort_background", "")


def _windowed(window: int = WINDOW) -> OpenAIProvider:
    p = _provider()
    p._models_cache = [{"id": MODEL, "max_model_len": window}]
    return p


def test_self_hosted_output_is_the_answer_budget_plus_reasoning_headroom():
    """No 16K clamp: 32,000 asked (settings.max_tokens on the box) plus the
    model-card headroom for the default effort (high: 16K)."""
    payload = _windowed()._build_payload([{"role": "user", "content": "hi"}], MODEL, 32_000, None, None, stream=True)
    assert payload["max_tokens"] == 32_000 + 16_384
    assert "reasoning_effort" not in payload  # "" = leave the model's default


@pytest.mark.parametrize(
    "effort,headroom", [("none", 8_192), ("low", 8_192), ("medium", 8_192), ("high", 16_384), ("xhigh", 32_768)]
)
def test_reasoning_effort_is_sent_and_sizes_the_headroom(monkeypatch, effort, headroom):
    monkeypatch.setattr(settings, "reasoning_effort", effort)
    payload = _windowed()._build_payload([{"role": "user", "content": "hi"}], MODEL, 1_500, None, None, stream=False)
    assert payload["reasoning_effort"] == effort
    # A one-shot caller's small answer budget survives the thinking.
    assert payload["max_tokens"] == 1_500 + headroom


def test_background_effort_applies_to_the_background_model_only(monkeypatch):
    monkeypatch.setattr(settings, "background_model", "small-thinker")
    monkeypatch.setattr(settings, "reasoning_effort", "high")
    monkeypatch.setattr(settings, "reasoning_effort_background", "low")
    assert reasoning.effort_for("small-thinker") == "low"
    assert reasoning.effort_for(MODEL) == "high"
    # Same model in both roles: Primary wins, as for ollama_think.
    monkeypatch.setattr(settings, "background_model", MODEL)
    assert reasoning.effort_for(MODEL) == "high"


def test_an_invalid_effort_in_settings_json_is_omitted_not_sent(monkeypatch, caplog):
    monkeypatch.setattr(settings, "reasoning_effort", "turbo")
    with caplog.at_level("WARNING", logger="pernix.llm.reasoning"):
        payload = _windowed()._build_payload([{"role": "user", "content": "x"}], MODEL, 100, None, None, stream=False)
    assert "reasoning_effort" not in payload


def test_the_request_never_exceeds_the_model_window():
    """vLLM 400s when prompt + max_tokens > max_model_len."""
    p = _windowed(window=20_000)
    big = [{"role": "user", "content": "word " * 40_000}]  # ~40K tokens by any count
    small_window = _windowed(window=60_000)
    payload = small_window._build_payload(big, MODEL, 32_000, None, None, stream=True)
    from core.context.tokens import get_estimator

    prompt = get_estimator().count_messages(big)
    assert payload["max_tokens"] + prompt <= 60_000
    assert payload["max_tokens"] >= 1_024
    # A prompt that leaves no room at all still asks for the floor; the
    # server's overflow error is what makes the agent compact.
    assert p._build_payload(big, MODEL, 32_000, None, None, stream=True)["max_tokens"] == 1_024


def test_unknown_window_leaves_the_request_alone():
    payload = _provider()._build_payload([{"role": "user", "content": "x"}], MODEL, 4_000, None, None, stream=False)
    assert payload["max_tokens"] == 4_000 + 16_384


async def test_settings_endpoint_accepts_only_known_efforts(monkeypatch):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from api.routers import health

    app = FastAPI()
    app.include_router(health.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        bad = await client.post("/api/settings", json={"reasoning_effort": "turbo"})
        assert "reasoning_effort" not in bad.json()["updated"]
        assert settings.reasoning_effort == ""
        good = await client.post(
            "/api/settings", json={"reasoning_effort": "XHigh", "reasoning_effort_background": "low"}
        )
        assert {"reasoning_effort", "reasoning_effort_background"} <= set(good.json()["updated"])
        assert (settings.reasoning_effort, settings.reasoning_effort_background) == ("xhigh", "low")
        cleared = await client.post("/api/settings", json={"reasoning_effort": ""})
        assert "reasoning_effort" in cleared.json()["updated"] and settings.reasoning_effort == ""


def test_api_openai_com_keeps_the_old_cap_and_sends_no_reasoning_fields(monkeypatch):
    monkeypatch.setattr(settings, "reasoning_effort", "high")
    p = _provider(base_url="https://api.openai.com/v1", key="sk-test")
    msgs = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b", "reasoning": "r", "reasoning_model": "gpt-4o"},
    ]
    payload = p._build_payload(msgs, "gpt-4o", 32_000, None, None, stream=False)
    assert payload["max_tokens"] == 16_000
    assert "reasoning_effort" not in payload and "stop" not in payload
    assert payload["messages"][1] == {"role": "assistant", "content": "b"}
    p._needs_max_completion_tokens.add("gpt-4o")
    assert p._build_payload(msgs, "gpt-4o", 32_000, None, None, stream=False)["max_completion_tokens"] == 16_000
