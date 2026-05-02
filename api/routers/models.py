"""Pernix — Model listing, switching, and validation endpoints."""

from __future__ import annotations

import os

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


@router.get("/api/models/validate")
async def validate_model(model: str = Query(...)):
    """Check if an OpenRouter model exists and return its info."""
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        return {"valid": False, "error": "OPENROUTER_API_KEY not set"}

    import httpx

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                "https://openrouter.ai/api/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if resp.status_code != 200:
                return {"valid": False, "error": f"OpenRouter API returned {resp.status_code}"}

            data = resp.json()
            models = data.get("data", [])
            for m in models:
                if m.get("id") == model:
                    pricing = m.get("pricing", {})
                    return {
                        "valid": True,
                        "name": m.get("name", model),
                        "context_length": m.get("context_length", 0),
                        "prompt_cost": pricing.get("prompt", "0"),
                        "completion_cost": pricing.get("completion", "0"),
                    }
            return {"valid": False, "error": "Model not found on OpenRouter"}
    except Exception as e:
        return {"valid": False, "error": str(e)}


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

    # Get model info for context length
    try:
        info = await client.get_model_info(model)
        new_budget = int(info.context_length * 0.9)
    except Exception:
        new_budget = settings.context_budget

    old_model = settings.llm_model
    settings.llm_model = model
    settings.context_budget = new_budget
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
