"""Pernix — the sync side of MCP: tool wrappers and result formatting.

Registered MCP tools are plain sync callables (the registry contract). Each
wrapper runs on the executor's tool thread, marshals the call onto the main
loop via run_coroutine_threadsafe, and formats the CallToolResult back into
the registry's (str, metadata) shape. Formatting happens on the tool thread
on purpose: execute_sync has already scoped WORKSPACE_OVERRIDE there, so
saved blobs land in the calling session's workspace.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import concurrent.futures as _futures
import json
import logging
import re
import time

from config import settings

logger = logging.getLogger("pernix.ext.mcp")

# Inline cap for embedded text resources riding along in a tool result — the
# main text content is uncapped (truncation + kernel binding handle size).
_EMBEDDED_TEXT_CAP = 4000

_MIME_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/svg+xml": "svg",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/mpeg": "mp3",
    "audio/ogg": "ogg",
    "application/pdf": "pdf",
}


def make_tool_fn(manager, server: str, remote_name: str):
    """Build the sync registry function for one remote tool."""

    def _mcp_tool(_context: dict | None = None, **arguments) -> str | tuple[str, dict]:
        return call_mcp_tool_sync(manager, server, remote_name, arguments, _context)

    _mcp_tool.__name__ = f"mcp_{server}_{remote_name}"
    _mcp_tool.__doc__ = f"MCP tool '{remote_name}' on server '{server}'."
    return _mcp_tool


def call_mcp_tool_sync(
    manager, server: str, remote_name: str, arguments: dict, _context: dict | None
) -> str | tuple[str, dict]:
    if not settings.mcp_enabled:
        return "Error: MCP is disabled (Settings → MCP Servers). No call was made."
    conn = manager.connections.get(server)
    if conn is None:
        if not manager.started:
            # shutdown() drops its connections; the tools stay registered so
            # a restart lands them on the same names.
            return "Error: the MCP manager is stopped (shutting down, or MCP was disabled). No call was made."
        return f"Error: MCP server '{server}' is no longer configured; this tool is stale."
    loop = (_context or {}).get("_loop") or manager._loop
    if loop is None or not loop.is_running():
        return "Error: MCP requires the event loop context. Internal error."

    from core.extensions.mcp.manager import MCPUnavailable

    try:
        fut = asyncio.run_coroutine_threadsafe(conn.call_tool(remote_name, arguments), loop)
    except RuntimeError as e:
        return f"Error: MCP call could not be scheduled (loop not running): {e}"
    wait = conn.call_timeout + 10
    try:
        result = fut.result(timeout=wait)
    except _futures.TimeoutError:
        fut.cancel()
        return (
            f"Error: MCP tool '{remote_name}' on server '{server}' timed out after {wait}s. "
            "The server may be overloaded; retry, or raise this server's timeout in its config."
        )
    except _futures.CancelledError:
        return f"Error: MCP call '{remote_name}' was cancelled."
    except MCPUnavailable as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error: MCP tool '{remote_name}' on server '{server}' failed: {e}"
    return format_call_result(result, server=server, remote_name=remote_name)


def build_description(server: str, tool) -> str:
    """[MCP:server]-prefixed, length-capped description. Server text is
    untrusted input headed for the system prompt — cap it and strip control
    characters, but don't editorialize."""
    desc = (getattr(tool, "description", "") or getattr(tool, "title", "") or "").strip()
    if not desc:
        desc = f"Tool '{tool.name}' from MCP server '{server}' (no description provided)."
    desc = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", desc)
    prefix = f"[MCP:{server}] "
    cap = max(200, settings.mcp_max_description_chars)
    if len(prefix) + len(desc) > cap:
        desc = desc[: cap - len(prefix) - 1].rstrip() + "…"
    return prefix + desc


