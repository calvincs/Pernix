"""Pernix — Skillmaker extension: agent can create and manage skills at runtime."""

from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml

from config import settings

logger = logging.getLogger("pernix.ext.skillmaker")


def _request_approval(action: str, details: str, _context: dict | None = None) -> str:
    """Post an approval question via ask_user. Returns the pending message.

    NOT an authorization mechanism: the caller re-invokes with a model-supplied
    `approved=true`, which nothing correlates with an actual user response. The
    two tools whose blast radius makes that unacceptable — create_skill (a new
    L2 instruction package the agent will later follow) and add_skill_script (a
    file load_skill then tells the agent to run under bash) — no longer take an
    `approved` argument at all; they are registered safety_level="dangerous" so
    the executor's server-side gate decides, from state only
    approve_dangerous_tool can write. The remaining users of this helper keep
    the prompt as a speed bump over markdown edits. See docs/security.md.
    """
    from core.tools.builtin.dialog_tools import ask_user

    ask_user(
        question=f"Skill modification requires approval:\n\n**{action}**\n{details}\n\nApprove this change?",
        context="Skill files are semi-protected. Reply 'yes' to approve.",
        urgency="high",
        question_type="question",
        _context=_context,
    )
    return (
        f"Approval requested: {action}. Awaiting user response. "
        f"Call this tool again with approved=true after the user approves."
    )


# Valid skill name pattern: lowercase letters, numbers, hyphens
SKILL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")
RESERVED_NAMES = {"test", "example", "template", "skill", "default", "core", "system"}
MAX_NAME_LENGTH = 64
MAX_INSTRUCTIONS_SIZE = 50_000  # ~12k tokens
MAX_SCRIPT_SIZE = 500_000  # ~500KB per script
MAX_REFERENCE_SIZE = 500_000  # ~500KB per reference


def _skills_dir() -> Path:
    return Path(settings.skills_dir)


def _get_skill_path(name: str) -> Path:
    return _skills_dir() / name


def _validate_name(name: str) -> str | None:
    """Validate skill name. Returns error message or None if valid."""
    if not name:
        return "Error: Skill name cannot be empty"
    if len(name) > MAX_NAME_LENGTH:
        return f"Error: Skill name too long (max {MAX_NAME_LENGTH} chars)"
    if not SKILL_NAME_PATTERN.match(name):
        return "Error: Skill name must be lowercase letters, numbers, and hyphens (e.g. 'git-workflow')"
    if name in RESERVED_NAMES:
        return f"Error: '{name}' is a reserved name"
    return None


def _build_skill_md(name: str, description: str, instructions: str, tags: list[str], version: str = "1.0") -> str:
    """Build SKILL.md content with YAML frontmatter + body."""
    frontmatter = {
        "name": name,
        "description": description,
        "tags": tags,
        "version": version,
    }
    yaml_str = yaml.dump(frontmatter, default_flow_style=False, sort_keys=False).strip()
    return f"---\n{yaml_str}\n---\n\n{instructions}\n"


def create_skill(
    name: str,
    description: str,
    instructions: str,
    tags: str = "",
    _context: dict | None = None,
) -> str:
    """Create a new skill package with SKILL.md.

    The skill will be immediately available for discovery and loading.
    Authorization is the executor's dangerous-tool gate (ask_user +
    approve_dangerous_tool), not an argument on this call.
    """
    # Validate name
    err = _validate_name(name)
    if err:
        return err

    if not description or len(description) < 10:
        return "Error: Description must be at least 10 characters"
    if not instructions or len(instructions) < 20:
        return "Error: Instructions must be at least 20 characters"
    if len(instructions) > MAX_INSTRUCTIONS_SIZE:
        return f"Error: Instructions too large (max {MAX_INSTRUCTIONS_SIZE} bytes)"

    skill_path = _get_skill_path(name)
    skill_md = skill_path / "SKILL.md"
    if skill_md.exists():
        return f"Error: Skill '{name}' already exists. Use update_skill to modify it."
    # Directory may exist without SKILL.md (e.g. cancelled creation or manual mkdir).
    # That's an orphaned dir — proceed and write the SKILL.md into it.

    # Parse tags
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

    # Create directory and SKILL.md
    skill_path.mkdir(parents=True, exist_ok=True)
    content = _build_skill_md(name, description, instructions, tag_list)
    skill_md.write_text(content, encoding="utf-8")

    # Rescan registry to pick up new skill
    from core.skills.registry import get_skill_registry

    reg = get_skill_registry()
    reg.rescan(Path(settings.skills_dir))
    # Clear any stale disabled flag from a prior skill of the same name
    # (e.g. user disabled "foo", deleted the dir manually, then create_skill
    # rebuilds it — without this, the new skill would be silently disabled).
    reg.enable(name)

    if reg.exists(name):
        return (
            f"Skill '{name}' created at {skill_path}/SKILL.md. "
            f"Use discover_skills or load_skill(name='{name}') to access it."
        )
    else:
        return f"Warning: Skill '{name}' created but failed to register. Check SKILL.md format."


