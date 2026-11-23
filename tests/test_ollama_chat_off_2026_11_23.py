"""An embeddings-only Ollama does not offer chat models (2026-11-23).

An agent gave three workers a large vision model that only the Ollama server
listed. That server ran on CPU and was there for embeddings. Each reply took
longer than the read timeout, so the workers produced nothing and the parent
waited on them for half an hour. With ollama_chat_enabled off, Ollama's
models stay out of the registry and out of the model list.
"""

from __future__ import annotations

from config import settings
from core.llm.registry import ModelRegistry
from core.llm.types import ModelInfo


class _Provider:
    def __init__(self, name: str, ids: list[str]):
        self.name = name
        self.available = True
        self.calls = 0
        self._ids = ids

    async def list_models(self):
        self.calls += 1
        return [ModelInfo(id=i, provider=self.name, context_length=128_000) for i in self._ids]


async def test_ollama_models_stay_out_of_the_registry_when_chat_is_off(monkeypatch):
    monkeypatch.setattr(settings, "ollama_chat_enabled", False)
    ollama = _Provider("ollama", ["big-vision:32b", "embedder:latest"])
    remote = _Provider("openai", ["served-model"])
    reg = ModelRegistry()
    await reg.populate(ollama, remote)
    assert [m.id for m in reg.all_models()] == ["served-model"]
    assert reg.get_model_info("big-vision:32b") is None
    assert ollama.calls == 0


async def test_ollama_models_are_listed_by_default(monkeypatch):
    monkeypatch.setattr(settings, "ollama_chat_enabled", True)
    ollama = _Provider("ollama", ["big-vision:32b"])
    remote = _Provider("openai", ["served-model"])
    reg = ModelRegistry()
    await reg.populate(ollama, remote)
    assert sorted(m.id for m in reg.all_models()) == ["big-vision:32b", "served-model"]
