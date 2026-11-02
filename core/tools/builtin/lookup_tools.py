"""Pernix — lookup and use_tool: self-service help that keeps the tool list fixed.

`lookup` searches memory, past sessions, skills and tools in one call, with no
LLM. `use_tool` runs a tool outside the native list. The agent's call gate
unwraps it before any check runs, so stuck detection, approval, allow-lists and
reflect all see the real tool.
"""

from __future__ import annotations

import json
import logging

from db import models as db

logger = logging.getLogger("pernix.tools.lookup")

_MEMORY_CHARS = 1800
_TOOL_HITS = 4
_SKILL_HITS = 3
_SCHEMA_CHARS = 700


def _memory_section(query: str, session_id: str) -> str:
    from core.memory.internal_recall import format_for_tool_output, internal_recall

    # Same rule as recall: a canary must not look up its own answer.
    session = db.get_session(session_id) if session_id else None
    if session and session.get("session_type") == "canary":
        return ""
    found = internal_recall(query, current_session_id=session_id or None, memory_limit=4)
    return format_for_tool_output(found)[:_MEMORY_CHARS]


def _skill_section(query: str) -> str:
    from core.skills.registry import get_skill_registry

    hits = get_skill_registry().discover(query, limit=_SKILL_HITS)
    return "\n".join(f"- {h.name}: {(h.description or '')[:120]} (load_skill)" for h in hits)


def _tool_section(query: str) -> str:
    from core.tools.native import NATIVE_TOOLS
    from core.tools.registry import get_registry

    registry = get_registry()
    lines = []
    for hit in registry.discover(query, limit=_TOOL_HITS):
        if hit.name in NATIVE_TOOLS:
            lines.append(f"- {hit.name}: call it directly")
            continue
        tool = registry.get(hit.name)
        schema = json.dumps(tool.parameters if tool else {}, separators=(",", ":"))[:_SCHEMA_CHARS]
        lines.append(f'- {hit.name}: {(hit.description or "")[:200]}\n  use_tool name="{hit.name}" schema: {schema}')
    return "\n".join(lines)


def lookup(query: str, _context: dict | None = None) -> str:
    """Search memory, past sessions, skills and tools in one call."""
    query = (query or "").strip()
    if not query:
        return "Error: lookup needs a query of 3 to 8 plain words."
    session_id = (_context or {}).get("session_id", "") or ""

    sections = []
    for title, build in (
        ("MEMORY AND PAST SESSIONS", lambda: _memory_section(query, session_id)),
        ("SKILLS", lambda: _skill_section(query)),
        ("TOOLS", lambda: _tool_section(query)),
    ):
        try:
            body = build()
        except Exception as e:
            logger.warning("lookup section %s failed: %s", title, e)
            continue
        if body:
            sections.append(f"[{title}]\n{body}")

    if not sections:
        return f"Nothing found for '{query}'. Try once with different words. Empty does not mean it does not exist."
    return "\n\n".join(sections)


def use_tool(name: str = "", arguments: dict | None = None, _context: dict | None = None) -> str:
    """Never runs a tool. The call gate unwraps use_tool before execution.

    Reaching this body means a path skipped the gate, and running the target
    from here would skip its checks too.
    """
    return f"Error: use_tool could not run '{name}'. Call it from a normal tool round."


def register(reg) -> None:
    reg.register(
        name="lookup",
        func=lookup,
        description=(
            "Search memory, past sessions, skills and tools in one fast call. "
            "Returns matches, and the schema of any tool you must call with use_tool."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "3 to 8 plain words naming the subject, not a sentence."},
            },
            "required": ["query"],
        },
        category="core",
        tags=["search", "memory", "tools", "skills", "discover"],
    )
    reg.register(
        name="use_tool",
        func=use_tool,
        description="Run a tool that is not in your tool list. Find its name and schema with lookup first.",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The tool's exact name."},
                "arguments": {"type": "object", "description": "The tool's arguments, as its schema describes."},
            },
            "required": ["name"],
        },
        category="core",
        tags=["tools", "run"],
    )
