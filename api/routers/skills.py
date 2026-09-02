"""Pernix — Skills management API: list, view, enable/disable, delete.

Disabled-skill state is owned by ``SkillRegistry`` (see
``core/skills/registry.py``). This router only proxies to the registry's
``is_disabled`` / ``enable`` / ``disable`` methods so there is one source of
truth for what's disabled — same JSON file, same in-memory set, used by every
agent path (scout, builtins, agent loop).
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from config import settings

router = APIRouter(tags=["skills"])
logger = logging.getLogger("pernix.api.skills")

# Mirrors api/routers/workspace.py: the SKILL.md editor is the same editor,
# so an edit racing the agent's own rewrite has to fail the same way.
MTIME_TOLERANCE_S = 0.5


@router.get("/api/skills")
async def list_skills():
    """List all installed skills with metadata, enabled state, and performance."""
    from core.signals import from_row
    from core.skills.registry import get_skill_registry
    from db import models as db

    reg = get_skill_registry()
    reg.rescan(Path(settings.skills_dir))
    skills = reg.all_skills()

    # Batch-load performance counters for all skills
    skill_names = [s.name for s in skills]
    perf_rows = db.get_signals_by_subjects([("skill", n) for n in skill_names])
    perf_map = {r["subject"]: from_row(r).to_display() for r in perf_rows}

    # Batch-load pending-proposal counts so the UI can flag rows without
    # an N+1 query. Skills with no pending proposals are simply absent
    # from the map (treated as 0 below).
    pending_counts = db.get_pending_proposal_counts_by_skill()

    result = []
    for s in sorted(skills, key=lambda x: x.name):
        # include_disabled=True so disabled skills still expose their resource
        # tree to the UI's edit/inspect view.
        resources = reg.list_resources(s.name, include_disabled=True)
        result.append(
            {
                "name": s.name,
                "description": s.description,
                "version": s.version,
                "tags": s.tags,
                "enabled": not reg.is_disabled(s.name),
                "valid": reg.is_valid(s.name),
                "validation_issues": reg.validation_issues(s.name),
                "has_scripts": "scripts" in resources,
                "has_references": "references" in resources,
                "has_assets": "assets" in resources,
                "resources": resources,
                "performance": perf_map.get(s.name),  # None if no observations yet
                "pending_proposals_count": pending_counts.get(s.name, 0),
            }
        )

    return {"skills": result}


# ---------------------------------------------------------------------------
# Skill improvement proposals
# ---------------------------------------------------------------------------
# Written by reflect/refine when a skill visibly under-performs, then reviewed
# by a human here. These endpoints were served under /api/workflows/proposals
# until the workflow engine was removed — they only ever shared that router by
# accident of where post-run reflect happened to live. The proposals themselves
# target SKILL.md files, so they belong here.
#
# Declared BEFORE the parameterised /api/skills/{name} routes below. Starlette
# matches in DECLARATION order, not by specificity, so with these last a GET of
# /api/skills/proposals binds name="proposals" and 404s as a missing skill.


@router.get("/api/skills/proposals")
async def list_proposals(
    skill_name: str = "",
    status: str = "pending",
    source_origin: str = "",
    limit: int = 50,
):
    """List skill improvement proposals. Defaults to pending proposals.

    `source_origin` filters by what produced the proposal ("session" for
    post-turn reflect, "refine" for the authoring pass). Empty string = all.
    """
    from db import models as db

    proposals = db.list_skill_proposals(
        skill_name=skill_name or None,
        status=status or None,
        source_origin=source_origin or None,
        limit=limit,
    )
    return {"proposals": proposals}


@router.post("/api/skills/proposals/{proposal_id}/approve")
async def approve_proposal(proposal_id: str):
    """Mark a proposal as approved (user will edit the skill manually)."""
    from db import models as db

    ok = db.resolve_skill_proposal(proposal_id, "approved")
    if not ok:
        raise HTTPException(status_code=404, detail=f"Proposal '{proposal_id}' not found")
    return {"ok": True, "status": "approved"}


@router.post("/api/skills/proposals/{proposal_id}/reject")
async def reject_proposal(proposal_id: str):
    """Mark a proposal as rejected."""
    from db import models as db

    ok = db.resolve_skill_proposal(proposal_id, "rejected")
    if not ok:
        raise HTTPException(status_code=404, detail=f"Proposal '{proposal_id}' not found")
    return {"ok": True, "status": "rejected"}


@router.post("/api/skills/proposals/{proposal_id}/apply")
async def apply_proposal_route(proposal_id: str):
    """Apply a proposal to its target SKILL.md and mark it applied.

    User-gated only. Nothing re-runs afterwards: the user re-invokes the skill
    themselves, so a misdiagnosed proposal cannot compound into automatic cost.
    """
    from core.skills.proposals import ProposalApplyError, apply_proposal

    try:
        result = apply_proposal(proposal_id)
    except ProposalApplyError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("apply_proposal failed for %s", proposal_id)
        raise HTTPException(status_code=500, detail=f"Internal error: {e}")
    return {
        "ok": True,
        "status": "applied",
        "proposal_id": result.proposal_id,
        "skill_name": result.skill_name,
        "skill_md_path": result.skill_md_path,
        "section": result.section,
        "section_existed": result.section_existed,
        "bytes_before": result.bytes_before,
        "bytes_after": result.bytes_after,
    }


@router.get("/api/skills/{name}")
async def get_skill(name: str):
    """Get full skill details including instructions."""
    from core.skills.registry import get_skill_registry

    reg = get_skill_registry()
    skill = reg.get(name)
    if not skill:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")

    # include_disabled=True so a toggled-off skill still shows its body in the
    # UI editor (the agent paths use the default-filtered overload).
    instructions = reg.load_instructions(name, include_disabled=True) or ""
    resources = reg.list_resources(name, include_disabled=True)

    # Read SKILL.md raw content for editing
    skill_md = skill.path / "SKILL.md"
    raw_content = skill_md.read_text(encoding="utf-8") if skill_md.exists() else ""
    # Handed back as base_mtime on save — see update_skill below.
    try:
        mtime = skill_md.stat().st_mtime if skill_md.exists() else None
    except OSError:
        mtime = None

    return {
        "name": skill.name,
        "description": skill.description,
        "version": skill.version,
        "tags": skill.tags,
        "enabled": not reg.is_disabled(name),
        "instructions": instructions,
        "raw_content": raw_content,
        "mtime": mtime,
        "resources": resources,
        "path": str(skill.path),
    }


class SkillUpdate(BaseModel):
    content: str
    base_mtime: float | None = None


@router.put("/api/skills/{name}")
async def update_skill(name: str, body: SkillUpdate):
    """Update a skill's SKILL.md content."""
    from core.skills.registry import get_skill_registry

    reg = get_skill_registry()
    skill = reg.get(name)
    if not skill:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")

    skill_md = skill.path / "SKILL.md"
    # Optimistic concurrency, opt-in: only a caller that read the file sends
    # base_mtime, so every other writer keeps last-writer-wins.
    if body.base_mtime is not None and skill_md.is_file():
        try:
            current = skill_md.stat().st_mtime
        except OSError:
            current = None
        if current is not None and abs(current - body.base_mtime) > MTIME_TOLERANCE_S:
            return JSONResponse(status_code=409, content={"detail": "changed_on_disk", "mtime": current})
    skill_md.write_text(body.content, encoding="utf-8")

    # Rescan to pick up changes
    reg.rescan(Path(settings.skills_dir))
    logger.info("Skill '%s' updated via API", name)

    return {"ok": True, "mtime": skill_md.stat().st_mtime}


class SkillToggle(BaseModel):
    enabled: bool


@router.patch("/api/skills/{name}")
async def toggle_skill(name: str, body: SkillToggle):
    """Enable or disable a skill."""
    from core.skills.registry import get_skill_registry

    reg = get_skill_registry()
    if not reg.exists(name):
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")

    if body.enabled:
        reg.enable(name)
    else:
        reg.disable(name)

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

    # Clear from disabled set first (idempotent — no-op if not disabled)
    # so a future skill of the same name doesn't inherit a stale disabled flag.
    reg.enable(name)

    # Rescan registry
    reg.rescan(Path(settings.skills_dir))
    logger.info("Skill '%s' deleted", name)

    return {"ok": True}
