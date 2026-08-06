"""Pernix — Model registry: authoritative model-to-provider mapping.

Replaces the fragile is_openrouter_model() heuristic with a runtime catalog
built from actual provider API responses. Handles collisions (same model ID
on both Ollama and OpenRouter) via explicit policy.
"""

from __future__ import annotations

import asyncio
import logging
import os

from config import settings
from core.llm.types import ModelInfo

logger = logging.getLogger("pernix.llm.registry")


_warned_models: set[str] = set()  # Track warned model names to avoid log spam


class ModelRegistry:
    """Runtime catalog of known models and their providers.

    Collision policy:
      - Ollama wins by default (local, free, lower latency).
      - If a model is explicitly listed in settings.openrouter_models,
        OpenRouter wins (user explicitly chose that provider).
    """

    def __init__(self):
        self._models: dict[str, ModelInfo] = {}
        self._lock = asyncio.Lock()
        self._populated = False

    @property
    def populated(self) -> bool:
        return self._populated

    @staticmethod
    def _remote_whitelists() -> dict[str, set[str]]:
        """Explicit model->provider intent, per remote provider name."""
        return {
            "openrouter": set(settings.openrouter_models or []),
            "openai": set(settings.openai_models or []),
        }

    async def populate(self, ollama_provider, *remote_providers) -> None:
        """Fetch model lists from all providers and build the lookup map.

        Remote providers are registered first (in the order given); Ollama
        registers last and wins on collision UNLESS the model is explicitly
        whitelisted for a remote provider (the user chose that provider).
        """
        async with self._lock:
            self._models.clear()
            whitelists = self._remote_whitelists()

            ollama_models: list[ModelInfo] = []
            try:
                ollama_models = await ollama_provider.list_models()
            except Exception as e:
                logger.warning("Failed to fetch Ollama models for registry: %s", e)

            for provider in remote_providers:
                if not getattr(provider, "available", False):
                    continue
                try:
                    remote_models = await provider.list_models()
                except Exception as e:
                    logger.warning("Failed to fetch %s models for registry: %s", provider.name, e)
                    continue
                whitelist = whitelists.get(provider.name, set())
                for m in remote_models:
                    # A model whitelisted for THIS provider always wins the
                    # slot; otherwise first remote to claim it keeps it.
                    if m.id in self._models and m.id not in whitelist:
                        continue
                    self._models[m.id] = m

            whitelisted_anywhere = set().union(*whitelists.values()) if whitelists else set()
            for m in ollama_models:
                if m.id in self._models and m.id in whitelisted_anywhere:
                    # User explicitly wants this model from a remote provider
                    continue
                self._models[m.id] = m

            self._populated = True
            by_provider: dict[str, int] = {}
            for m in self._models.values():
                by_provider[m.provider] = by_provider.get(m.provider, 0) + 1
            logger.info(
                "Model registry populated: %d models (%s)",
                len(self._models),
                ", ".join(f"{count} {name}" for name, count in sorted(by_provider.items())),
            )

    def resolve_provider(self, model: str) -> str:
        """Return the provider name ('ollama', 'openrouter', 'openai') for a model.

        Resolution order:
          1. Exact match in registry -> return that provider
          2. Model in a remote whitelist -> that provider (this is how bare
             OpenAI names like 'gpt-4o' route correctly — the '/'-heuristic
             below would misroute them to Ollama)
          3. Registry populated but no match -> legacy heuristic with warning
          4. Registry not populated -> legacy heuristic with warning
        """
        # 1. Exact match
        info = self._models.get(model)
        if info:
            return info.provider

        # 2. Explicit whitelists
        for provider_name, whitelist in self._remote_whitelists().items():
            if model in whitelist:
                return provider_name

        # 3/4. Fallback heuristic
        has_key = bool(os.environ.get("OPENROUTER_API_KEY"))
        if self._populated and model not in _warned_models:
            _warned_models.add(model)
            logger.warning(
                "Model '%s' not found in registry (%d known models), " "falling back to name heuristic",
                model,
                len(self._models),
            )
        result = "openrouter" if ("/" in model and has_key) else "ollama"
        return result

    async def refresh(self, ollama_provider, *remote_providers) -> None:
        """Re-populate from providers. Called after model switch, etc."""
        for provider in remote_providers:
            if hasattr(provider, "clear_models_cache"):
                provider.clear_models_cache()
        await self.populate(ollama_provider, *remote_providers)

    def get_model_info(self, model: str) -> ModelInfo | None:
        """Get cached ModelInfo for a model, or None if unknown."""
        return self._models.get(model)

    def resolve_model_id(self, model: str) -> str:
        """Resolve a possibly bare model name to its full registry ID.

        If the model is already in the registry, returns it as-is.
        Otherwise, searches for a model whose ID ends with '/<model>'
        (e.g. 'grok-2' -> 'x-ai/grok-2'). Returns the first match,
        preferring shorter IDs (more specific). Returns the original
        string if no match is found.
        """
        if model in self._models:
            return model
        # Try suffix match: look for "<provider>/<model>"
        candidates = [mid for mid in self._models if mid.endswith(f"/{model}")]
        if candidates:
            # Prefer the shortest (most specific) match
            best = min(candidates, key=len)
            logger.info("Resolved bare model name '%s' -> '%s'", model, best)
            return best
        return model

    def all_models(self) -> list[ModelInfo]:
        """Return all known models."""
        return list(self._models.values())
