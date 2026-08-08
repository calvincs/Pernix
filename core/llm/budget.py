"""Pernix — Effective context/output budget derivation.

Single source of truth for "how much context does the active model really
have" and "how many output tokens should we request". Both derive from the
model registry — which is populated from live provider metadata (Ollama
/api/show, OpenRouter /models) — when settings.context_auto is on.
settings.context_budget / settings.max_tokens act as the manual override
(context_auto off) and as fallbacks when the registry doesn't know the
model. Shared by the agent loop and the context introspection endpoint so
the status bar and the agent always agree on the budget.
"""

from __future__ import annotations

import logging

from config import settings

logger = logging.getLogger("pernix.llm.budget")


def derive_model_budget(model: str) -> int | None:
    """Registry-derived context budget (90% of the model window), or None.

    Returns None when context_auto is off, the model is unknown to the
    registry, or the reported window is absurdly small — callers then fall
    back to settings.context_budget. Catalog lookup only, no network.
    """
    if not settings.context_auto:
        return None
    try:
        from core.llm.client import get_llm_client

        registry = get_llm_client().router.registry
        info = registry.get_model_info(registry.resolve_model_id(model))
        ctx = int(getattr(info, "context_length", 0) or 0) if info else 0
        # No artificial floor: a floor above a small model's real window
        # (e.g. 8K) would stop the compiler trimming and overflow the model.
        if ctx >= 2048:
            return int(ctx * 0.9)
    except Exception as e:
        logger.debug("Model context budget lookup failed for %s: %s", model, e)
    return None


def derive_max_output(model: str) -> int:
    """Effective output-token request for a model.

    settings.max_tokens is the ceiling; when the registry reports a smaller
    provider completion cap (OpenRouter top_provider.max_completion_tokens),
    use that — so the compiler's output reservation matches what the
    provider will actually generate instead of over-reserving history space
    for tokens that can never come back.
    """
    if not settings.context_auto:
        return settings.max_tokens
    try:
        from core.llm.client import get_llm_client

        registry = get_llm_client().router.registry
        info = registry.get_model_info(registry.resolve_model_id(model))
        cap = int(getattr(info, "max_output_tokens", 0) or 0) if info else 0
        if cap > 0:
            return min(settings.max_tokens, cap)
    except Exception as e:
        logger.debug("Max output lookup failed for %s: %s", model, e)
    return settings.max_tokens
