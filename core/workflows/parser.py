"""Pernix — WORKFLOW.md parser: YAML frontmatter + DAG validation."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger("pernix.workflows.parser")


class WorkflowParseError(Exception):
    """Raised when a WORKFLOW.md file cannot be parsed or contains an invalid DAG."""


@dataclass
class StepDef:
    """Definition of a single step in a workflow."""

    id: str
    type: str  # "skill" | "instruction"
    description: str  # short human-readable label
    instructions: str  # detailed instructions (may be empty string)
    skill: str | None  # skill name; None for instruction steps
    output_file: str  # filename (no path); executor places in run dir
    depends_on: list[str] = field(default_factory=list)
    # Optional per-step model override. When set, the worker spawned for
    # this step uses this model instead of the default llm_model. Useful
    # for steps that need stronger reasoning (synthesize) or tool-use
    # consistency (transcribe with multi-step bash) than the default.
    # Empty string means "use the global default."
    model: str = ""


@dataclass
class WorkflowDef:
    """Definition of a registered workflow."""

    name: str
    description: str
    path: Path  # absolute path to workflow directory
    tags: list[str]
    version: str
    steps: list[StepDef]
    body: str  # freeform usage notes (markdown body)

    def topological_waves(self) -> list[list[StepDef]]:
        """Return steps grouped into parallel execution waves.

        Steps in the same wave have no dependencies on each other.
        Each wave depends only on prior waves. Raises WorkflowParseError
        on cycles (should not occur if parse_workflow_md passed).
        """
        step_map = {s.id: s for s in self.steps}
        in_degree: dict[str, int] = {s.id: 0 for s in self.steps}
        dependents: dict[str, list[str]] = {s.id: [] for s in self.steps}

        for step in self.steps:
            for dep in step.depends_on:
                in_degree[step.id] += 1
                dependents[dep].append(step.id)

        waves: list[list[StepDef]] = []
        ready = [sid for sid, deg in in_degree.items() if deg == 0]

        while ready:
            wave = [step_map[sid] for sid in sorted(ready)]
            waves.append(wave)
            next_ready = []
            for sid in ready:
                for child_id in dependents[sid]:
                    in_degree[child_id] -= 1
                    if in_degree[child_id] == 0:
                        next_ready.append(child_id)
            ready = next_ready

        if sum(len(w) for w in waves) != len(self.steps):
            raise WorkflowParseError("Cycle detected in workflow DAG")

        return waves


def parse_workflow_md(path: Path) -> tuple[dict, str]:
    """Parse a WORKFLOW.md file into (frontmatter_dict, body_markdown).

    Raises WorkflowParseError on missing required fields or invalid DAG.
    """
    text = path.read_text(encoding="utf-8")

    if not text.startswith("---"):
        raise WorkflowParseError(f"{path}: Missing YAML frontmatter (must start with ---)")

    parts = text.split("---", 2)
    if len(parts) < 3:
        raise WorkflowParseError(f"{path}: Malformed frontmatter (needs opening and closing ---)")

    raw_yaml = parts[1].strip()
    body = parts[2].strip()

    try:
        frontmatter = yaml.safe_load(raw_yaml)
    except yaml.YAMLError as e:
        raise WorkflowParseError(f"{path}: Invalid YAML frontmatter: {e}") from e

    if not isinstance(frontmatter, dict):
        raise WorkflowParseError(f"{path}: Frontmatter must be a YAML mapping")

    name = frontmatter.get("name")
    if not name:
        raise WorkflowParseError(f"{path}: Missing required field 'name'")
    if not isinstance(name, str):
        frontmatter["name"] = str(name)

    desc = frontmatter.get("description")
    if not desc:
        raise WorkflowParseError(f"{path}: Missing required field 'description'")
    if not isinstance(desc, str):
        frontmatter["description"] = str(desc)

    # Normalize tags
    tags = frontmatter.get("tags", [])
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    frontmatter["tags"] = [str(t) for t in tags]

    frontmatter.setdefault("version", "1.0")

    raw_steps = frontmatter.get("steps")
    if not raw_steps or not isinstance(raw_steps, list):
        raise WorkflowParseError(f"{path}: Missing or empty 'steps' list")

    steps = _parse_steps(path, raw_steps)
    _validate_dag(path, steps)
    frontmatter["_parsed_steps"] = steps

    return frontmatter, body


def _parse_steps(path: Path, raw_steps: list) -> list[StepDef]:
    steps = []
    seen_ids: set[str] = set()

    for i, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            raise WorkflowParseError(f"{path}: Step {i} must be a YAML mapping")

        step_id = raw.get("id")
        if not step_id:
            raise WorkflowParseError(f"{path}: Step {i} missing required 'id'")
        step_id = str(step_id)
        if step_id in seen_ids:
            raise WorkflowParseError(f"{path}: Duplicate step id '{step_id}'")
        seen_ids.add(step_id)

        skill = raw.get("skill")
        if skill is not None:
            skill = str(skill)

        # Infer type from presence of skill field
        step_type = str(raw.get("type", "skill" if skill else "instruction"))
        if step_type not in ("skill", "instruction"):
            raise WorkflowParseError(
                f"{path}: Step '{step_id}' type must be 'skill' or 'instruction', got '{step_type}'"
            )
        if step_type == "skill" and not skill:
            raise WorkflowParseError(f"{path}: Step '{step_id}' has type 'skill' but no 'skill' field")

        description = str(raw.get("description", "")).strip()
        instructions = str(raw.get("instructions", "")).strip()
        output_file = str(raw.get("output_file", f"step_{step_id}_output.md")).strip()

        depends_on_raw = raw.get("depends_on", [])
        if isinstance(depends_on_raw, str):
            depends_on = [depends_on_raw.strip()]
        else:
            depends_on = [str(d) for d in depends_on_raw]

        # Optional per-step model override (e.g. use a 122B model for
        # synthesize but the default 27B for the simpler crawl/web-news
        # steps).
        model = str(raw.get("model", "")).strip()

        steps.append(
            StepDef(
                id=step_id,
                type=step_type,
                description=description,
                instructions=instructions,
                skill=skill,
                output_file=output_file,
                depends_on=depends_on,
                model=model,
            )
        )

    return steps


def _validate_dag(path: Path, steps: list[StepDef]) -> None:
    """Validate DAG: no cycles, no missing references."""
    step_ids = {s.id for s in steps}

    for step in steps:
        for dep in step.depends_on:
            if dep not in step_ids:
                raise WorkflowParseError(f"{path}: Step '{step.id}' depends_on unknown step '{dep}'")

    # Kahn's algorithm cycle detection
    in_degree: dict[str, int] = {s.id: 0 for s in steps}
    for step in steps:
        for dep in step.depends_on:
            in_degree[step.id] += 1

    dependents: dict[str, list[str]] = {s.id: [] for s in steps}
    for step in steps:
        for dep in step.depends_on:
            dependents[dep].append(step.id)

    queue = [sid for sid, deg in in_degree.items() if deg == 0]
    visited = 0
    while queue:
        sid = queue.pop()
        visited += 1
        for child in dependents[sid]:
            in_degree[child] -= 1
            if in_degree[child] == 0:
                queue.append(child)

    if visited != len(steps):
        raise WorkflowParseError(f"{path}: Cycle detected in workflow step dependencies")