def update_skill(
    name: str,
    description: str = "",
    instructions: str = "",
    tags: str = "",
    approved: bool = False,
    _context: dict | None = None,
) -> str:
    """Update an existing skill. Only provided fields are changed.
    Requires user approval (set approved=true after user approves)."""
    if not approved:
        changes = []
        if description:
            changes.append(f"description → {description[:80]}...")
        if instructions:
            changes.append(f"instructions ({len(instructions)} chars)")
        if tags:
            changes.append(f"tags → {tags}")
        return _request_approval(
            f"Update skill '{name}'",
            "Changes: " + ", ".join(changes) if changes else "No changes specified",
            _context,
        )

    skill_path = _get_skill_path(name)
    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        return f"Error: Skill '{name}' not found"

    if instructions and len(instructions) > MAX_INSTRUCTIONS_SIZE:
        return f"Error: Instructions too large (max {MAX_INSTRUCTIONS_SIZE} bytes)"

    # Parse existing
    from core.skills.parser import SkillParseError, parse_skill_md

    try:
        frontmatter, existing_body = parse_skill_md(skill_md)
    except SkillParseError as e:
        return f"Error: Failed to parse existing skill: {e}"

    # Apply updates
    if description:
        frontmatter["description"] = description
    if tags:
        frontmatter["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
    body = instructions if instructions else existing_body

    # Write updated SKILL.md
    content = _build_skill_md(
        name=frontmatter["name"],
        description=frontmatter["description"],
        instructions=body,
        tags=frontmatter.get("tags", []),
        version=frontmatter.get("version", "1.0"),
    )
    skill_md.write_text(content, encoding="utf-8")

    # Rescan
    from core.skills.registry import get_skill_registry

    reg = get_skill_registry()
    reg.rescan(Path(settings.skills_dir))

    return f"Skill '{name}' updated."


def list_skills(_context: dict | None = None) -> str:
    """List all enabled installed skills with their metadata.

    Disabled skills are hidden from the agent (they appear in the Explorer UI
    only). The agent should not be told about skills it cannot use.
    """
    from core.skills.registry import get_skill_registry

    reg = get_skill_registry()
    skills = reg.enabled_skills()

    if not skills:
        return "No skills installed. Use create_skill to create one."

    lines = []
    for s in sorted(skills, key=lambda x: x.name):
        tags_str = ", ".join(s.tags[:5]) if s.tags else "none"
        resources = reg.list_resources(s.name)
        extras = []
        if "scripts" in resources:
            extras.append(f"{len(resources['scripts'])} scripts")
        if "references" in resources:
            extras.append(f"{len(resources['references'])} refs")
        extra_str = f" [{', '.join(extras)}]" if extras else ""
        lines.append(f"- **{s.name}** (v{s.version}): {s.description}")
        lines.append(f"  tags: {tags_str}{extra_str}")

    return "\n".join(lines)


def add_skill_script(
    name: str,
    script_name: str,
    content: str,
    executable: bool = True,
    purpose: str = "",
    usage: str = "",
    _context: dict | None = None,
) -> str:
    """Add or update a standalone CLI script in a skill's scripts/ directory.

    Scripts should be self-contained and callable via bash. Provide `purpose`
    (one line: what it does) and `usage` (invocation example) so the contract
    is recorded in SKILL.md frontmatter and future sessions know how to call
    the script without reading it.
    Authorization is the executor's dangerous-tool gate (ask_user +
    approve_dangerous_tool), not an argument on this call.
    """
    skill_path = _get_skill_path(name)
    if not (skill_path / "SKILL.md").exists():
        return f"Error: Skill '{name}' not found"

    if not script_name:
        return "Error: script_name is required"
    if not content or len(content) < 5:
        return "Error: Script content too short"
    if len(content) > MAX_SCRIPT_SIZE:
        return f"Error: Script too large (max {MAX_SCRIPT_SIZE} bytes)"
    if ".." in script_name or "/" in script_name or "\x00" in script_name:
        return "Error: script_name must be a simple filename (no paths or special chars)"

    scripts_dir = skill_path / "scripts"
    scripts_dir.mkdir(exist_ok=True)

    script_path = scripts_dir / script_name
    is_update = script_path.exists()

    # Block --break-system-packages in any script
    if "--break-system-packages" in content:
        return "Error: --break-system-packages is not allowed. Use the workspace venv."

    # For Python scripts, ensure they use env python (resolves to venv via PATH)
    if script_name.endswith(".py"):
        if content.startswith("#!"):
            first_line = content.split("\n")[0]
            if "/usr/bin/python" in first_line and "env" not in first_line:
                lines = content.split("\n")
                lines[0] = "#!/usr/bin/env python3"
                content = "\n".join(lines)
        else:
            content = "#!/usr/bin/env python3\n" + content

    script_path.write_text(content, encoding="utf-8")

    # Set permissions explicitly: add or remove execute bit based on flag
    mode = script_path.stat().st_mode
    if executable:
        script_path.chmod(mode | 0o755)
    else:
        script_path.chmod(mode & ~0o111)

    # Record the script contract in frontmatter so the L2 injection can show
    # invocation shape without a file_read round (adaptation plan 1d).
    if purpose or usage:
        try:
            _upsert_script_contract(skill_path, f"scripts/{script_name}", purpose, usage)
        except Exception as e:
            logger.warning("Could not record script contract for '%s': %s", script_name, e)

    # Rescan to update resource metadata
    from core.skills.registry import get_skill_registry

    get_skill_registry().rescan(Path(settings.skills_dir))

    verb = "updated" if is_update else "added"
    return f"Script '{script_name}' {verb} in skill '{name}'. Run: bash {script_path}"


def _upsert_script_contract(skill_path: Path, rel_path: str, purpose: str, usage: str) -> None:
    """Add or update this script's entry in SKILL.md frontmatter `scripts:`."""
    import yaml

    from core.skills.parser import parse_skill_md

    skill_md = skill_path / "SKILL.md"
    frontmatter, body = parse_skill_md(skill_md)
    scripts = frontmatter.get("scripts")
    if not isinstance(scripts, list):
        scripts = []
    entry = next((s for s in scripts if isinstance(s, dict) and s.get("path") == rel_path), None)
    if entry is None:
        entry = {"path": rel_path}
        scripts.append(entry)
    if purpose:
        entry["purpose"] = purpose
    if usage:
        entry["usage"] = usage
    frontmatter["scripts"] = scripts
    raw = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
    skill_md.write_text(f"---\n{raw}\n---\n\n{body}\n", encoding="utf-8")


def add_skill_reference(
    name: str,
    filename: str,
    content: str,
    approved: bool = False,
    _context: dict | None = None,
) -> str:
    """Add or update a reference document in a skill's references/ directory.
    Requires user approval (set approved=true after user approves)."""
    if not approved:
        return _request_approval(
            f"Add/update reference '{filename}' in skill '{name}'",
            f"Content: {len(content)} chars",
            _context,
        )

    skill_path = _get_skill_path(name)
    if not (skill_path / "SKILL.md").exists():
        return f"Error: Skill '{name}' not found"

    if not filename:
        return "Error: filename is required"
    if not content:
        return "Error: Content is required"
    if len(content) > MAX_REFERENCE_SIZE:
        return f"Error: Reference too large (max {MAX_REFERENCE_SIZE} bytes)"
    if ".." in filename or "/" in filename or "\x00" in filename:
        return "Error: filename must be a simple filename (no paths or special chars)"

    refs_dir = skill_path / "references"
    refs_dir.mkdir(exist_ok=True)

    ref_path = refs_dir / filename
    is_update = ref_path.exists()
    ref_path.write_text(content, encoding="utf-8")

    # Rescan to update resource metadata
    from core.skills.registry import get_skill_registry

    get_skill_registry().rescan(Path(settings.skills_dir))

    verb = "updated" if is_update else "added"
    return f"Reference '{filename}' {verb} in skill '{name}'. Access via read_skill_resource or load_skill."


def remove_skill_script(
    name: str,
    script_name: str,
    approved: bool = False,
    _context: dict | None = None,
) -> str:
    """Remove a script from a skill's scripts/ directory.
    Requires user approval (set approved=true after user approves)."""
    if not approved:
        return _request_approval(
            f"Remove script '{script_name}' from skill '{name}'",
            "This will permanently delete the script file.",
            _context,
        )

    skill_path = _get_skill_path(name)
    if not (skill_path / "SKILL.md").exists():
        return f"Error: Skill '{name}' not found"

    if not script_name:
        return "Error: script_name is required"
    if ".." in script_name or "/" in script_name or "\x00" in script_name:
        return "Error: script_name must be a simple filename (no paths or special chars)"

    script_path = skill_path / "scripts" / script_name
    if not script_path.exists():
        return f"Error: Script '{script_name}' not found in skill '{name}'"

    script_path.unlink()

    # Clean up empty scripts/ directory
    scripts_dir = skill_path / "scripts"
    if scripts_dir.exists() and not any(scripts_dir.iterdir()):
        scripts_dir.rmdir()

    from core.skills.registry import get_skill_registry

    get_skill_registry().rescan(Path(settings.skills_dir))

    return f"Script '{script_name}' removed from skill '{name}'."


def remove_skill_reference(
    name: str,
    filename: str,
    approved: bool = False,
    _context: dict | None = None,
) -> str:
    """Remove a reference document from a skill's references/ directory.
    Requires user approval (set approved=true after user approves)."""
    if not approved:
        return _request_approval(
            f"Remove reference '{filename}' from skill '{name}'",
            "This will permanently delete the reference file.",
            _context,
        )

    skill_path = _get_skill_path(name)
    if not (skill_path / "SKILL.md").exists():
        return f"Error: Skill '{name}' not found"

    if not filename:
        return "Error: filename is required"
    if ".." in filename or "/" in filename or "\x00" in filename:
        return "Error: filename must be a simple filename (no paths or special chars)"

    ref_path = skill_path / "references" / filename
    if not ref_path.exists():
        return f"Error: Reference '{filename}' not found in skill '{name}'"

    ref_path.unlink()

    # Clean up empty references/ directory
    refs_dir = skill_path / "references"
    if refs_dir.exists() and not any(refs_dir.iterdir()):
        refs_dir.rmdir()

    from core.skills.registry import get_skill_registry

    get_skill_registry().rescan(Path(settings.skills_dir))

    return f"Reference '{filename}' removed from skill '{name}'."


def register(reg) -> None:
    common = {"category": "skills", "source": "extension"}
    tags_base = ["skill", "create", "manage", "author"]

    # Confirmation prompt, not authorization — see _request_approval. Kept on
    # the markdown-editing tools only; create_skill and add_skill_script use
    # the executor's server-side dangerous gate instead.
    _approved_param = {
        "type": "boolean",
        "description": "Set to true after user approves the change. First call without this posts an approval request.",
    }

    reg.register(
        name="create_skill",
        func=create_skill,
        description=(
            "Create a new skill package. Skills provide domain expertise as instruction "
            "packages with optional scripts and references. Provide a name, description, "
            "and procedural instructions. Requires user approval via ask_user + "
            "approve_dangerous_tool before the call will execute."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Skill name (lowercase, hyphens allowed, e.g. 'git-workflow')",
                },
                "description": {
                    "type": "string",
                    "description": "What the skill does and when to use it (10+ chars)",
                },
                "instructions": {
                    "type": "string",
                    "description": "Procedural instructions in markdown (the SKILL.md body)",
                },
                "tags": {
                    "type": "string",
                    "description": "Comma-separated discovery tags",
                },
            },
            "required": ["name", "description", "instructions"],
        },
        tags=tags_base + ["new", "build"],
        timeout=30,
        parallel_safe=False,
        # A skill is an instruction package the agent will later load and
        # follow, so authoring one is authoring the agent's own future
        # behaviour. The old `approved` boolean was model-supplied and could
        # simply be set to true; the executor's gate keeps approval state on
        # the session where no argument can reach it.
        safety_level="dangerous",
        **common,
    )

    reg.register(
        name="update_skill",
        func=update_skill,
        description="Update an existing skill's description, instructions, or tags. Requires user approval.",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Skill name to update"},
                "description": {"type": "string", "description": "New description (optional)"},
                "instructions": {"type": "string", "description": "New instructions body (optional)"},
                "tags": {"type": "string", "description": "New comma-separated tags (optional)"},
                "approved": _approved_param,
            },
            "required": ["name"],
        },
        tags=tags_base + ["update", "edit", "modify"],
        timeout=30,
        parallel_safe=False,
        safety_level="safe",
        **common,
    )

    reg.register(
        name="list_skills",
        func=list_skills,
        description="List all installed skills with their metadata, tags, and resources.",
        parameters={"type": "object", "properties": {}},
        tags=tags_base + ["list", "show", "available"],
        timeout=15,
        parallel_safe=True,
        **common,
    )

    reg.register(
        name="add_skill_script",
        func=add_skill_script,
        description=(
            "Add a standalone CLI script to a skill's scripts/ directory. "
            "Scripts should be self-contained and callable via bash. Requires user approval "
            "via ask_user + approve_dangerous_tool before the call will execute."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Skill name"},
                "script_name": {"type": "string", "description": "Script filename (e.g. 'check.sh', 'analyze.py')"},
                "content": {"type": "string", "description": "Script content"},
                "executable": {"type": "boolean", "description": "Set executable permission (default true)"},
                "purpose": {
                    "type": "string",
                    "description": "One line: what the script does (recorded as a contract in SKILL.md)",
                },
                "usage": {
                    "type": "string",
                    "description": "Invocation example, e.g. 'bash scripts/check.sh <url>'",
                },
            },
            "required": ["name", "script_name", "content"],
        },
        tags=["skill", "script", "add", "bash", "cli"],
        timeout=30,
        parallel_safe=False,
        # Writes an executable file that load_skill then advertises to the
        # agent as `bash <skill>/scripts/<file>` — write-then-run arbitrary
        # code, previously ungated at every step.
        safety_level="dangerous",
        **common,
    )

    reg.register(
        name="add_skill_reference",
        func=add_skill_reference,
        description="Add a reference document to a skill's references/ directory. Requires user approval.",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Skill name"},
                "filename": {
                    "type": "string",
                    "description": "Reference filename (e.g. 'schema.json', 'cheatsheet.md')",
                },
                "content": {"type": "string", "description": "Reference content"},
                "approved": _approved_param,
            },
            "required": ["name", "filename", "content"],
        },
        tags=["skill", "reference", "add", "document"],
        timeout=30,
        parallel_safe=False,
        safety_level="safe",
        **common,
    )

    reg.register(
        name="remove_skill_script",
        func=remove_skill_script,
        description="Remove a script from a skill's scripts/ directory. Requires user approval.",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Skill name"},
                "script_name": {"type": "string", "description": "Script filename to remove"},
                "approved": _approved_param,
            },
            "required": ["name", "script_name"],
        },
        tags=["skill", "script", "remove", "delete"],
        timeout=15,
        parallel_safe=False,
        safety_level="safe",
        **common,
    )

    reg.register(
        name="remove_skill_reference",
        func=remove_skill_reference,
        description="Remove a reference document from a skill's references/ directory. Requires user approval.",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Skill name"},
                "filename": {"type": "string", "description": "Reference filename to remove"},
                "approved": _approved_param,
            },
            "required": ["name", "filename"],
        },
        tags=["skill", "reference", "remove", "delete"],
        timeout=15,
        parallel_safe=False,
        safety_level="safe",
        **common,
    )
