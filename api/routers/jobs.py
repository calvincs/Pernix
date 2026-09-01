"""Pernix — Jobs management endpoints: CRUD, status, SSE events."""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, Query

from api.streaming import sse_event, sse_response
from core.events import get_event_bus
from db import models as db

logger = logging.getLogger("pernix.api.jobs")
router = APIRouter(tags=["jobs"])

HEARTBEAT_INTERVAL = 30

# Strong refs for fire-and-forget test runs — asyncio holds only a weak
# reference to running tasks, so a bare create_task can be GC'd mid-flight.
_bg_tasks: set = set()


@router.get("/api/jobs")
async def list_jobs():
    """List all scheduled jobs with enriched status."""
    from core.extensions.scheduling import get_all_jobs_with_status

    jobs = get_all_jobs_with_status()
    return {"items": jobs, "count": len(jobs)}


@router.post("/api/jobs")
async def create_job(body: dict):
    """Create a new scheduled job."""
    name = body.get("name", "").strip()
    cron_expr = body.get("cron_expr", "").strip()
    prompt = body.get("prompt", "").strip()
    model = body.get("model", "")

    if not name:
        raise HTTPException(400, detail="name required")
    if not cron_expr:
        raise HTTPException(400, detail="cron_expr required")
    if not prompt:
        raise HTTPException(400, detail="prompt required")

    from core.extensions.scheduling import schedule_job

    # API creation is explicit: no calling session to inherit a space from,
    # so an absent/empty space_id means unbound ("none"), never "inherit".
    result = schedule_job(name, cron_expr, prompt, model=model, space_id=body.get("space_id") or "none")
    if result.startswith("Error"):
        raise HTTPException(400, detail=result)
    return {"status": "created", "name": name, "message": result}


@router.delete("/api/jobs/{name}")
async def delete_job(name: str):
    """Remove a scheduled job."""
    from core.extensions.scheduling import remove_scheduled_job

    result = remove_scheduled_job(name)
    if result.startswith("Error"):
        raise HTTPException(400, detail=result)
    return {"status": "deleted", "name": name}


@router.put("/api/jobs/{name}")
async def update_job(name: str, body: dict):
    """Update an existing scheduled job (reschedules with new parameters)."""
    from core.extensions.scheduling import update_scheduled_job

    result = update_scheduled_job(
        name,
        cron_expr=body.get("cron_expr"),
        prompt=body.get("prompt"),
        model=body.get("model"),
        space_id=body["space_id"] if "space_id" in body else None,
    )
    if result.startswith("Error"):
        raise HTTPException(400, detail=result)
    return {"status": "updated", "name": name, "message": result}


@router.post("/api/jobs/{name}/pause")
async def pause_job_endpoint(name: str):
    """Pause a scheduled job."""
    from core.extensions.scheduling import pause_job

    result = pause_job(name)
    if result.startswith("Error"):
        raise HTTPException(400, detail=result)
    return {"status": "paused", "name": name}


@router.post("/api/jobs/{name}/resume")
async def resume_job_endpoint(name: str):
    """Resume a paused job."""
    from core.extensions.scheduling import resume_job

    result = resume_job(name)
    if result.startswith("Error"):
        raise HTTPException(400, detail=result)
    return {"status": "resumed", "name": name}


@router.post("/api/jobs/{name}/validate")
async def validate_job_endpoint(name: str):
    """Re-validate a stored job's spec. Persists the result on the job."""
    from core.extensions.scheduling import _read_jobs_json, _update_job_field, validate_job_spec

    job = next((j for j in _read_jobs_json() if j["name"] == name), None)
    if job is None:
        raise HTTPException(404, detail=f"Job '{name}' not found")
    v = validate_job_spec(
        job.get("cron_expr", ""),
        job.get("prompt", ""),
        model=job.get("model", ""),
        allowed_tools=job.get("allowed_tools"),
    )
    _update_job_field(name, "validation", v)
    return {"name": name, "validation": v}


@router.post("/api/jobs/{name}/test")
async def test_job_endpoint(name: str):
    """Start an isolated dry-run of the job (spec Feature 7).

    Returns immediately — a test can take minutes. The outcome arrives as a
    `job.test_done` event on /api/jobs/events plus a bell notification, and
    is stamped on the job as `last_test`."""
    from core.extensions.scheduling import _read_jobs_json, run_job_test

    if not any(j["name"] == name for j in _read_jobs_json()):
        raise HTTPException(404, detail=f"Job '{name}' not found")

    async def _run_and_notify():
        from sessions.manager import get_manager

        try:
            result = await run_job_test(name)
        except Exception as e:
            logger.error("Job test '%s' crashed: %s", name, e)
            return
        try:
            title = f"Job test {'passed' if result.get('ok') else 'FAILED'}: {name}"
            body = (result.get("error") or result.get("answer_preview") or "completed cleanly")[:200]
            nid = await asyncio.to_thread(
                db.add_notification,
                session_id=result.get("session_id") or "",
                title=title,
                body=body,
                urgency="normal" if result.get("ok") else "high",
            )
            get_manager().broadcast(
                {
                    "type": "dialog.notification",
                    "notification_id": nid,
                    "title": title,
                    "body": body,
                    "urgency": "normal" if result.get("ok") else "high",
                    "source_session_id": result.get("session_id") or "",
                }
            )
        except Exception as e:
            logger.debug("Job test notification failed: %s", e)

    task = asyncio.create_task(_run_and_notify())
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return {"status": "test_started", "name": name}


