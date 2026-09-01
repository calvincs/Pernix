"""Pernix — MCP extension: connect external MCP servers, use their tools.

MCP (Model Context Protocol) servers expose tools over stdio or Streamable
HTTP. The manager (core/extensions/mcp/manager.py) connects each server
configured in data/mcp_servers.json and registers its tools in the
ToolRegistry as mcp_<server>_<tool> with source="mcp" — scout curation, the
dangerous-tool gate, health metrics and post-mortems apply unchanged.

register() is a hard off-switch at startup (restart to add/remove the
management tools); every call path re-checks mcp_enabled at call time so a
hot toggle-off degrades to a clear error, never a run.
"""

from __future__ import annotations

import asyncio
import concurrent.futures as _futures
import json
import logging

from config import settings
from core.tools.registry import ToolRegistry

logger = logging.getLogger("pernix.ext.mcp")

_MANAGE_TIMEOUT = 15  # seconds for list/remove
_CONNECT_MARGIN = 20  # on top of mcp_connect_timeout for add/reload


def _manager_or_error():
    from core.extensions.mcp.manager import get_mcp_manager_if_started

    if not settings.mcp_enabled:
        return None, "Error: MCP is disabled. Enable it in Settings → MCP Servers (no restart needed)."
    manager = get_mcp_manager_if_started()
    if manager is None:
        return None, "Error: the MCP manager is not running (it starts with the server when mcp_enabled is on)."
    return manager, None


def _run_on_loop(coro, _context: dict | None, timeout: float):
    """Bridge a manager coroutine from the tool thread to the main loop."""
    manager, err = _manager_or_error()
    if err:
        coro.close()
        raise RuntimeError(err)
    loop = (_context or {}).get("_loop") or manager._loop
    if loop is None or not loop.is_running():
        coro.close()
        raise RuntimeError("Error: MCP requires the event loop context. Internal error.")
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        return fut.result(timeout=timeout)
    except _futures.TimeoutError:
        fut.cancel()
        raise RuntimeError(f"Error: MCP operation timed out after {int(timeout)}s") from None


def _fmt_server_line(snap: dict) -> str:
    status = snap["status"]
    line = f"- {snap['name']} [{snap['transport']}] {status}"
    if snap.get("server_info"):
        line += f" ({snap['server_info']})"
    line += f" — {snap['tool_count']} tools, safety={snap['safety']}"
    if snap.get("error") and status in ("degraded", "disabled", "stopped"):
        line += f"\n    last error: {snap['error'][:300]}"
    if snap["tools"]:
        line += "\n    " + ", ".join(snap["tools"][:20])
        if len(snap["tools"]) > 20:
            line += f", … +{len(snap['tools']) - 20} more"
    return line


def mcp_list_servers(_context: dict | None = None) -> str:
    """List configured MCP servers with live status and their tools."""
    manager, err = _manager_or_error()
    if err:
        return err
    snaps = manager.status_snapshot()
    if not snaps:
        return (
            "No MCP servers configured. Add one with mcp_add_server, via the Explorer → MCP tab, "
            "or by editing data/mcp_servers.json (standard mcpServers format)."
        )
    return "MCP servers:\n" + "\n".join(_fmt_server_line(s) for s in snaps)


def mcp_add_server(name: str, config: dict, _context: dict | None = None) -> str:
    """Add (or replace) an MCP server and connect to it."""
    from core.extensions.mcp.config import parse_server_entry

    if isinstance(config, str):
        # Models sometimes send the object as a JSON string — accept it.
        try:
            config = json.loads(config)
        except json.JSONDecodeError as e:
            return f"Error: config must be a JSON object (parse failed: {e})"
    try:
        cfg = parse_server_entry(str(name), config)
    except ValueError as e:
        return f"Error: {e}"
    if cfg.transport == "stdio" and not settings.mcp_stdio_enabled:
        return (
            "Error: stdio MCP servers are disabled (Settings → MCP Servers → Allow stdio servers). "
            "Remote (url-based) servers are still allowed."
        )

    from core.extensions.mcp.manager import MCPUnavailable

    manager, err = _manager_or_error()
    if err:
        return err
    try:
        conn = _run_on_loop(manager.add_server(cfg), _context, settings.mcp_connect_timeout + _CONNECT_MARGIN)
    except MCPUnavailable as e:
        return (
            f"Saved server '{cfg.name}' to data/mcp_servers.json, but the first connection failed: {e}\n"
            "It will keep retrying with backoff; fix the config in the MCP tab or with mcp_add_server "
            "again, or check data/logs/mcp_" + cfg.name + ".stderr.log for stdio servers."
        )
    except (RuntimeError, ValueError) as e:
        return str(e) if str(e).startswith("Error:") else f"Error: {e}"
    snap = conn.snapshot()
    tools = ", ".join(snap["tools"]) or "(none)"
    return (
        f"MCP server '{cfg.name}' connected ({snap['server_info'] or cfg.transport}) with "
        f"{snap['tool_count']} tool(s) at safety '{snap['safety']}':\n{tools}\n"
        "They are registered and discoverable now (mcp_<server>_<tool>)."
    )


