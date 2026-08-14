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
import time

from config import settings

logger = logging.getLogger("pernix.llm.budget")

# Models whose registry miss has already been reported, so the per-round
# derivation calls don't repeat the same warning every tool round.
_warned_unknown: set[str] = set()

# Last refresh attempt per model (monotonic seconds). Rate limits the lazy
# repopulate below so a typo'd or genuinely absent model name cannot storm
# the provider list endpoints once per turn.
_refresh_attempts: dict[str, float] = {}
_REFRESH_COOLDOWN_S = 60.0


async def ensure_model_known(model: str) -> bool:
    """Refresh the model registry once when `model` is missing from it.

    The registry is populated at startup, so a model pulled onto the Ollama
    host afterwards is invisible to it: derivation falls back to the manual
    settings.context_budget, which can be small enough that the compiler
    cannot fit a turn at all (ContextBudgetError, every turn, until
    restart). Refreshing on the miss turns that into a one-off catalog
    fetch. Rate limited per model; never raises.

    Returns True when the model is in the registry afterwards.
    """
    if not settings.context_auto or not model:
        return False
    try:
        from core.llm.client import get_llm_client

        client = get_llm_client()
        registry = client.router.registry
        # An unpopulated registry means startup population hasn't run (or
        # failed) — that's not this function's job to paper over.
        if not registry.populated:
            return False
        if registry.get_model_info(registry.resolve_model_id(model)) is not None:
            return True

        now = time.monotonic()
        last = _refresh_attempts.get(model)
        if last is not None and now - last < _REFRESH_COOLDOWN_S:
            return False
        _refresh_attempts[model] = now

        logger.info("Model '%s' unknown to the registry — refreshing from providers", model)
        await client.refresh_registry()
        if registry.get_model_info(registry.resolve_model_id(model)) is None:
            logger.warning(
                "Model '%s' still unknown after a registry refresh; context budget falls back to "
                "the manual settings.context_budget (%d) and output to settings.max_tokens (%d)",
                model,
                settings.context_budget,
                settings.max_tokens,
            )
            return False
        _warned_unknown.discard(model)
        return True
    except Exception as e:
        logger.debug("Registry refresh for %s failed: %s", model, e)
        return False


def derive_model_budget(model: str) -> int | None:
    """Registry-derived context budget (90% of the model window), or None.

    Returns None when context_auto is off, the model is unknown to the
    registry, or the reported window is absurdly small — callers then fall
    back to settings.context_budget. Catalog lookup only, no network; call
    `ensure_model_known()` first from async callers that can afford a
    refresh.
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
        # Silent fallback to a manual budget is how an unknown model turns
        # into an unexplained ContextBudgetError — say so, once per model.
        if info is None and model not in _warned_unknown:
            _warned_unknown.add(model)
            logger.warning(
                "Model '%s' is not in the model registry — using the manual context budget "
                "fallback (settings.context_budget=%d) instead of the model's real window",
                model,
                settings.context_budget,
            )
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
