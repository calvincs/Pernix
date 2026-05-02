"""Pernix — Workflow tools: discover, create, validate, and inspect workflows.

IMPORTANT FOR AGENTS: Workflows are YAML-schema files, not prose documents.
They live in data/workflows/{name}/WORKFLOW.md — NOT in the workspace.
Always use create_workflow() to create them, never file_write().
Always call validate_workflow(name) after creation to confirm the file is valid.
"""

from __future__ import annotations

import logging
from pathlib import Path

from config import settings

logger = logging.getLogger("pernix.tools.workflows")


def _workflows_root() -> Path:
    """Absolute path to data/workflows/ — independent of CWD."""
    return Path(settings.workflows_dir).resolve()


_SCHEMA_EXAMPLE = """
WORKFLOW.md SCHEMA — copy this structure, fill in your own values:

---
name: my-workflow
description: One sentence describing what this workflow does
tags: [tag1, tag2]
version: "1.0"
steps:
  - id: step-one
    type: instruction
    description: Short label shown in the UI
    instructions: |
      Detailed instructions the worker will follow.
      Can span multiple lines.
    output_file: step_one_output.md
    depends_on: []

  - id: step-two
    type: skill
    skill: my-skill-name
    description: Short label
    instructions: |
      Additional per-step guidance that augments the skill's own instructions.
      Optional — omit if the skill instructions are sufficient.
    output_file: step_two_output.md
    depends_on: [step-one]
---

Optional usage notes in markdown here.

RULES:
- steps is a YAML list in the frontmatter — not prose sections in the body
- Each step needs: id, description, output_file, depends_on (list, can be empty)
- type: "skill" requires a "skill" field matching an installed skill name
- type: "instruction" has no skill — the worker follows instructions directly
- step id must start with a letter, contain only letters/digits/hyphens/underscores
- output_file is a bare filename (no slashes) — executor places it in the run directory
- depends_on references other step ids; empty list [] means "run in the first wave"
""".strip()


def get_workflow_schema(_context: dict | None = None) -> str:
    """Return the WORKFLOW.md schema with a concrete example.

    Call this first when creating a new workflow — it shows the exact YAML
    structure required. The body (below the closing ---) is optional prose.
    """
    return _SCHEMA_EXAMPLE


def create_workflow(name: str, content: str, _context: dict | None = None) -> str:
    """Create or overwrite a workflow file at data/workflows/{name}/WORKFLOW.md.

    Validates the content before writing. Returns a validation error description
    if the content is invalid so you can fix it and try again.

    ALWAYS use this tool instead of file_write — it writes to the correct
    location (data/workflows/, not the workspace) and registers the workflow
    in the registry so it can be run with run_workflow().

    Workflow:
      1. Call get_workflow_schema() to get the required format
      2. Call create_workflow(name, content) with the YAML content
      3. Fix any errors reported and call create_workflow() again
      4. Run with run_workflow(name, inputs)
    """
    from core.workflows.registry import get_workflow_registry
    from core.workflows.validator import validate_content

    # Validate before touching the filesystem
    result = validate_content(content, check_skills=True)
    if not result.valid:
        return (
            f"Cannot create workflow '{name}' — validation failed:\n\n"
            + result.to_agent_text()
            + "\n\nFix the errors above and call create_workflow() again."
        )

    # Write to the correct location (absolute path — independent of CWD)
    root = _workflows_root()
    wf_dir = root / name
    try:
        wf_dir.mkdir(parents=True, exist_ok=True)
        wf_md = wf_dir / "WORKFLOW.md"
        wf_md.write_text(content, encoding="utf-8")
    except OSError as e:
        return f"Error writing workflow to disk: {e}"

    # Register
    try:
        reg = get_workflow_registry()
        reg.rescan(root)
    except Exception as e:
        logger.warning("create_workflow: registry rescan failed: %s", e)

    info = result.info
    waves = info.get("waves", [])
    wave_summary = ""
    for i, wave in enumerate(waves, 1):
        wave_summary += f"\n  Wave {i}: {', '.join(wave)}"

    return (
        f"Workflow '{name}' created successfully at data/workflows/{name}/WORKFLOW.md\n"
        f"{info.get('step_count', 0)} step(s) in {info.get('wave_count', 0)} execution wave(s).{wave_summary}\n"
        + (
            f"Note: {', '.join(info.get('missing_skills', []))} not found in registry (will fail at runtime)."
            if info.get("missing_skills")
            else ""
        )
    )


def discover_workflows(query: str = "", _context: dict | None = None) -> str:
    """List available workflows, optionally filtered by keyword."""
    from core.workflows.registry import get_workflow_registry

    reg = get_workflow_registry()
    workflows = reg.all_workflows()

    if not workflows:
        return (
            "No workflows installed. Use create_workflow(name, content) to create one. "
            "Call get_workflow_schema() first for the required YAML format."
        )

    if query:
        q = query.lower()
        workflows = [
            wf
            for wf in workflows
            if q in wf.name.lower() or q in wf.description.lower() or any(q in t.lower() for t in wf.tags)
        ]
        if not workflows:
            return f"No workflows match '{query}'."

    lines = []
    for wf in sorted(workflows, key=lambda w: w.name):
        step_summary = f"{len(wf.steps)} step(s)"
        tags_str = f" [{', '.join(wf.tags)}]" if wf.tags else ""
        lines.append(f"- **{wf.name}** (v{wf.version}): {wf.description}{tags_str} — {step_summary}")

    return "\n".join(lines)


