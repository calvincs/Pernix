"""RLM run history endpoints (read-only).

Runs are created by the rlm_process tool (core/extensions/rlm); these routes
expose the rlm_runs audit index plus on-disk manifests, mirroring the
workflow-runs endpoints in workflows.py. Listing works even when
rlm_enabled=false — historical runs remain inspectable after a toggle-off.
"""

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException

from config import settings
from db import models as db

router = APIRouter()


@router.get("/api/rlm/runs")
async def list_rlm_runs(session_id: str = "", limit: int = 20):
    """List RLM runs, newest first. Optionally filter by owning session."""
    limit = max(1, min(int(limit), 100))
    runs = db.list_rlm_runs(session_id=session_id or None, limit=limit)
    return {"runs": runs, "rlm_enabled": settings.rlm_enabled}


@router.get("/api/rlm/runs/{run_id}")
async def get_rlm_run(run_id: str):
    """One run's row plus its on-disk manifest and artifact availability."""
    runs = db.list_rlm_runs(limit=1000)
    run = next((r for r in runs if r["run_id"] == run_id), None)
    if run is None:
        raise HTTPException(status_code=404, detail=f"RLM run '{run_id}' not found")

    # run_dir is workspace-relative and comes from our own DB row (never from
    # the caller), so joining it under the workspace is safe.
    run_dir = Path(settings.workspace_dir) / run["run_dir"]
    manifest = {}
    manifest_path = run_dir / "manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = {"error": "manifest unreadable"}

    return {
        **run,
        "manifest": manifest,
        "has_trace": (run_dir / "trace.jsonl").exists(),
        "trace_path": f"{run['run_dir']}/trace.jsonl",
        "answer_path": f"{run['run_dir']}/answer.txt" if (run_dir / "answer.txt").exists() else None,
    }
