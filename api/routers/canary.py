"""Pernix — Canary suite endpoints (adaptation plan 3.5)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from config import settings
from db import models as db

router = APIRouter(tags=["canary"])


@router.get("/api/canary")
async def list_canaries():
    """Suite definitions + per-task pass rates over the retention window."""
    import asyncio as _asyncio

    from core.canary import scan_canaries

    defs = await _asyncio.to_thread(scan_canaries)
    runs = await _asyncio.to_thread(db.list_canary_runs, None, None, 500)
    by_task: dict[str, dict] = {}
    for r in runs:
        s = by_task.setdefault(r["task"], {"runs": 0, "passed": 0, "last_run": None})
        s["runs"] += 1
        s["passed"] += 1 if r.get("passed") else 0
        if s["last_run"] is None:
            s["last_run"] = {
                "created_at": r.get("created_at"),
                "passed": bool(r.get("passed")),
                "outcome": r.get("outcome"),
                "trigger": r.get("trigger"),
                "duration_s": r.get("duration_s"),
            }
    return {
        "enabled": settings.canary_enabled,
        "schedule": settings.canary_schedule,
        "canaries": [
            {
                "name": d.name,
                "tags": d.tags,
                "flaky": d.flaky,
                "gates": [g["name"] for g in d.gates],
                "timeout": d.timeout,
                "last_reviewed": d.last_reviewed,
                "stats": by_task.get(d.name, {"runs": 0, "passed": 0, "last_run": None}),
            }
            for d in defs
        ],
    }


@router.get("/api/canary/runs")
async def list_runs(task: str = "", batch_id: str = "", limit: int = 50):
    import asyncio as _asyncio

    rows = await _asyncio.to_thread(db.list_canary_runs, task or None, batch_id or None, max(1, min(limit, 500)))
    return {"runs": rows, "count": len(rows)}


@router.post("/api/canary/run")
async def trigger_run(body: dict = {}):
    """Manual trigger: queue one canary (or the whole suite with name='*')."""
    if not settings.canary_enabled:
        raise HTTPException(400, detail="canary_enabled is off")
    from core.canary import load_canary
    from core.extensions.scheduling import _execute_canary_sweep_job, _get_scheduler, enqueue_manual_canary

    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, detail="name is required ('*' runs the whole suite)")
    if name == "*":
        scheduler = _get_scheduler()
        if not scheduler:
            raise HTTPException(503, detail="scheduler unavailable")
        from datetime import datetime, timezone

        from apscheduler.triggers.date import DateTrigger

        scheduler.add_job(
            _execute_canary_sweep_job,
            trigger=DateTrigger(run_date=datetime.now(timezone.utc)),
            id="_canary_manual_sweep",
            replace_existing=True,
            kwargs={"meta": {"kind": "canary", "transient": True, "trigger": "manual"}},
        )
        return {"queued": "*"}
    if load_canary(name) is None:
        raise HTTPException(404, detail=f"no canary named '{name}'")
    if not enqueue_manual_canary(name):
        raise HTTPException(503, detail="scheduler unavailable")
    return {"queued": name}
