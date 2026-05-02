"""Pernix — Skill discovery tools: discover_skills, load_skill, read_skill_resource."""

from __future__ import annotations

import logging

logger = logging.getLogger("pernix.tools.skills")


def discover_skills(
    query: str,
    limit: int = 10,
    _context: dict | None = None,
) -> str:
    """Search for available skills by natural language description.

    Returns skill summaries (name + description + tags), not full instructions.
    Use load_skill(name) to activate a skill and get its instructions.
    """
    from core.skills.registry import get_skill_registry

    registry = get_skill_registry()

    results = registry.discover(query, limit=limit)

    if not results:
        return f"No skills found matching '{query}'. Try broader terms or different keywords."

    lines = []
    for r in results:
        tags_str = ", ".join(r.tags[:5]) if r.tags else ""
        extras = []
        if r.has_scripts:
            extras.append("has scripts")
        if r.has_references:
            extras.append("has references")
        extra_str = f" ({', '.join(extras)})" if extras else ""
        lines.append(f"- **{r.name}**: {r.description}{extra_str}")
        if tags_str:
            lines.append(f"  tags: {tags_str}")
    return "\n".join(lines)


def load_skill(
    name: str,
    _context: dict | None = None,
) -> str:
    """Load a skill's full instructions. Returns the SKILL.md body with procedural guidance.

    Also lists available scripts/references/assets for L3 access via bash or read_skill_resource.
    """
    from core.skills.registry import get_skill_registry

    registry = get_skill_registry()

    instructions = registry.load_instructions(name)
    if instructions is None:
        return f"Error: Skill '{name}' not found. Use discover_skills to search."

    # Build response with instructions + resource manifest
    parts = [instructions]

    resources = registry.list_resources(name)
    if resources:
        parts.append("\n---\n**Available Resources:**")
        skill = registry.get(name)
        skill_path = skill.path if skill else ""
        for category, files in resources.items():
            parts.append(f"\n{category}/:")
            for f in files:
                parts.append(f"  - {f}  (run: `bash {skill_path}/{category}/{f}` or use read_skill_resource)")

    return "\n".join(parts)


def read_skill_resource(
    name: str,
    resource_path: str,
    _context: dict | None = None,
) -> str:
    """Read a specific file from a skill's scripts/, references/, or assets/ directory.

    resource_path is relative to the skill directory (e.g. 'scripts/check.sh').
    """
    from core.skills.registry import get_skill_registry

    registry = get_skill_registry()

    content = registry.read_resource(name, resource_path)
    if content is None:
        if not registry.exists(name):
            return f"Error: Skill '{name}' not found. Use discover_skills to search."

        resources = registry.list_resources(name)
        if resources:
            available = []
            for cat, files in resources.items():
                for f in files:
                    available.append(f"{cat}/{f}")
            return (
                f"Error: Resource '{resource_path}' not found in skill '{name}'. " f"Available: {', '.join(available)}"
            )
        return f"Error: Resource '{resource_path}' not found in skill '{name}'. No resources available."

    return content


def register(reg) -> None:
    reg.register(
        name="discover_skills",
        func=discover_skills,
        description=(
            "Search for available skills (domain expertise packages) by capability. "
            "Returns names and descriptions. Use when you need specialized guidance — "
            "workflows, best practices, deployment procedures, debugging strategies, etc."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What expertise you need (e.g. 'git workflow', 'API debugging', 'deploy pipeline')",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 10)",
                },
            },
            "required": ["query"],
        },
        category="core",
        tags=["skill", "find", "capability", "expertise", "domain", "workflow", "guide"],
        timeout=15,
        parallel_safe=True,
    )

    reg.register(
        name="load_skill",
        func=load_skill,
        description=(
            "Load a skill's full instructions. Use after discover_skills to get "
            "detailed procedural guidance, workflows, and available scripts/references."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Exact skill name from discover_skills"},
            },
            "required": ["name"],
        },
        category="core",
        tags=["skill", "activate", "load", "instructions", "expertise"],
        timeout=15,
        parallel_safe=True,
    )

    reg.register(
        name="read_skill_resource",
        func=read_skill_resource,
        description=(
            "Read a specific file from a skill's scripts/, references/, or assets/ directory. "
            "Use for viewing reference docs or script source before executing."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Skill name"},
                "resource_path": {
                    "type": "string",
                    "description": "Path relative to skill dir (e.g. 'scripts/check.sh', 'references/schema.json')",
                },
            },
            "required": ["name", "resource_path"],
        },
        category="core",
        tags=["skill", "resource", "file", "script", "reference"],
        timeout=15,
        parallel_safe=True,
    )
