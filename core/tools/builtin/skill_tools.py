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

    # Distinguish "doesn't exist" from "exists but disabled" so the model
    # gets an actionable message instead of a generic not-found.
    if registry.is_disabled(name):
        return f"Error: Skill '{name}' is disabled. " "Enable it in Explorer > Skills before use."
    instructions = registry.load_instructions(name)
    if instructions is None:
        # Registry may be stale (skill written externally or after a cancelled
        # creation). Rescan once before giving up.
        from pathlib import Path

        from config import settings

        registry.rescan(Path(settings.skills_dir))
        instructions = registry.load_instructions(name)
    if instructions is None:
        return f"Error: Skill '{name}' not found. Use discover_skills to search."

    # Build response with instructions + resource manifest
    parts = []

    # Surface broken-skill health FIRST with the concrete reason and fix —
    # otherwise the agent discovers the breakage mid-task via a bash stack
    # trace. Instructions still load: a broken script doesn't invalidate the
    # procedural guidance around it.
    issues = registry.validation_issues(name)
    if issues:
        parts.append(
            "[SKILL HEALTH] This skill has known problems — fix or work around them "
            "before relying on its scripts:\n" + "\n".join(f"  - {i}" for i in issues) + "\n---"
        )

    parts.append(instructions)

    # Script contracts from frontmatter: invocation shape without a
    # file_read round.
    skill_def = registry.get(name)
    if skill_def and skill_def.scripts_meta:
        parts.append("\n---\n**Scripts:**")
        for s in skill_def.scripts_meta:
            line = f"  - {s['path']}"
            if s.get("purpose"):
                line += f" — {s['purpose']}"
            parts.append(line)
            if s.get("usage"):
                parts.append(f"    usage: `{s['usage']}`")

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
        if registry.is_disabled(name):
            return f"Error: Skill '{name}' is disabled. " "Enable it in Explorer > Skills before use."

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


def delete_skill(name: str, _context: dict | None = None) -> str:
    """Permanently delete a skill and its directory from data/skills/{name}/.

    This is irreversible. The agent MUST call ask_user() describing the skill
    to be deleted and then approve_dangerous_tool() before this tool will execute.
    """
    import shutil
    from pathlib import Path

    from config import settings
    from core.skills.registry import get_skill_registry

    registry = get_skill_registry()
    skill = registry.get(name)

    if skill is None:
        # Check disabled skills too
        if not registry.exists(name):
            available = sorted(s.name for s in registry.all_skills())
            hint = f"Available: {', '.join(available)}" if available else "No skills installed."
            return f"Skill '{name}' not found. {hint}"
        # Exists but is disabled — still allow deletion
        skills_dir = Path(settings.skills_dir).resolve()
        skill_dir = skills_dir / name
    else:
        skill_dir = skill.path

    if not skill_dir.exists():
        return f"Skill directory not found at {skill_dir}."

    try:
        shutil.rmtree(skill_dir)
    except OSError as e:
        return f"Error deleting skill '{name}': {e}"

    try:
        skills_root = Path(settings.skills_dir).resolve()
        registry.rescan(skills_root)
    except Exception as e:
        logger.warning("delete_skill: registry rescan failed: %s", e)

    return f"Skill '{name}' deleted (data/skills/{name}/ removed)."


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

    # Under --dangerous the executor gate never fires, so the approval-sequence
    # text would teach a ritual with no enforcement behind it — describe the
    # actual behavior instead.
    from config import settings

    if settings.auto_approve_dangerous:
        _delete_skill_sequence = (
            "Executes immediately (--dangerous mode, no approval gate) — "
            "only call this when the user clearly asked for the deletion."
        )
    else:
        _delete_skill_sequence = (
            "Required call sequence: "
            "1) ask_user() naming the skill to be deleted, "
            "2) approve_dangerous_tool(tool_name='delete_skill', scope='delete skill <name>'), "
            "3) delete_skill(name). "
            "The executor will block this call if approval has not been granted."
        )
    reg.register(
        name="delete_skill",
        func=delete_skill,
        description=(
            "Permanently delete a skill and its directory from data/skills/{name}/. "
            "IRREVERSIBLE. " + _delete_skill_sequence
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Exact skill name (directory name in data/skills/)",
                },
            },
            "required": ["name"],
        },
        category="core",
        tags=["skill", "delete", "remove", "uninstall", "destroy"],
        timeout=15,
        parallel_safe=False,
        safety_level="dangerous",
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
