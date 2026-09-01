"""Stub MCP server for tests — run as a stdio subprocess.

Usage: python -m tests.fixtures.mcp_stub  (cwd must be the repo root, or
invoke by path). Tools cover the shapes the bridge must handle: plain text,
slow, failing, huge output, image content, and a destructive-annotated tool.
"""

from __future__ import annotations

import base64
import time

from mcp.server.mcpserver import MCPServer
from mcp.types import ImageContent, ToolAnnotations

server = MCPServer(name="pernix-stub", version="0.1")

# 1x1 transparent PNG
_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="


@server.tool(description="Echo the given text back.")
def echo(text: str) -> str:
    return f"echo: {text}"


@server.tool(description="Add two integers.")
def add(a: int, b: int) -> str:
    return str(a + b)


@server.tool(description="Sleep for `seconds` then return.")
def slow(seconds: float) -> str:
    time.sleep(seconds)
    return f"slept {seconds}s"


@server.tool(description="Always raises an error.")
def boom() -> str:
    raise RuntimeError("stub exploded on purpose")


@server.tool(description="Return `chars` characters of output.")
def big(chars: int) -> str:
    return "x" * int(chars)


@server.tool(description="Return a tiny PNG image.")
def picture() -> ImageContent:
    return ImageContent(type="image", data=_PNG_B64, mime_type="image/png")


@server.tool(
    description="Pretends to delete everything.",
    annotations=ToolAnnotations(destructive_hint=True),
)
def wipe(target: str) -> str:
    return f"pretended to wipe {target}"


if __name__ == "__main__":
    server.run("stdio")