@router.get("/api/jobs/runs")
async def list_runs(
    limit: int = Query(50),
    offset: int = Query(0),
    job_name: str | None = Query(None),
):
    """Paginated cron run history."""
    runs, total = db.list_cron_runs_paginated(
        limit=limit,
        offset=offset,
        job_name=job_name,
    )
    return {"items": runs, "total": total, "limit": limit, "offset": offset}


@router.delete("/api/jobs/runs")
async def clear_runs(job_name: str | None = Query(None)):
    """Clear completed/error run history. Preserves running jobs."""
    deleted = db.clear_cron_runs(job_name=job_name)
    return {"status": "cleared", "deleted": deleted}


@router.get("/api/jobs/status")
async def jobs_status():
    """Live status: running jobs, snooze state, next run times."""
    from core.extensions.scheduling import _get_scheduler
    from core.snooze import get_snooze

    running_runs = db.list_cron_runs(limit=20)
    running_count = sum(1 for r in running_runs if r.get("status") == "running")

    snooze = get_snooze()
    snooze_stats = snooze.get_stats()

    next_runs = []
    scheduler = _get_scheduler()
    if scheduler:
        try:
            for job in scheduler.get_jobs():
                meta = job.kwargs.get("meta", {})
                try:
                    next_time = str(job.next_run_time) if job.next_run_time else None
                except Exception:
                    next_time = None
                next_runs.append(
                    {
                        "name": job.id,
                        "next_run": next_time,
                        "paused": job.next_run_time is None,
                    }
                )
        except Exception:
            pass

    return {
        "running_jobs": running_count,
        "scheduled_count": len(next_runs),
        "snooze": snooze_stats,
        "next_runs": next_runs,
    }


@router.get("/api/jobs/events")
async def job_events():
    """Global SSE stream for job and snooze lifecycle events."""
    bus = get_event_bus()

    async def stream():
        queue = bus.subscribe()
        try:
            while True:
                # Match api/streaming.py: asyncio.timeout() over create_task
                # +wait. The latter created a Task that wasn't always cleaned
                # up on client disconnect, leaving "Task was destroyed but
                # it is pending" warnings on every reconnect.
                try:
                    async with asyncio.timeout(HEARTBEAT_INTERVAL):
                        event = await queue.get()
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                event_type = event.get("type", "message")
                clean = {k: v for k, v in event.items() if not k.startswith("_")}
                yield sse_event(event_type, clean)
        except (asyncio.CancelledError, GeneratorExit):
            pass
        finally:
            bus.unsubscribe(queue)

    return sse_response(stream())


# ---------------------------------------------------------------------------
# User heartbeat (adaptation plan 3c) — one per session, API-only surface.
# The agent's set_heartbeat/clear_heartbeat tools operate on a separate
# owner namespace and can never see or modify this one.
# ---------------------------------------------------------------------------


@router.get("/api/sessions/{session_id}/heartbeat")
async def get_heartbeat(session_id: str):
    from core.extensions.scheduling import get_user_heartbeat

    hb = get_user_heartbeat(session_id)
    return {"heartbeat": hb}


@router.put("/api/sessions/{session_id}/heartbeat")
async def put_heartbeat(session_id: str, body: dict):
    from config import settings as _settings
    from core.extensions.scheduling import set_user_heartbeat

    if not _settings.heartbeats_enabled:
        return {"error": "heartbeats are disabled (settings.heartbeats_enabled)"}
    instruction = (body or {}).get("instruction", "").strip()
    if not instruction:
        return {"error": "instruction is required"}
    result = set_user_heartbeat(
        session_id,
        instruction,
        every=(body or {}).get("every", "5m"),
        delivery=(body or {}).get("delivery", "steer"),
    )
    if result.startswith("Error:"):
        return {"error": result}
    return {"ok": True, "job_id": result}


@router.delete("/api/sessions/{session_id}/heartbeat")
async def delete_heartbeat(session_id: str):
    from core.extensions.scheduling import clear_user_heartbeat

    return {"ok": clear_user_heartbeat(session_id)}
