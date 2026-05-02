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

    async def populate(self, ollama_provider, openrouter_provider) -> None:
        """Fetch model lists from both providers and build the lookup map."""
        async with self._lock:
            self._models.clear()
            openrouter_whitelist = set(settings.openrouter_models or [])

            # Fetch from both providers concurrently
            ollama_models: list[ModelInfo] = []
            openrouter_models: list[ModelInfo] = []

            try:
                ollama_models = await ollama_provider.list_models()
            except Exception as e:
                logger.warning("Failed to fetch Ollama models for registry: %s", e)

            if openrouter_provider.available:
                try:
                    openrouter_models = await openrouter_provider.list_models()
                except Exception as e:
                    logger.warning("Failed to fetch OpenRouter models for registry: %s", e)

            # Register OpenRouter models first
            for m in openrouter_models:
                self._models[m.id] = m

            # Register Ollama models second — they overwrite OpenRouter on collision
            # UNLESS the model is explicitly in the OpenRouter whitelist
            for m in ollama_models:
                if m.id in self._models and m.id in openrouter_whitelist:
                    # User explicitly wants this model from OpenRouter
                    continue
                self._models[m.id] = m

            self._populated = True
            logger.info(
                "Model registry populated: %d models (%d ollama, %d openrouter)",
                len(self._models),
                sum(1 for m in self._models.values() if m.provider == "ollama"),
                sum(1 for m in self._models.values() if m.provider == "openrouter"),
            )

    def resolve_provider(self, model: str) -> str:
        """Return 'ollama' or 'openrouter' for a given model ID.

        Resolution order:
          1. Exact match in registry -> return that provider
          2. Model in settings.openrouter_models whitelist -> 'openrouter'
          3. Registry populated but no match -> legacy heuristic with warning
          4. Registry not populated -> legacy heuristic with warning
        """
        # 1. Exact match
        info = self._models.get(model)
        if info:
            return info.provider

        # 2. Explicit whitelist
        if model in (settings.openrouter_models or []):
            return "openrouter"

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

    async def refresh(self, ollama_provider, openrouter_provider) -> None:
        """Re-populate from providers. Called after model switch, etc."""
        if hasattr(openrouter_provider, "clear_models_cache"):
            openrouter_provider.clear_models_cache()
        await self.populate(ollama_provider, openrouter_provider)

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
