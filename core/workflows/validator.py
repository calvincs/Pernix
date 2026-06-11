"""Pernix — Workflow validator: structured schema and semantic validation.

Used by:
  - validate_workflow() agent tool — confirms a workflow is runnable before execution
  - POST /api/workflows/validate — UI live validation
  - POST /api/workflows (create) and PUT (update) — write-time gate

Returns ValidationResult with typed errors so the agent can self-correct.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_VALID_ID_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]*$")


@dataclass
class ValidationIssue:
    severity: str  # "error" | "warning"
    type: str  # "parse_error" | "missing_field" | "invalid_step" | "missing_skill" | "dag_error" | "schema"
    message: str
    step_id: str | None = None
    field: str | None = None

    def to_dict(self) -> dict:
        d = {"severity": self.severity, "type": self.type, "message": self.message}
        if self.step_id:
            d["step_id"] = self.step_id
        if self.field:
            d["field"] = self.field
        return d


@dataclass
class ValidationResult:
    valid: bool
    issues: list[ValidationIssue] = field(default_factory=list)
    info: dict = field(default_factory=dict)  # step_count, wave_count, skill_refs, etc.

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    def to_dict(self) -> dict:
        return {
            "valid": self.valid,
            "errors": [e.to_dict() for e in self.errors],
            "warnings": [w.to_dict() for w in self.warnings],
            "info": self.info,
        }

    def to_agent_text(self) -> str:
        """Human-readable summary suitable for agent self-correction."""
        if self.valid:
            lines = [
                f"Workflow is valid. {self.info.get('step_count', 0)} step(s) in "
                f"{self.info.get('wave_count', 0)} execution wave(s)."
            ]
            waves = self.info.get("waves", [])
            for i, wave in enumerate(waves, 1):
                lines.append(f"  Wave {i}: {', '.join(wave)}")
            if self.info.get("skill_refs"):
                lines.append(f"Skills referenced: {', '.join(self.info['skill_refs'])}")
            missing = self.info.get("missing_skills", [])
            if missing:
                lines.append(f"WARNING: Skills not found in registry: {', '.join(missing)}")
            disabled = self.info.get("disabled_skills", [])
            if disabled:
                lines.append(f"WARNING: Skills are disabled (run_workflow will refuse): {', '.join(disabled)}")
            if self.warnings:
                for w in self.warnings:
                    lines.append(f"Warning: {w.message}")
            return "\n".join(lines)

        lines = [f"Workflow is INVALID — {len(self.errors)} error(s):"]
        for i, err in enumerate(self.errors, 1):
            loc = f"[step '{err.step_id}'] " if err.step_id else ""
            fld = f"field '{err.field}': " if err.field else ""
            lines.append(f"  {i}. {loc}{fld}{err.message}")
        if self.warnings:
            lines.append("Warnings:")
            for w in self.warnings:
                lines.append(f"  - {w.message}")
        lines.append("\nFix the issues above and call validate_workflow() again to confirm.")
        return "\n".join(lines)


def validate_content(content: str, check_skills: bool = True) -> ValidationResult:
    """Validate raw WORKFLOW.md content.

    check_skills: if True, verify referenced skills exist in the skill registry.
    Set to False for offline/test contexts where the registry isn't loaded.
    """
    import yaml

    issues: list[ValidationIssue] = []

    def err(type_: str, message: str, step_id=None, field=None):
        issues.append(ValidationIssue("error", type_, message, step_id, field))

    def warn(type_: str, message: str, step_id=None, field=None):
        issues.append(ValidationIssue("warning", type_, message, step_id, field))

    # 1. Parse frontmatter
    text = content.strip()
    if not text.startswith("---"):
        err("parse_error", "File must start with --- YAML frontmatter")
        return ValidationResult(valid=False, issues=issues)

    parts = text.split("---", 2)
    if len(parts) < 3:
        err("parse_error", "Missing closing --- after YAML frontmatter block")
        return ValidationResult(valid=False, issues=issues)

    raw_yaml = parts[1].strip()
    try:
        fm = yaml.safe_load(raw_yaml)
    except yaml.YAMLError as e:
        err("parse_error", f"YAML syntax error: {e}")
        return ValidationResult(valid=False, issues=issues)

    if not isinstance(fm, dict):
        err("parse_error", "Frontmatter must be a YAML key-value mapping")
        return ValidationResult(valid=False, issues=issues)

    # 2. Required top-level fields
    name = fm.get("name")
    if not name:
        err("missing_field", "Missing required field 'name'", field="name")
    elif not isinstance(name, str):
        err("schema", f"'name' must be a string, got {type(name).__name__}", field="name")

    if not fm.get("description"):
        err("missing_field", "Missing required field 'description'", field="description")

    raw_steps = fm.get("steps")
    if not raw_steps:
        err("missing_field", "Missing required field 'steps' (must be a non-empty list)", field="steps")
        return ValidationResult(valid=False, issues=issues)

    if not isinstance(raw_steps, list):
        err("schema", "'steps' must be a YAML list", field="steps")
        return ValidationResult(valid=False, issues=issues)

    if len(raw_steps) == 0:
        err("missing_field", "'steps' list is empty — workflow must have at least one step", field="steps")
        return ValidationResult(valid=False, issues=issues)

    # 3. Validate each step
    seen_ids: set[str] = set()
    step_defs: list[dict] = []
    skill_refs: set[str] = set()

    for i, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            err("invalid_step", f"Step {i} must be a YAML mapping (got {type(raw).__name__})")
            continue

        step_id = raw.get("id")
        if not step_id:
            err("invalid_step", f"Step {i} is missing required field 'id'", field="id")
            step_id = f"<step_{i}>"
        else:
            step_id = str(step_id)
            if step_id in seen_ids:
                err("invalid_step", f"Duplicate step id '{step_id}'", step_id=step_id, field="id")
            elif not _VALID_ID_RE.match(step_id):
                err(
                    "schema",
                    f"Step id '{step_id}' must start with a letter and contain only letters, digits, hyphens, underscores",
                    step_id=step_id,
                    field="id",
                )
        seen_ids.add(step_id)

        skill = raw.get("skill")
        if skill is not None:
            skill = str(skill)

        # Infer type
        step_type = str(raw.get("type", "skill" if skill else "instruction"))
        if step_type not in ("skill", "instruction"):
            err(
                "schema",
                f"Step type must be 'skill' or 'instruction', got '{step_type}'",
                step_id=step_id,
                field="type",
            )

        if step_type == "skill" and not skill:
            err(
                "invalid_step",
                f"Step '{step_id}' has type 'skill' but is missing the 'skill' field",
                step_id=step_id,
                field="skill",
            )

        if skill:
            skill_refs.add(skill)

        # output_file should be a bare filename, not a path
        output_file = raw.get("output_file", "")
        if output_file and ("/" in str(output_file) or "\\" in str(output_file)):
            warn(
                "schema",
                f"Step '{step_id}': output_file '{output_file}' contains path separators — "
                "use a bare filename; the executor places it in the run directory automatically",
                step_id=step_id,
                field="output_file",
            )

        if not raw.get("description") and not raw.get("instructions"):
            warn(
                "schema",
                f"Step '{step_id}' has neither 'description' nor 'instructions' — " "the worker will have no guidance",
                step_id=step_id,
            )

        depends_on_raw = raw.get("depends_on", [])
        if isinstance(depends_on_raw, str):
            depends_on = [depends_on_raw.strip()]
        else:
            depends_on = [str(d) for d in (depends_on_raw or [])]

        step_defs.append(
            {
                "id": step_id,
                "type": step_type,
                "skill": skill,
                "depends_on": depends_on,
            }
        )

    # 4. Validate DAG (depends_on references and cycles)
    if not any(i.type in ("parse_error", "schema") and i.field == "steps" for i in issues):
        for step in step_defs:
            for dep in step["depends_on"]:
                if dep not in seen_ids:
                    err(
                        "dag_error",
                        f"Step '{step['id']}' depends_on unknown step '{dep}'",
                        step_id=step["id"],
                        field="depends_on",
                    )

        # Cycle detection (Kahn's algorithm)
        in_degree: dict[str, int] = {s["id"]: 0 for s in step_defs}
        adjacency: dict[str, list[str]] = {s["id"]: [] for s in step_defs}
        for step in step_defs:
            for dep in step["depends_on"]:
                if dep in in_degree:
                    in_degree[step["id"]] += 1
                    adjacency[dep].append(step["id"])

        queue = [sid for sid, deg in in_degree.items() if deg == 0]
        visited = 0
        waves: list[list[str]] = []
        while queue:
            wave = sorted(queue)
            waves.append(wave)
            queue = []
            visited += len(wave)
            for sid in wave:
                for child in adjacency[sid]:
                    in_degree[child] -= 1
                    if in_degree[child] == 0:
                        queue.append(child)

        if visited != len(step_defs):
            err(
                "dag_error",
                "Cycle detected in step dependencies — workflow cannot execute. "
                "Review depends_on fields to remove circular references.",
            )
            waves = []
    else:
        waves = []

    # 5. Check skill references exist in registry AND are not disabled
    missing_skills: list[str] = []
    disabled_skills: list[str] = []
    if check_skills and skill_refs:
        try:
            from core.skills.registry import get_skill_registry

            reg = get_skill_registry()
            missing_skills = [s for s in skill_refs if not reg.exists(s)]
            for skill in missing_skills:
                warn(
                    "missing_skill",
                    f"Skill '{skill}' is referenced but not found in the registry. "
                    "The worker will still try to load it at runtime.",
                )
            disabled_skills = [s for s in skill_refs if reg.exists(s) and reg.is_disabled(s)]
            for skill in disabled_skills:
                warn(
                    "disabled_skill",
                    f"Skill '{skill}' is referenced but is currently disabled. "
                    "run_workflow will refuse to start until it is re-enabled in Explorer > Skills.",
                )
        except Exception:
            pass  # Registry unavailable — skip skill check, don't fail validation

    valid = not any(i.severity == "error" for i in issues)
    info = {
        "step_count": len(step_defs),
        "wave_count": len(waves),
        "waves": waves,
        "skill_refs": sorted(skill_refs),
        "missing_skills": missing_skills,
        "disabled_skills": disabled_skills,
    }
    return ValidationResult(valid=valid, issues=issues, info=info)


def validate_file(path: Path, check_skills: bool = True) -> ValidationResult:
    """Validate a WORKFLOW.md file on disk."""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as e:
        return ValidationResult(
            valid=False,
            issues=[ValidationIssue("error", "parse_error", f"Cannot read file: {e}")],
        )
    return validate_content(content, check_skills=check_skills)
