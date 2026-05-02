"""Pernix — Skills management API: list, view, enable/disable, delete."""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config import settings

router = APIRouter(tags=["skills"])
logger = logging.getLogger("pernix.api.skills")


def _disabled_path() -> Path:
    return Path(settings.skills_dir) / ".disabled.json"


def _load_disabled() -> set[str]:
    path = _disabled_path()
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text()))
    except (json.JSONDecodeError, OSError):
        return set()


def _save_disabled(names: set[str]) -> None:
    _disabled_path().write_text(json.dumps(sorted(names)), encoding="utf-8")


@router.get("/api/skills")
async def list_skills():
    """List all installed skills with metadata, enabled state, and performance."""
    from core.signals import from_row
    from core.skills.registry import get_skill_registry
    from db import models as db

    reg = get_skill_registry()
    reg.rescan(Path(settings.skills_dir))
    skills = reg.all_skills()
    disabled = _load_disabled()

    # Batch-load performance counters for all skills
    skill_names = [s.name for s in skills]
    perf_rows = db.get_signals_by_subjects([("skill", n) for n in skill_names])
    perf_map = {r["subject"]: from_row(r).to_display() for r in perf_rows}

    result = []
    for s in sorted(skills, key=lambda x: x.name):
        resources = reg.list_resources(s.name)
        result.append(
            {
                "name": s.name,
                "description": s.description,
                "version": s.version,
                "tags": s.tags,
                "enabled": s.name not in disabled,
                "has_scripts": "scripts" in resources,
                "has_references": "references" in resources,
                "has_assets": "assets" in resources,
                "resources": resources,
                "performance": perf_map.get(s.name),  # None if no observations yet
            }
        )

    return {"skills": result}


@router.get("/api/skills/{name}")
async def get_skill(name: str):
    """Get full skill details including instructions."""
    from core.skills.registry import get_skill_registry

    reg = get_skill_registry()
    skill = reg.get(name)
    if not skill:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")

    instructions = reg.load_instructions(name) or ""
    resources = reg.list_resources(name)
    disabled = _load_disabled()

    # Read SKILL.md raw content for editing
    skill_md = skill.path / "SKILL.md"
    raw_content = skill_md.read_text(encoding="utf-8") if skill_md.exists() else ""

    return {
        "name": skill.name,
        "description": skill.description,
        "version": skill.version,
        "tags": skill.tags,
        "enabled": skill.name not in disabled,
        "instructions": instructions,
        "raw_content": raw_content,
        "resources": resources,
        "path": str(skill.path),
    }


class SkillUpdate(BaseModel):
    content: str


@router.put("/api/skills/{name}")
async def update_skill(name: str, body: SkillUpdate):
    """Update a skill's SKILL.md content."""
    from core.skills.registry import get_skill_registry

    reg = get_skill_registry()
    skill = reg.get(name)
    if not skill:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")

    skill_md = skill.path / "SKILL.md"
    skill_md.write_text(body.content, encoding="utf-8")

    # Rescan to pick up changes
    reg.rescan(Path(settings.skills_dir))
    logger.info("Skill '%s' updated via API", name)

    return {"ok": True}


class SkillToggle(BaseModel):
    enabled: bool


@router.patch("/api/skills/{name}")
async def toggle_skill(name: str, body: SkillToggle):
    """Enable or disable a skill."""
    from core.skills.registry import get_skill_registry

    reg = get_skill_registry()
    if not reg.exists(name):
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")

    disabled = _load_disabled()
    if body.enabled:
        disabled.discard(name)
    else:
        disabled.add(name)
    _save_disabled(disabled)

    logger.info("Skill '%s' %s", name, "enabled" if body.enabled else "disabled")
    return {"ok": True, "enabled": body.enabled}


@router.delete("/api/skills/{name}")
async def delete_skill(name: str):
    """Delete a skill permanently."""
    from core.skills.registry import get_skill_registry

    reg = get_skill_registry()
    skill = reg.get(name)
    if not skill:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")

    # Remove from filesystem
    shutil.rmtree(skill.path)

    # Remove from disabled list if present
    disabled = _load_disabled()
    disabled.discard(name)
    _save_disabled(disabled)

    # Rescan registry
    reg.rescan(Path(settings.skills_dir))
    logger.info("Skill '%s' deleted", name)

    return {"ok": True}
