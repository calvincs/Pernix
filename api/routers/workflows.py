"""Pernix — Workflow management API: CRUD, run history, skill improvement proposals."""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config import settings

router = APIRouter(tags=["workflows"])
logger = logging.getLogger("pernix.api.workflows")


def _wf_dir() -> Path:
    return Path("data/workflows")


# ---------------------------------------------------------------------------
# Workflow CRUD
# ---------------------------------------------------------------------------


@router.get("/api/workflows")
async def list_workflows():
    """List all installed workflows with metadata."""
    from core.workflows.registry import get_workflow_registry

    reg = get_workflow_registry()
    summaries = reg.all_summaries()
    return {"workflows": [s.to_dict() for s in sorted(summaries, key=lambda x: x.name)]}


class WorkflowValidate(BaseModel):
    content: str


@router.post("/api/workflows/validate")
async def validate_workflow(body: WorkflowValidate):
    """Validate raw WORKFLOW.md content without writing to disk."""
    from core.workflows.validator import validate_content

    result = validate_content(body.content, check_skills=True)
    return result.to_dict()


# NOTE: /api/workflows/proposals must be registered BEFORE /api/workflows/{name}
# to prevent FastAPI from matching "proposals" as a {name} path parameter.
# Proposals endpoints are declared below after workflow CRUD but are registered
# first via include_router since FastAPI resolves routes in declaration order.
# We solve this by using a unique path prefix for proposals (/proposals/ routes).
# The GET /api/workflows/proposals endpoint is declared later but won't conflict
# because FastAPI resolves literal path segments before parameterised ones only
# when the literal route is declared FIRST. We move it up here:


@router.get("/api/workflows/proposals")
async def list_proposals(
    skill_name: str = "",
    status: str = "pending",
    source_origin: str = "",
    limit: int = 50,
):
    """List skill improvement proposals. Defaults to pending proposals.

    `source_origin` filters between workflow-origin (post-workflow reflect) and
    session-origin (snooze_reflect on a regular session). Empty string = both.
    """
    from db import models as db

    proposals = db.list_skill_proposals(
        skill_name=skill_name or None,
        status=status or None,
        source_origin=source_origin or None,
        limit=limit,
    )
    return {"proposals": proposals}


@router.post("/api/workflows/proposals/{proposal_id}/approve")
async def approve_proposal(proposal_id: str):
    """Mark a proposal as approved (user will edit skill manually)."""
    from db import models as db

    ok = db.resolve_skill_proposal(proposal_id, "approved")
    if not ok:
        raise HTTPException(status_code=404, detail=f"Proposal '{proposal_id}' not found")
    return {"ok": True, "status": "approved"}


@router.post("/api/workflows/proposals/{proposal_id}/reject")
async def reject_proposal(proposal_id: str):
    """Mark a proposal as rejected."""
    from db import models as db

    ok = db.resolve_skill_proposal(proposal_id, "rejected")
    if not ok:
        raise HTTPException(status_code=404, detail=f"Proposal '{proposal_id}' not found")
    return {"ok": True, "status": "rejected"}


@router.post("/api/workflows/proposals/{proposal_id}/apply")
async def apply_proposal_route(proposal_id: str):
    """Apply a proposal to its target SKILL.md and mark it applied.

    User-gated only: this endpoint is triggered by the Apply button in the
    Workflows panel. The workflow runner never calls it. After apply, the
    user must re-invoke the workflow manually to validate the fix — we do not
    auto re-run, to avoid compounding cost on a misdiagnosed proposal.
    """
    from core.workflows.apply import ProposalApplyError, apply_proposal

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


@router.get("/api/workflows/{name}")
async def get_workflow(name: str):
    """Get full workflow definition including steps."""
    from core.workflows.registry import get_workflow_registry

    reg = get_workflow_registry()
    wf = reg.get(name)
    if not wf:
        raise HTTPException(status_code=404, detail=f"Workflow '{name}' not found")

    wf_md = wf.path / "WORKFLOW.md"
    raw_content = wf_md.read_text(encoding="utf-8") if wf_md.exists() else ""

    return {
        "name": wf.name,
        "description": wf.description,
        "version": wf.version,
        "tags": wf.tags,
        "body": wf.body,
        "raw_content": raw_content,
        "steps": [
            {
                "id": s.id,
                "type": s.type,
                "description": s.description,
                "instructions": s.instructions,
                "skill": s.skill,
                "output_file": s.output_file,
                "depends_on": s.depends_on,
            }
            for s in wf.steps
        ],
        "path": str(wf.path),
    }


class WorkflowCreate(BaseModel):
    content: str  # Full WORKFLOW.md content


