"""Pernix — Tool discovery: discover_tools, get_tool_schema."""

from __future__ import annotations

import json
import logging

logger = logging.getLogger("pernix.tools.discovery")


def discover_tools(
    query: str,
    category: str = "",
    limit: int = 10,
    _context: dict | None = None,
) -> str:
    """Search for available tools by natural language description.

    Returns tool summaries (name + description + tags), not full schemas.
    Use get_tool_schema(name) to get full parameter details for a specific tool.
    """
    from core.tools.registry import get_registry

    registry = get_registry()

    cat = category if category else None
    results = registry.discover(query, category=cat, limit=limit)

    if not results:
        return f"No tools found matching '{query}'. Try broader terms or different keywords."

    lines = []
    for r in results:
        tags_str = ", ".join(r.tags[:5]) if r.tags else ""
        lines.append(f"- **{r.name}** [{r.category}]: {r.description}")
        if tags_str:
            lines.append(f"  tags: {tags_str}")
    return "\n".join(lines)


def get_tool_schema(name: str, _context: dict | None = None) -> str:
    """Get the full JSON Schema for a tool's parameters.

    Use this after discover_tools to get exact parameter definitions
    before calling a discovered tool.
    """
    from core.tools.registry import get_registry

    registry = get_registry()

    tool = registry.get(name)
    if not tool:
        return f"Error: Tool '{name}' not found. Use discover_tools to search."
    if registry.is_disabled(name):
        return f"Error: Tool '{name}' is disabled. " "Enable it in Explorer > Tools before use."

    schema = tool.to_openai_schema()
    return json.dumps(schema, indent=2)


def register(reg) -> None:
    reg.register(
        name="discover_tools",
        func=discover_tools,
        description="Search for available tools by capability. Returns names and descriptions. Use when you need a tool you don't currently have — web search, git, workers, evaluation, scheduling, etc.",
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What capability you need (e.g. 'search the web', 'parallel workers', 'parse CSV')",
                },
                "category": {
                    "type": "string",
                    "description": "Optional category filter (core, web, vcs, orchestration, scheduling, evaluation, custom, etc.)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 10)",
                },
            },
            "required": ["query"],
        },
        category="core",
        tags=["discover", "find", "search", "tools", "capabilities", "available"],
        timeout=15,
        parallel_safe=True,
    )

    reg.register(
        name="get_tool_schema",
        func=get_tool_schema,
        description="Get the full parameter schema for a specific tool. Use after discover_tools to see exact parameters before calling.",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Exact tool name"},
            },
            "required": ["name"],
        },
        category="core",
        tags=["schema", "parameters", "tool", "details", "usage"],
        timeout=5,
        parallel_safe=True,
    )