def mcp_remove_server(name: str, _context: dict | None = None) -> str:
    """Remove an MCP server: disconnect, unregister its tools, delete config."""
    manager, err = _manager_or_error()
    if err:
        return err
    try:
        removed = _run_on_loop(manager.remove_server(str(name)), _context, _MANAGE_TIMEOUT)
    except RuntimeError as e:
        return str(e) if str(e).startswith("Error:") else f"Error: {e}"
    if not removed:
        return f"Error: no MCP server named '{name}'. Use mcp_list_servers to see what exists."
    return f"MCP server '{name}' removed; its tools are unregistered and its config entry deleted."


def mcp_reload_server(name: str, _context: dict | None = None) -> str:
    """Reconnect an MCP server (re-reads its config entry from disk)."""
    from core.extensions.mcp.manager import MCPUnavailable

    manager, err = _manager_or_error()
    if err:
        return err
    try:
        conn = _run_on_loop(manager.reload_server(str(name)), _context, settings.mcp_connect_timeout + _CONNECT_MARGIN)
    except MCPUnavailable as e:
        return f"Error: reload of '{name}' failed: {e}"
    except KeyError:
        return f"Error: no MCP server named '{name}'. Use mcp_list_servers to see what exists."
    except (RuntimeError, ValueError) as e:
        return str(e) if str(e).startswith("Error:") else f"Error: {e}"
    snap = conn.snapshot()
    return (
        f"MCP server '{name}' reloaded ({snap['server_info'] or snap['transport']}): "
        f"{snap['tool_count']} tool(s) registered."
    )


def register(reg: ToolRegistry) -> None:
    if not settings.mcp_enabled:
        logger.info("MCP extension disabled (mcp_enabled=false); management tools not registered")
        return

    reg.register(
        name="mcp_list_servers",
        func=mcp_list_servers,
        description=(
            "List configured MCP (Model Context Protocol) servers: connection status, discovered "
            "tools, safety level, and last error. MCP servers plug external tool providers into "
            "this system; their tools appear as mcp_<server>_<tool>."
        ),
        parameters={"type": "object", "properties": {}},
        category="mcp",
        tags=["mcp", "server", "integration", "list", "status", "external"],
        timeout=30,
        parallel_safe=True,
        source="extension",
        safety_level="safe",
    )
    reg.register(
        name="mcp_add_server",
        func=mcp_add_server,
        description=(
            "Add or replace an MCP server and connect to it. Accepts the ecosystem-standard "
            'mcpServers entry shape: {"command": "npx", "args": [...], "env": {...}} for a local '
            'stdio server, or {"url": "https://...", "headers": {...}} for a remote one. Optional '
            "keys: type (stdio|http|sse), enabled, safety (safe|caution|dangerous), timeout "
            '(seconds), tool_allowlist. Secrets go in .env and are referenced as "${VAR}" — never '
            "pasted literally. On success the server's tools are registered immediately."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Short alias, 1-16 chars [a-z0-9_], becomes the tool prefix mcp_<name>_*",
                },
                "config": {
                    "type": "object",
                    "description": "The server entry (standard mcpServers shape; see tool description)",
                    "additionalProperties": True,
                },
            },
            "required": ["name", "config"],
        },
        category="mcp",
        tags=["mcp", "server", "integration", "add", "install", "connect", "external"],
        timeout=90,
        source="extension",
        safety_level="dangerous",  # config mutation + (stdio) supply chain — always confirm
    )
    reg.register(
        name="mcp_remove_server",
        func=mcp_remove_server,
        description="Remove an MCP server: disconnect it, unregister its tools, delete its config entry.",
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Server alias to remove"}},
            "required": ["name"],
        },
        category="mcp",
        tags=["mcp", "server", "integration", "remove", "delete", "disconnect"],
        timeout=30,
        source="extension",
        safety_level="dangerous",
    )
    reg.register(
        name="mcp_reload_server",
        func=mcp_reload_server,
        description=(
            "Reconnect an MCP server and re-discover its tools (re-reads its entry from "
            "data/mcp_servers.json, so hand edits are picked up). Use when a server is degraded "
            "or its tool list looks stale."
        ),
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Server alias to reload"}},
            "required": ["name"],
        },
        category="mcp",
        tags=["mcp", "server", "integration", "reload", "reconnect", "refresh"],
        timeout=90,
        source="extension",
        safety_level="caution",
    )