@router.post("/api/workflows")
async def create_workflow(body: WorkflowCreate):
    """Create a new workflow by writing WORKFLOW.md to disk."""
    import os
    import tempfile

    from core.workflows.parser import WorkflowParseError, parse_workflow_md
    from core.workflows.registry import get_workflow_registry

    # Parse to validate before writing. Use NamedTemporaryFile and always
    # delete in finally — no explicit unlink in the except block.
    with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w", encoding="utf-8") as tf:
        tf.write(body.content)
        tmp_path = Path(tf.name)

    try:
        frontmatter, _ = parse_workflow_md(tmp_path)
    except WorkflowParseError as e:
        raise HTTPException(status_code=422, detail=str(e))
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    # Run full semantic validation (skill refs, DAG, step schema)
    from core.workflows.validator import validate_content

    vr = validate_content(body.content, check_skills=True)
    if not vr.valid:
        errors = [e["message"] for e in vr.to_dict()["errors"]]
        raise HTTPException(status_code=422, detail={"errors": errors})

    name = frontmatter["name"]
    wf_path = _wf_dir() / name
    wf_path.mkdir(parents=True, exist_ok=True)
    wf_md = wf_path / "WORKFLOW.md"
    wf_md.write_text(body.content, encoding="utf-8")

    reg = get_workflow_registry()
    reg.rescan(_wf_dir())
    logger.info("Workflow '%s' created via API", name)

    return {"ok": True, "name": name}


class WorkflowUpdate(BaseModel):
    content: str


@router.put("/api/workflows/{name}")
async def update_workflow(name: str, body: WorkflowUpdate):
    """Update a workflow's WORKFLOW.md content."""
    from core.workflows.registry import get_workflow_registry

    reg = get_workflow_registry()
    wf = reg.get(name)
    if not wf:
        raise HTTPException(status_code=404, detail=f"Workflow '{name}' not found")

    from core.workflows.validator import validate_content

    vr = validate_content(body.content, check_skills=True)
    if not vr.valid:
        errors = [e["message"] for e in vr.to_dict()["errors"]]
        raise HTTPException(status_code=422, detail={"errors": errors})

    wf_md = wf.path / "WORKFLOW.md"
    wf_md.write_text(body.content, encoding="utf-8")
    reg.rescan(_wf_dir())
    logger.info("Workflow '%s' updated via API", name)
    return {"ok": True}


@router.delete("/api/workflows/{name}")
async def delete_workflow(name: str):
    """Delete a workflow and its directory."""
    from core.workflows.registry import get_workflow_registry

    reg = get_workflow_registry()
    wf = reg.get(name)
    if not wf:
        raise HTTPException(status_code=404, detail=f"Workflow '{name}' not found")

    shutil.rmtree(wf.path)
    reg.rescan(_wf_dir())
    logger.info("Workflow '%s' deleted", name)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Run history
# ---------------------------------------------------------------------------


@router.get("/api/workflows/{name}/runs")
async def list_workflow_runs(name: str, limit: int = 20):
    """List past executions of a workflow, newest first."""
    from core.workflows.registry import get_workflow_registry

    reg = get_workflow_registry()
    if not reg.exists(name):
        raise HTTPException(status_code=404, detail=f"Workflow '{name}' not found")

    from db import models as db

    runs = db.list_workflow_runs(workflow_name=name, limit=limit)
    return {"runs": runs}


@router.get("/api/workflows/{name}/runs/{run_id}")
async def get_workflow_run(name: str, run_id: str):
    """Get full manifest JSON for a specific workflow run."""
    from db import models as db

    run = db.get_workflow_run(run_id)
    if not run or run.get("workflow_name") != name:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

    # Also try to read manifest from disk for step-level details
    run_dir = Path(settings.workspace_dir) / run["run_dir"]
    manifest = {}
    manifest_path = run_dir / "manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    return {"run": run, "manifest": manifest}


@router.delete("/api/workflows/{name}/runs/{run_id}")
async def delete_workflow_run(name: str, run_id: str):
    """Delete a workflow run directory and its DB row."""
    from db import models as db

    run = db.get_workflow_run(run_id)
    if not run or run.get("workflow_name") != name:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

    # Archive pending proposals
    db.archive_proposals_for_run(run_id)

    # Delete filesystem first; if it fails, leave the DB row so cleanup can retry.
    run_dir = Path(settings.workspace_dir) / run["run_dir"]
    if run_dir.exists():
        try:
            shutil.rmtree(run_dir)
        except OSError as e:
            raise HTTPException(
                status_code=500,
                detail=f"Could not delete run directory: {e}",
            )

    # Only delete the DB row once the filesystem is clean.
    db.delete_workflow_run(run_id)
    logger.info("Workflow run '%s/%s' deleted", name, run_id)
    return {"ok": True}
