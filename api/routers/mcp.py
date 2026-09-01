"""MCP server management endpoints.

Config CRUD works even while mcp_enabled=false (it edits
data/mcp_servers.json directly — the file is the source of truth); live
operations (connect, reload, test) need the running manager and return 409
without it. The paste-import path accepts the ecosystem-standard
{"mcpServers": {...}} blob so configs copied from Claude Code / Cursor /
VS Code work verbatim.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException

from config import settings
from core.extensions.mcp.config import (
    load_server_configs,
    parse_server_entry,
    save_server_configs,
)

logger = logging.getLogger("pernix.api")

router = APIRouter()


def _manager():
    from core.extensions.mcp.manager import get_mcp_manager_if_started

    return get_mcp_manager_if_started()


def _require_manager():
    mgr = _manager()
    if mgr is None:
        raise HTTPException(
            status_code=409,
            detail="MCP manager is not running — enable mcp_enabled in Settings first",
        )
    return mgr


@router.get("/api/mcp/servers")
async def list_mcp_servers():
    """Configured servers merged with live status (when the manager runs)."""
    mgr = _manager()
    if mgr is not None:
        servers = mgr.status_snapshot()
    else:
        servers = [
            {
                "name": cfg.name,
                "transport": cfg.transport,
                "target": cfg.command if cfg.transport == "stdio" else cfg.url,
                "enabled": cfg.enabled,
                "status": "offline",
                "error": "",
                "server_info": "",
                "tool_count": 0,
                "tools": [],
                "safety": cfg.safety or settings.mcp_default_safety,
                "connected_at": 0,
                "last_used": 0,
            }
            for cfg in sorted(load_server_configs().values(), key=lambda c: c.name)
        ]
    configs = {name: cfg.to_dict() for name, cfg in load_server_configs().items()}
    return {
        "mcp_enabled": settings.mcp_enabled,
        "stdio_enabled": settings.mcp_stdio_enabled,
        "servers": servers,
        "configs": configs,
        "max_servers": settings.mcp_max_servers,
    }


def _parse_body_entries(body: dict) -> dict:
    """Accept {"name","config"} or a pasted {"mcpServers": {...}} blob.
    Validates every entry before anything is applied."""
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    raw_entries: dict = {}
    if "mcpServers" in body and isinstance(body["mcpServers"], dict):
        raw_entries = body["mcpServers"]
    elif "name" in body:
        cfg_obj = body.get("config")
        if not isinstance(cfg_obj, dict):
            raise HTTPException(status_code=400, detail="'config' must be an object")
        raw_entries = {str(body["name"]): cfg_obj}
    else:
        raise HTTPException(
            status_code=400,
            detail='Provide {"name": ..., "config": {...}} or a pasted {"mcpServers": {...}} blob',
        )
    parsed = {}
    for name, raw in raw_entries.items():
        try:
            parsed[str(name)] = parse_server_entry(str(name), raw)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
    if not parsed:
        raise HTTPException(status_code=400, detail="No servers in request")
    for cfg in parsed.values():
        if cfg.transport == "stdio" and not settings.mcp_stdio_enabled:
            raise HTTPException(
                status_code=400,
                detail=f"Server '{cfg.name}': stdio servers are disabled (mcp_stdio_enabled)",
            )
    return parsed


@router.post("/api/mcp/servers")
async def add_mcp_servers(body: dict):
    """Add or update one server, or import a pasted mcpServers blob."""
    parsed = _parse_body_entries(body)
    existing = load_server_configs()
    new_names = set(parsed) - set(existing)
    if len(existing) + len(new_names) > settings.mcp_max_servers:
        raise HTTPException(
            status_code=400,
            detail=f"Server cap reached ({settings.mcp_max_servers}); remove one or raise mcp_max_servers",
        )

    mgr = _manager()
    results = {}
    if mgr is None:
        # Manager not running: persist only. Entries connect on next start.
        existing.update(parsed)
        save_server_configs(existing)
        results = {name: {"ok": True, "status": "saved (manager offline)"} for name in parsed}
    else:
        from core.extensions.mcp.manager import MCPUnavailable

        for name, cfg in sorted(parsed.items()):
            try:
                conn = await asyncio.wait_for(mgr.add_server(cfg), timeout=settings.mcp_connect_timeout + 15)
                snap = conn.snapshot()
                results[name] = {"ok": True, "status": snap["status"], "tools": snap["tools"]}
            except (MCPUnavailable, asyncio.TimeoutError) as e:
                results[name] = {
                    "ok": False,
                    "status": "saved, but first connect failed (retrying with backoff)",
                    "error": str(e) or "connect timeout",
                }
            except ValueError as e:
                results[name] = {"ok": False, "status": "rejected", "error": str(e)}
    return {"results": results}


@router.delete("/api/mcp/servers/{name}")
async def delete_mcp_server(name: str):
    mgr = _manager()
    if mgr is not None:
        removed = await mgr.remove_server(name)
    else:
        configs = load_server_configs()
        removed = configs.pop(name, None) is not None
        if removed:
            save_server_configs(configs)
    if not removed:
        raise HTTPException(status_code=404, detail=f"No MCP server named '{name}'")
    return {"removed": name}


@router.post("/api/mcp/servers/{name}/toggle")
async def toggle_mcp_server(name: str, body: dict):
    enabled = bool(body.get("enabled", True))
    mgr = _manager()
    if mgr is not None:
        try:
            conn = await mgr.toggle_server(name, enabled)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"No MCP server named '{name}'") from None
        return {"name": name, "enabled": enabled, "status": conn.status}
    configs = load_server_configs()
    if name not in configs:
        raise HTTPException(status_code=404, detail=f"No MCP server named '{name}'")
    configs[name].enabled = enabled
    save_server_configs(configs)
    return {"name": name, "enabled": enabled, "status": "offline"}


@router.post("/api/mcp/servers/{name}/reload")
async def reload_mcp_server(name: str):
    mgr = _require_manager()
    from core.extensions.mcp.manager import MCPUnavailable

    try:
        conn = await asyncio.wait_for(mgr.reload_server(name), timeout=settings.mcp_connect_timeout + 15)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No MCP server named '{name}'") from None
    except (MCPUnavailable, ValueError, asyncio.TimeoutError) as e:
        raise HTTPException(status_code=502, detail=str(e) or "reload timed out") from None
    snap = conn.snapshot()
    return {"name": name, "status": snap["status"], "tools": snap["tools"]}


@router.post("/api/mcp/test")
async def test_mcp_server(body: dict):
    """Dry-run connect: nothing is saved or registered."""
    parsed = _parse_body_entries(body)
    if len(parsed) != 1:
        raise HTTPException(status_code=400, detail="Test one server at a time")
    _require_manager()  # needs mcp_enabled (stdio gate already checked in parsing)
    from core.extensions.mcp.manager import probe_server

    cfg = next(iter(parsed.values()))
    try:
        return await asyncio.wait_for(probe_server(cfg), timeout=settings.mcp_connect_timeout + 10)
    except asyncio.TimeoutError:
        return {"ok": False, "error": f"connect timed out after {settings.mcp_connect_timeout + 10}s"}
    except Exception as e:
        return {"ok": False, "error": str(e) or type(e).__name__}
