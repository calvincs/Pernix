"""Pernix — Model listing, switching, and validation endpoints."""

from __future__ import annotations

import asyncio
import os
import time

from fastapi import APIRouter, Query

from config import settings

router = APIRouter(tags=["models"])


@router.get("/api/models")
async def list_models():
    from core.llm.client import get_llm_client

    client = get_llm_client()
    try:
        models = await client.list_models()
        return {
            "models": [
                {
                    "id": m.id,
                    "provider": m.provider,
                    "context_length": m.context_length,
                    "supports_vision": m.supports_vision,
                    "supports_tools": m.supports_tools,
                }
                for m in models
            ],
            "current": settings.llm_model,
        }
    except Exception as e:
        return {"models": [], "current": settings.llm_model, "error": str(e)}


# The OpenRouter catalog, shared by every /api/models/validate call. The
# settings modal validates each curated model on open — a dozen requests
# that each used to download the entire catalog (megabytes, ~1s apiece,
# serialized behind one another), which is most of the delay in opening
# settings. One fetch per TTL covers the whole modal.
_CATALOG_TTL_S = 300.0
_catalog: dict[str, dict] | None = None
_catalog_fetched_at = 0.0
_catalog_lock = asyncio.Lock()


async def _openrouter_catalog(api_key: str) -> dict[str, dict]:
    """Model-id → catalog entry, cached for _CATALOG_TTL_S. Raises on fetch failure."""
    global _catalog, _catalog_fetched_at

    now = time.monotonic()
    if _catalog is not None and now - _catalog_fetched_at < _CATALOG_TTL_S:
        return _catalog

    import httpx

    # Single-flight: the modal fires its validations concurrently, and
    # without the lock every one of them would miss the cache together and
    # fetch the catalog in parallel — the exact stampede this replaces.
    async with _catalog_lock:
        now = time.monotonic()
        if _catalog is not None and now - _catalog_fetched_at < _CATALOG_TTL_S:
            return _catalog
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://openrouter.ai/api/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if resp.status_code != 200:
                raise RuntimeError(f"OpenRouter API returned {resp.status_code}")
            entries = resp.json().get("data", [])
        _catalog = {m.get("id", ""): m for m in entries if m.get("id")}
        _catalog_fetched_at = time.monotonic()
        return _catalog


@router.get("/api/models/validate")
async def validate_model(model: str = Query(...)):
    """Check if an OpenRouter model exists and return its info."""
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        return {"valid": False, "error": "OPENROUTER_API_KEY not set"}

    try:
        catalog = await _openrouter_catalog(api_key)
    except Exception as e:
        return {"valid": False, "error": str(e)}

    entry = catalog.get(model)
    if entry is None:
        return {"valid": False, "error": "Model not found on OpenRouter"}

    pricing = entry.get("pricing", {})
    return {
        "valid": True,
        "name": entry.get("name", model),
        "context_length": entry.get("context_length", 0),
        "prompt_cost": pricing.get("prompt", "0"),
        "completion_cost": pricing.get("completion", "0"),
    }


@router.get("/api/models/ollama")
async def list_ollama_models():
    """List models available on the configured Ollama server."""
    import httpx

    base = settings.llm_base_url.replace("/v1", "")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{base}/api/tags")
            resp.raise_for_status()
            data = resp.json()
        models = []
        for m in data.get("models", []):
            models.append(
                {
                    "name": m.get("name", ""),
                    "size": m.get("size", 0),
                    "modified_at": m.get("modified_at", ""),
                    "family": m.get("details", {}).get("family", ""),
                    "parameter_size": m.get("details", {}).get("parameter_size", ""),
                    "quantization": m.get("details", {}).get("quantization_level", ""),
                }
            )
        return {"models": models}
    except Exception as e:
        return {"models": [], "error": str(e)}


@router.post("/api/models/switch")
async def switch_model(body: dict):
    model = body.get("model", "")
    if not model:
        return {"error": "model required"}

    from core.llm.client import get_llm_client

    client = get_llm_client()

    # Derived for the response only — the budget itself is computed
    # per-session at turn start (core/llm/budget.derive_model_budget), so
    # switching the global model must NOT mutate settings.context_budget:
    # that value is the manual fallback, and clobbering it here silently
    # rewrote user configuration on every switch (and diverged from the
    # session-scoped switch_model tool).
    try:
        info = await client.get_model_info(model)
        new_budget = int(info.context_length * 0.9)
    except Exception:
        new_budget = settings.context_budget

    old_model = settings.llm_model
    settings.llm_model = model
    settings.save()

    # Refresh model registry so the new model is properly indexed
    try:
        await client.refresh_registry()
    except Exception:
        pass  # Non-critical — registry will use heuristic until next refresh

    # Persist preference
    from pathlib import Path

    Path("data/model_pref.txt").write_text(model)

    return {
        "switched": True,
        "from": old_model,
        "to": model,
        "context_budget": new_budget,
    }
