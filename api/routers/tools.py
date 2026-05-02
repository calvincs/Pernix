"""Pernix — Tool listing, discovery, and health endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from core.tools.registry import get_registry

router = APIRouter(tags=["tools"])


@router.get("/api/tools")
async def list_tools():
    from core.signals import from_row
    from db import models as db

    registry = get_registry()
    all_tools = list(registry.all_tools())

    # Batch-load performance counters
    tool_names = [t.name for t in all_tools]
    perf_rows = db.get_signals_by_subjects([("tool", n) for n in tool_names])
    perf_map = {r["subject"]: from_row(r).to_display() for r in perf_rows}

    tools = []
    for t in all_tools:
        tools.append(
            {
                "name": t.name,
                "description": t.description,
                "category": t.category,
                "tags": t.tags,
                "enabled": not registry.is_disabled(t.name),
                "source": t.source,
                "timeout": t.timeout,
                "parallel_safe": t.parallel_safe,
                "safety_level": t.safety_level,
                "performance": perf_map.get(t.name),  # None if no observations yet
            }
        )
    return {"tools": tools, "count": len(tools)}


@router.get("/api/tools/health")
async def tool_health():
    registry = get_registry()
    return {"tools": registry.get_health_report()}


@router.post("/api/tools/toggle")
async def toggle_tool(body: dict):
    name = body.get("name", "")
    enabled = body.get("enabled", True)
    registry = get_registry()
    if not registry.exists(name):
        return {"error": f"Tool '{name}' not found"}
    if enabled:
        registry.enable(name)
    else:
        registry.disable(name)
    return {"name": name, "enabled": enabled}


@router.post("/api/tools/set-safety")
async def set_tool_safety(body: dict):
    name = body.get("name", "")
    level = body.get("safety_level", "")
    registry = get_registry()
    if not registry.exists(name):
        return {"error": f"Tool '{name}' not found"}
    try:
        registry.set_safety_level(name, level)
    except (ValueError, KeyError) as e:
        return {"error": str(e)}
    return {"name": name, "safety_level": level}
