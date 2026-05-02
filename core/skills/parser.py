"""Pernix — SKILL.md parser: YAML frontmatter + markdown body."""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

logger = logging.getLogger("pernix.skills.parser")


class SkillParseError(Exception):
    """Raised when a SKILL.md file cannot be parsed."""


def parse_skill_md(path: Path) -> tuple[dict, str]:
    """Parse a SKILL.md file into (frontmatter_dict, body_markdown).

    Frontmatter is YAML between --- delimiters at the top of the file.
    Body is everything after the closing ---.

    Raises SkillParseError if the file is missing required fields.
    """
    text = path.read_text(encoding="utf-8")

    # Split frontmatter from body
    if not text.startswith("---"):
        raise SkillParseError(f"{path}: Missing YAML frontmatter (must start with ---)")

    parts = text.split("---", 2)
    if len(parts) < 3:
        raise SkillParseError(f"{path}: Malformed frontmatter (needs opening and closing ---)")

    # parts[0] is empty (before first ---), parts[1] is YAML, parts[2] is body
    raw_yaml = parts[1].strip()
    body = parts[2].strip()

    try:
        frontmatter = yaml.safe_load(raw_yaml)
    except yaml.YAMLError as e:
        raise SkillParseError(f"{path}: Invalid YAML frontmatter: {e}") from e

    if not isinstance(frontmatter, dict):
        raise SkillParseError(f"{path}: Frontmatter must be a YAML mapping")

    # Validate required fields (enforce string types — YAML may parse bare values as int/bool)
    name = frontmatter.get("name")
    if not name:
        raise SkillParseError(f"{path}: Missing required field 'name' in frontmatter")
    if not isinstance(name, str):
        frontmatter["name"] = str(name)
    desc = frontmatter.get("description")
    if not desc:
        raise SkillParseError(f"{path}: Missing required field 'description' in frontmatter")
    if not isinstance(desc, str):
        frontmatter["description"] = str(desc)

    # Validate name matches directory
    expected_name = path.parent.name
    if frontmatter["name"] != expected_name:
        logger.warning(
            "Skill name '%s' doesn't match directory '%s' in %s",
            frontmatter["name"],
            expected_name,
            path,
        )

    # Normalize tags
    tags = frontmatter.get("tags", [])
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    frontmatter["tags"] = tags

    # Default version
    frontmatter.setdefault("version", "1.0")

    return frontmatter, body