def validate_workflow(name: str, _context: dict | None = None) -> str:
    """Validate an installed workflow by name.

    Rescans the registry to pick up recent file changes, then runs full
    validation (schema, step structure, skill references, DAG integrity).
    Returns a human-readable result the agent can use to self-correct.

    IMPORTANT: Workflows must be in data/workflows/{name}/WORKFLOW.md.
    If you wrote the file to the workspace, use create_workflow() instead
    (it writes to the correct location automatically).
    """
    from core.workflows.registry import get_workflow_registry
    from core.workflows.validator import validate_content, validate_file

    # Rescan to pick up any file changes made since last scan
    try:
        reg = get_workflow_registry()
        reg.rescan(_workflows_root())
    except Exception as e:
        logger.warning("validate_workflow: rescan failed: %s", e)
        reg = get_workflow_registry()

    wf = reg.get(name)
    if not wf:
        available = sorted(w.name for w in reg.all_workflows())

        # Check common mistake: file written to workspace instead of data/workflows/
        workspace_path = Path("data/workspace") / "workflows" / name / "WORKFLOW.md"
        if workspace_path.exists():
            content = workspace_path.read_text(encoding="utf-8")
            pre_check = validate_content(content, check_skills=False)
            schema_ok = pre_check.valid
            return (
                f"Workflow '{name}' is not in the registry.\n"
                f"Found a file at {workspace_path} — but workflows must be in data/workflows/, not the workspace.\n\n"
                f"The file {'passes' if schema_ok else 'FAILS'} schema validation.\n"
                + (pre_check.to_agent_text() + "\n\n" if not schema_ok else "")
                + f"Fix: call create_workflow('{name}', <content>) with the corrected YAML content. "
                f"It will write to the correct location and register the workflow automatically."
            )

        hint = (
            f"Available: {', '.join(available)}"
            if available
            else ("No workflows installed. Use create_workflow(name, content) to create one.")
        )
        return (
            f"Workflow '{name}' not found in registry. {hint}\n\n"
            "Note: workflows live in data/workflows/{name}/WORKFLOW.md — "
            "never in the workspace. Use create_workflow() to create them."
        )

    wf_md = wf.path / "WORKFLOW.md"
    result = validate_file(wf_md, check_skills=True)
    return result.to_agent_text()


def validate_workflow_content(content: str, _context: dict | None = None) -> str:
    """Validate raw WORKFLOW.md content without writing to disk.

    Use this to pre-check a workflow definition before calling create_workflow().
    Call get_workflow_schema() to see the required YAML format.
    """
    from core.workflows.validator import validate_content

    result = validate_content(content, check_skills=True)
    return result.to_agent_text()


def register(reg) -> None:
    wf_tags = ["workflow", "pipeline", "chain", "automate", "multi-step", "orchestrate"]

    reg.register(
        name="get_workflow_schema",
        func=get_workflow_schema,
        description=(
            "Return the WORKFLOW.md YAML schema with a concrete example. "
            "Call this FIRST before creating a workflow — workflows are not prose documents, "
            "they are YAML files with a specific steps: list in the frontmatter. "
            "Never use file_write to create workflows; use create_workflow() instead."
        ),
        parameters={"type": "object", "properties": {}},
        category="core",
        tags=wf_tags + ["schema", "template", "format", "example"],
        timeout=5,
        parallel_safe=True,
    )

    reg.register(
        name="create_workflow",
        func=create_workflow,
        description=(
            "Create or update a workflow at data/workflows/{name}/WORKFLOW.md. "
            "Validates the content before writing — returns errors to fix if invalid. "
            "ALWAYS use this instead of file_write: it writes to the correct location "
            "(data/workflows/, NOT the workspace) and registers the workflow automatically. "
            "Workflow: get_workflow_schema() → build content → create_workflow() → fix errors → run_workflow()."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Workflow name — becomes the directory name in data/workflows/",
                },
                "content": {
                    "type": "string",
                    "description": "Full WORKFLOW.md content with YAML frontmatter steps: list. "
                    "Call get_workflow_schema() first if unsure of the format.",
                },
            },
            "required": ["name", "content"],
        },
        category="core",
        tags=wf_tags + ["create", "write", "save", "install"],
        timeout=15,
        parallel_safe=False,
    )

    reg.register(
        name="discover_workflows",
        func=discover_workflows,
        description=(
            "List available reusable workflows. Workflows are multi-step pipelines "
            "that chain skills together. Use to see what's available before running one."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Optional keyword to filter by name, description, or tags",
                },
            },
        },
        category="core",
        tags=wf_tags + ["discover", "list", "find"],
        timeout=10,
        parallel_safe=True,
    )

    reg.register(
        name="validate_workflow",
        func=validate_workflow,
        description=(
            "Validate an installed workflow by name. Checks YAML schema, step structure, "
            "skill references, and DAG integrity. Always call after create_workflow() to confirm. "
            "If the workflow is not found, checks whether it was accidentally written to the "
            "workspace (wrong location) and tells you how to fix it."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Workflow name (directory name in data/workflows/)",
                },
            },
            "required": ["name"],
        },
        category="core",
        tags=wf_tags + ["validate", "check", "verify"],
        timeout=15,
        parallel_safe=True,
    )
