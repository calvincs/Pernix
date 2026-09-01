"""Pernix — Tool discovery: discover_tools."""

from __future__ import annotations

import logging

logger = logging.getLogger("pernix.tools.discovery")


def discover_tools(
    query: str,
    category: str = "",
    limit: int = 10,
    _context: dict | None = None,
) -> str:
    """Search for available tools by natural language description.

    Returns tool summaries (name + description + tags). Discovered tools are
    added to the session's active set, so their full schemas arrive on the
    next round automatically.
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