def format_call_result(result, *, server: str, remote_name: str) -> str | tuple[str, dict]:
    """CallToolResult → (text, metadata) in registry shape."""
    from mcp import types as t

    meta: dict = {"mcp_server": server, "mcp_tool": remote_name}

    if isinstance(result, t.InputRequiredResult):
        try:
            detail = json.dumps(result.model_dump(exclude_none=True), default=str)[:600]
        except Exception:
            detail = "(unrenderable input request)"
        return (
            f"Error: MCP tool '{remote_name}' needs additional interactive input before it can run "
            f"(multi round-trip request): {detail}. Pernix does not yet relay interactive input to "
            "MCP servers — if possible, supply the missing information as tool arguments instead.",
            meta,
        )
    if not isinstance(result, t.CallToolResult):
        try:
            return json.dumps(result.model_dump(exclude_none=True), indent=2, default=str), meta
        except Exception:
            return str(result), meta

    parts: list[str] = []
    for i, block in enumerate(result.content or []):
        if isinstance(block, t.TextContent):
            if block.text:
                parts.append(block.text)
        elif isinstance(block, (t.ImageContent, t.AudioContent)):
            kind = "image" if isinstance(block, t.ImageContent) else "audio"
            path = _save_blob(server, remote_name, i, getattr(block, "data", ""), getattr(block, "mime_type", ""))
            if path:
                hint = " Use view_image to look at it." if kind == "image" else ""
                parts.append(f"[{kind} saved to {path}]{hint}")
            else:
                parts.append(f"[{kind} content could not be decoded]")
        elif isinstance(block, t.ResourceLink):
            label = getattr(block, "name", "") or ""
            desc = getattr(block, "description", "") or ""
            line = f"[resource] {block.uri}"
            if label:
                line += f" — {label}"
            if desc:
                line += f": {desc[:200]}"
            parts.append(line)
        elif isinstance(block, t.EmbeddedResource):
            parts.append(_render_embedded(server, remote_name, i, block))
        else:
            parts.append(f"[unsupported content block: {getattr(block, 'type', type(block).__name__)}]")

    if not parts and result.structured_content is not None:
        try:
            parts.append(json.dumps(result.structured_content, indent=2, default=str))
        except Exception:
            parts.append(str(result.structured_content))

    text = "\n\n".join(p for p in parts if p).strip() or "(empty result)"
    if result.is_error:
        return f"Error: MCP tool '{remote_name}' on server '{server}' failed: {text}", meta
    return text, meta


def _render_embedded(server: str, remote_name: str, idx: int, block) -> str:
    resource = getattr(block, "resource", None)
    uri = str(getattr(resource, "uri", "") or "")
    text = getattr(resource, "text", None)
    if isinstance(text, str):
        body = text if len(text) <= _EMBEDDED_TEXT_CAP else text[:_EMBEDDED_TEXT_CAP] + "\n…[truncated]"
        return f"[embedded resource {uri}]\n{body}"
    blob = getattr(resource, "blob", None)
    if isinstance(blob, str):
        path = _save_blob(server, remote_name, idx, blob, getattr(resource, "mime_type", "") or "")
        if path:
            return f"[embedded resource {uri} saved to {path}]"
    return f"[embedded resource {uri}]"


def _save_blob(server: str, remote_name: str, idx: int, data_b64: str, mime: str) -> str | None:
    """Decode a base64 blob into the session workspace; returns the path."""
    if not data_b64:
        return None
    try:
        raw = base64.b64decode(data_b64)
    except (binascii.Error, ValueError):
        return None
    try:
        from core.tools import paths

        out_dir = paths.workspace() / "mcp" / server
        out_dir.mkdir(parents=True, exist_ok=True)
        ext = _MIME_EXT.get((mime or "").lower().split(";")[0], "bin")
        safe = re.sub(r"[^a-z0-9_]+", "_", remote_name.lower())[:40] or "result"
        path = out_dir / f"{safe}_{int(time.time() * 1000)}_{idx}.{ext}"
        path.write_bytes(raw)
        return str(path)
    except Exception as e:
        logger.warning("Could not save MCP blob from '%s': %s", server, e)
        return None
