"""Pernix — Scheduling extension: cron job management via APScheduler."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from config import settings
from db import models as db

logger = logging.getLogger("pernix.ext.scheduling")

CRON_PATH = Path("data/cron_jobs.json")

# APScheduler instance (lazy init)
_scheduler = None
_json_lock = threading.Lock()
_init_lock = threading.Lock()


def _get_scheduler():
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    with _init_lock:
        # Double-check after acquiring lock
        if _scheduler is not None:
            return _scheduler
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler

            _scheduler = AsyncIOScheduler(timezone="UTC")
            _scheduler.start()
            _load_jobs()
            logger.info("APScheduler started")
        except ImportError:
            logger.warning("APScheduler not installed, scheduling disabled")
        except RuntimeError as e:
            logger.warning("Scheduler init failed (no event loop): %s", e)
    return _scheduler


def init_scheduler():
    """Initialize the scheduler on the main event loop (call from app startup)."""
    _get_scheduler()


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


# Entry keys that are positional args or derived state, not meta payload.
# Everything else in a persisted entry round-trips through extra_meta so
# variant fields (kind, last_fired_at, workflow_name, ...) survive restarts.
_ENTRY_STRUCTURAL_KEYS = frozenset({"name", "cron_expr", "prompt", "session_id", "model", "cron_trigger", "paused"})


def _load_jobs():
    """Load persisted jobs from JSON."""
    if not CRON_PATH.exists():
        return
    try:
        jobs = json.loads(CRON_PATH.read_text())
        for job in jobs:
            # Round-trip every non-structural field verbatim — dropping
            # unknown keys here silently erased job variants on restart.
            extra = {k: v for k, v in job.items() if k not in _ENTRY_STRUCTURAL_KEYS}
            _add_job_internal(
                job["name"],
                job["cron_expr"],
                job["prompt"],
                session_id=job.get("session_id"),
                model=job.get("model", ""),
                extra_meta=extra,
            )
            # Restore paused state
            if job.get("paused") and _scheduler:
                try:
                    _scheduler.pause_job(job["name"])
                except Exception:
                    pass
        logger.info("Loaded %d cron jobs", len(jobs))
        _schedule_coalesced_catchup(jobs)
    except Exception as e:
        logger.warning("Failed to load cron jobs: %s", e)


def _save_jobs():
    """Persist all jobs to JSON."""
    scheduler = _get_scheduler()
    if not scheduler:
        return
    with _json_lock:
        jobs = []
        for job in scheduler.get_jobs():
            meta = job.kwargs.get("meta", {})
            # One-shot catch-up dispatches (coalesced missed runs) are not
            # real jobs — persisting one would resurrect it as a recurring
            # job on the next restart.
            if meta.get("transient"):
                continue
            entry = {
                "name": job.id,
                "cron_expr": meta.get("cron_expr", ""),
                "cron_trigger": str(job.trigger),
                "prompt": meta.get("prompt", ""),
                "model": meta.get("model", ""),
                "session_id": meta.get("session_id"),
                "session_mode": meta.get("session_mode", "fresh"),
                "paused": job.next_run_time is None,
                "created_at": meta.get("created_at", ""),
            }
            # Round-trip every remaining meta key verbatim (workflow_name,
            # last_fired_at, kind, ...) — the load side mirrors this.
            for k, v in meta.items():
                if k not in entry and k != "name":
                    entry[k] = v
            jobs.append(entry)
        CRON_PATH.parent.mkdir(parents=True, exist_ok=True)
        CRON_PATH.write_text(json.dumps(jobs, indent=2))


def _update_job_field(name: str, field: str, value) -> None:
    """Update a single field on a persisted job."""
    with _json_lock:
        if not CRON_PATH.exists():
            return
        jobs = json.loads(CRON_PATH.read_text())
        for job in jobs:
            if job["name"] == name:
                job[field] = value
                break
        CRON_PATH.write_text(json.dumps(jobs, indent=2))


def _read_jobs_json() -> list[dict]:
    """Read persisted jobs from JSON (no scheduler needed)."""
    if not CRON_PATH.exists():
        return []
    try:
        return json.loads(CRON_PATH.read_text())
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Claim-before-deliver + missed-run coalescing (adaptation plan 1c)
# ---------------------------------------------------------------------------


def _count_missed_fires(cron_expr: str, last_fired_iso: str, now: datetime, cap: int = 1000) -> int:
    """Count scheduled fires strictly after last_fired and at/before now.

    Pure computation over the cron expression — used at startup to decide
    whether downtime swallowed any ticks. Capped so a years-stale
    last_fired_at on a every-minute job can't spin."""
    from apscheduler.triggers.cron import CronTrigger

    try:
        trigger = CronTrigger.from_crontab(cron_expr, timezone="UTC")
        prev = datetime.fromisoformat(last_fired_iso)
    except (ValueError, TypeError):
        return 0
    if prev.tzinfo is None:
        prev = prev.replace(tzinfo=timezone.utc)
    missed = 0
    while missed < cap:
        nxt = trigger.get_next_fire_time(prev, prev)
        if nxt is None or nxt > now:
            break
        missed += 1
        prev = nxt
    return missed


def _schedule_coalesced_catchup(job_entries: list[dict]) -> None:
    """After loading jobs at startup, dispatch AT MOST ONE catch-up run per
    job whose schedule fired while the server was down.

    APScheduler adds loaded jobs fresh (next fire is in the future), so
    missed ticks are entirely our responsibility — and a crash can therefore
    never double-fire. The catch-up run is a transient one-shot: never
    persisted (see _save_jobs), and the parent's last_fired_at is advanced
    BEFORE dispatch so a crash during catch-up can't re-coalesce the same
    span (the claimed row -> 'uncertain' sweep covers honesty)."""
    scheduler = _scheduler
    if not scheduler:
        return
    now = datetime.now(timezone.utc)
    advanced = False
    for entry in job_entries:
        name = entry.get("name", "")
        expr = entry.get("cron_expr") or ""
        job = scheduler.get_job(name) if name else None
        if job is None or not expr or entry.get("paused"):
            continue
        # Only plain prompt jobs — workflow jobs run a different callable and
        # coalescing them through _execute_cron_job would misroute them.
        if job.func is not _execute_cron_job:
            continue
        meta = job.kwargs.get("meta", {})
        last = meta.get("last_fired_at")
        if not last:
            # Legacy job predating 1c: baseline now, no catch-up (there is
            # no honest way to know what it missed).
            meta["last_fired_at"] = now.isoformat()
            advanced = True
            continue
        missed = _count_missed_fires(expr, last, now)
        if missed < 1:
            continue
        meta["last_fired_at"] = now.isoformat()
        advanced = True
        co_meta = dict(meta)
        co_meta["transient"] = True
        co_meta["prompt"] = (
            f"[coalesced {missed} missed run(s) since {last}; the server was down "
            f"across the scheduled time(s) — this single run stands in for all of them]\n\n"
        ) + meta.get("prompt", "")
        from apscheduler.triggers.date import DateTrigger

        scheduler.add_job(
            _execute_cron_job,
            trigger=DateTrigger(run_date=now),
            id=f"{name}__coalesced",
            replace_existing=True,
            misfire_grace_time=300,
            kwargs={"meta": co_meta},
        )
        logger.info("Job '%s': coalesced %d missed run(s) since %s into one catch-up", name, missed, last)
    if advanced:
        _save_jobs()


def reconcile_cron_runs() -> int:
    """Startup sweep: mark claimed/running rows 'uncertain', notify the user.

    Must be called BEFORE init_scheduler() so no job fires into a
    half-reconciled table. Uncertain runs are reported, never replayed."""
    affected = db.reconcile_uncertain_cron_runs()
    if affected:
        names = ", ".join(sorted({r["job_name"] for r in affected}))
        db.add_notification(
            session_id="",
            title=f"{len(affected)} cron run(s) uncertain after restart",
            body=(
                f"Jobs: {names}. The server restarted mid-run; each outcome is "
                f"unknown and was NOT re-run. Check the job history and re-run "
                f"manually if needed."
            ),
            urgency="high",
        )
        logger.warning("Marked %d cron run(s) uncertain at startup: %s", len(affected), names)
    return len(affected)


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------


def _add_job_internal(
    name: str,
    cron_expr: str,
    prompt: str,
    session_id: str | None = None,
    model: str = "",
    extra_meta: dict | None = None,
):
    """Internal: add job to APScheduler."""
    from apscheduler.triggers.cron import CronTrigger

    scheduler = _get_scheduler()
    if not scheduler:
        return

    meta = {
        "name": name,
        "cron_expr": cron_expr,
        "prompt": prompt,
        "model": model,
        "session_id": session_id,
        "session_mode": "fresh",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra_meta:
        meta.update(extra_meta)

    trigger = CronTrigger.from_crontab(cron_expr)
    scheduler.add_job(
        _execute_cron_job,
        trigger=trigger,
        id=name,
        replace_existing=True,
        misfire_grace_time=300,
        kwargs={"meta": meta},
    )


def _notify_job_failure(manager, bus, job_name: str, session_id: str | None, error: str):
    """Broadcast a dialog.notification for job failures so push/webhook fire."""
    notification = {
        "type": "dialog.notification",
        "title": f"Job failed: {job_name}",
        "body": error[:200],
        "urgency": "high",
        "source_session_id": session_id or "",
    }
    nid = db.add_notification(
        session_id=session_id or "",
        title=notification["title"],
        body=notification["body"],
        urgency="high",
    )
    notification["notification_id"] = nid
    manager.broadcast(notification)
    bus.emit({**notification, "session_id": session_id or ""})


async def _execute_cron_job(meta: dict):
    """Execute a cron job by creating/reusing a session and sending the prompt."""
    from core.events import get_event_bus
    from core.snooze import get_snooze
    from sessions.manager import get_manager

    get_snooze().request_cancel()
    manager = get_manager()
    bus = get_event_bus()

    name = meta["name"]
    prompt = meta["prompt"]
    session_id = meta.get("session_id")
    model = meta.get("model", "")

    start_time = time.time()

    # Claim BEFORE dispatch (adaptation plan 1c): the run row (status=claimed,
    # fire_time) and the advanced last_fired_at hit disk before the prompt is
    # sent, so a crash anywhere past this point surfaces as an 'uncertain' run
    # at next startup — reported, never replayed.
    fire_time = datetime.now(timezone.utc).isoformat()
    run_id = db.add_cron_run(name, session_id, status="claimed", fire_time=fire_time)
    meta["last_fired_at"] = fire_time  # meta is the live APScheduler job's dict
    try:
        _save_jobs()
    except Exception as e:  # persistence best-effort; the DB claim is the record
        logger.warning("Failed to persist last_fired_at for '%s': %s", name, e)

    bus.emit({"type": "job.started", "job_name": name, "session_id": session_id, "run_id": run_id})

    try:
        if not session_id:
            # session_type="cron" is what _is_unattended_session() keys off —
            # without it scheduled jobs hit the dangerous-tool approval gate,
            # call ask_user into the void, and wedge in AWAITING_USER forever.
            # Jobs reusing an explicit session_id keep that session's type:
            # a user-attended session shouldn't lose its gate just because a
            # job also runs in it.
            session_id = manager.create_session(title=f"Cron: {name}", session_type="cron")

        # Set model override if specified
        session = manager.get(session_id)
        if session and model:
            session.model_override = model

        db.update_cron_run(run_id, "running")
        cron_timeout = settings.tool_timeout * settings.max_tool_rounds
        await asyncio.wait_for(
            manager.prompt(session_id, prompt),
            timeout=cron_timeout,
        )

        duration_ms = int((time.time() - start_time) * 1000)
        db.update_cron_run(run_id, "completed")
        bus.emit(
            {
                "type": "job.completed",
                "job_name": name,
                "session_id": session_id,
                "run_id": run_id,
                "duration_ms": duration_ms,
            }
        )
    except Exception as e:
        logger.error("Cron job '%s' failed: %s", name, e)
        db.update_cron_run(run_id, "error", str(e))
        bus.emit({"type": "job.error", "job_name": name, "session_id": session_id, "error": str(e)})
        # Notify user about the failure via push/webhook
        _notify_job_failure(manager, bus, name, session_id, str(e))
    finally:
        # Clear model override for reused sessions
        if session_id and model:
            session = manager.get(session_id)
            if session:
                session.model_override = None
        get_snooze().notify_activity()


async def _init_scheduler_async():
    """Initialize scheduler from async context (main event loop)."""
    _get_scheduler()


# ---------------------------------------------------------------------------
# Tool functions
# ---------------------------------------------------------------------------


def schedule_job(
    name: str, cron_expr: str, prompt: str, session_id: str = "", model: str = "", _context: dict | None = None
) -> str:
    """Schedule a recurring job with cron expression."""
    import asyncio

    from apscheduler.triggers.cron import CronTrigger

    # Validate cron expression early for a clear error message
    try:
        CronTrigger.from_crontab(cron_expr)
    except (ValueError, KeyError) as e:
        return f"Error: Invalid cron expression '{cron_expr}': {e}"

    ctx = _context or {}
    loop = ctx.get("_loop")

    try:
        if loop and not _scheduler:
            future = asyncio.run_coroutine_threadsafe(_init_scheduler_async(), loop)
            future.result(timeout=10)

        _add_job_internal(name, cron_expr, prompt, session_id=session_id or None, model=model)
        _save_jobs()
        return f"Job '{name}' scheduled: {cron_expr}"
    except Exception as e:
        return f"Error scheduling job: {e}"


def update_scheduled_job(
    name: str,
    cron_expr: str | None = None,
    prompt: str | None = None,
    model: str | None = None,
    _context: dict | None = None,
) -> str:
    """Update an existing scheduled job. Only provided fields are changed."""
    scheduler = _get_scheduler()
    if not scheduler:
        return "Scheduler not available"

    # Read current job from JSON
    saved = _read_jobs_json()
    current = next((j for j in saved if j["name"] == name), None)
    if not current:
        return f"Error: Job '{name}' not found"

    # Merge changes
    new_cron = cron_expr if cron_expr is not None else current.get("cron_expr", "")
    new_prompt = prompt if prompt is not None else current.get("prompt", "")
    new_model = model if model is not None else current.get("model", "")
    was_paused = current.get("paused", False)

    try:
        # Re-add with updated parameters (replace_existing=True in _add_job_internal)
        _add_job_internal(name, new_cron, new_prompt, session_id=current.get("session_id"), model=new_model)
        # Restore paused state if it was paused
        if was_paused:
            scheduler.pause_job(name)
        _save_jobs()
        return f"Job '{name}' updated"
    except Exception as e:
        return f"Error updating job: {e}"


def set_job_state(name: str, paused: bool, _context: dict | None = None) -> str:
    """Pause or resume a scheduled cron job."""
    if paused:
        return pause_job(name, _context=_context)
    return resume_job(name, _context=_context)


def list_scheduled_jobs(_context: dict | None = None) -> str:
    """List all scheduled jobs with next run times."""
    scheduler = _get_scheduler()

    # Try live scheduler first
    if scheduler:
        try:
            jobs = scheduler.get_jobs()
            if jobs:
                lines = []
                for job in jobs:
                    meta = job.kwargs.get("meta", {})
                    try:
                        next_run = str(job.next_run_time) if job.next_run_time else "paused"
                    except Exception:
                        next_run = "unknown"
                    model = meta.get("model", "")
                    model_str = f" model={model}" if model else ""
                    lines.append(
                        f"- {job.id}: cron={meta.get('cron_expr', '')} "
                        f"next={next_run}{model_str} "
                        f"prompt=\"{meta.get('prompt', '')[:60]}\""
                    )
                return "\n".join(lines)
        except Exception as e:
            logger.debug("Live scheduler query failed, falling back to JSON: %s", e)

    # Fallback: read from persisted JSON
    saved = _read_jobs_json()
    if not saved:
        return "No scheduled jobs."
    lines = []
    for job in saved:
        paused = " [paused]" if job.get("paused") else ""
        model = f" model={job['model']}" if job.get("model") else ""
        lines.append(
            f"- {job['name']}: cron={job.get('cron_expr', '')}{paused}{model} "
            f"prompt=\"{job.get('prompt', '')[:60]}\""
        )
    return "\n".join(lines)


def remove_scheduled_job(name: str, _context: dict | None = None) -> str:
    """Remove a scheduled job."""
    scheduler = _get_scheduler()
    if not scheduler:
        return "Scheduler not available"
    try:
        scheduler.remove_job(name)
        _save_jobs()
        return f"Job '{name}' removed"
    except Exception as e:
        return f"Error removing job: {e}"


def pause_job(name: str, _context: dict | None = None) -> str:
    """Pause a scheduled job."""
    scheduler = _get_scheduler()
    if not scheduler:
        return "Scheduler not available"
    try:
        scheduler.pause_job(name)
        _update_job_field(name, "paused", True)
        return f"Job '{name}' paused"
    except Exception as e:
        return f"Error pausing job: {e}"


def resume_job(name: str, _context: dict | None = None) -> str:
    """Resume a paused job."""
    scheduler = _get_scheduler()
    if not scheduler:
        return "Scheduler not available"
    try:
        scheduler.resume_job(name)
        _update_job_field(name, "paused", False)
        return f"Job '{name}' resumed"
    except Exception as e:
        return f"Error resuming job: {e}"


def get_all_jobs_with_status() -> list[dict]:
    """Return all jobs with enriched status (for API)."""
    saved = _read_jobs_json()
    scheduler = _get_scheduler()

    # Build next_run lookup from live scheduler
    next_runs: dict[str, str | None] = {}
    if scheduler:
        try:
            for job in scheduler.get_jobs():
                try:
                    next_runs[job.id] = str(job.next_run_time) if job.next_run_time else None
                except Exception:
                    next_runs[job.id] = None
        except Exception:
            pass

    # Enrich each job with run stats and status
    result = []
    for job in saved:
        name = job["name"]
        stats = db.get_cron_run_stats(name)
        running = db.list_cron_runs(job_name=name, limit=1)
        is_running = bool(running and running[0].get("status") == "running")

        if is_running:
            status = "running"
        elif job.get("paused"):
            status = "paused"
        else:
            status = "idle"

        result.append(
            {
                "name": name,
                "cron_expr": job.get("cron_expr", ""),
                "prompt": job.get("prompt", ""),
                "model": job.get("model", ""),
                "session_id": job.get("session_id"),
                "session_mode": job.get("session_mode", "fresh"),
                "paused": job.get("paused", False),
                "created_at": job.get("created_at", ""),
                "status": status,
                "next_run": next_runs.get(name),
                "run_count": stats.get("run_count", 0),
                "last_run_at": stats.get("last_run_at"),
            }
        )
    return result


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def schedule_workflow(
    workflow_name: str,
    cron_expr: str,
    inputs: str = "",
    title: str = "",
    _context: dict | None = None,
) -> str:
    """Schedule a workflow to run on a cron schedule.

    Creates a cron job that fires `run_workflow(workflow_name, inputs)` on each
    tick via a fresh session. Uses the existing scheduler infrastructure.
    """
    from apscheduler.triggers.cron import CronTrigger

    try:
        CronTrigger.from_crontab(cron_expr)
    except (ValueError, KeyError) as e:
        return f"Error: Invalid cron expression '{cron_expr}': {e}"

    from core.workflows.registry import get_workflow_registry

    reg = get_workflow_registry()
    if not reg.exists(workflow_name):
        return f"Error: Workflow '{workflow_name}' not found in registry"

    job_name = title or f"workflow:{workflow_name}"
    prompt = f"Run workflow {workflow_name}" + (f" with inputs: {inputs}" if inputs else "")

    ctx = _context or {}
    loop = ctx.get("_loop")

    try:
        import asyncio

        if loop and not _scheduler:
            future = asyncio.run_coroutine_threadsafe(_init_scheduler_async(), loop)
            future.result(timeout=10)

        _add_job_internal(
            job_name,
            cron_expr,
            prompt,
            session_id=None,
            model="",
            extra_meta={"workflow_name": workflow_name, "workflow_inputs": inputs},
        )
        _save_jobs()

        return f"Workflow '{workflow_name}' scheduled as '{job_name}': {cron_expr}"
    except Exception as e:
        return f"Error scheduling workflow: {e}"


def register(reg) -> None:
    common = {"category": "scheduling", "source": "extension"}
    tags = ["schedule", "cron", "recurring", "automated", "periodic", "timer", "job"]

    reg.register(
        name="schedule_job",
        func=schedule_job,
        description="Schedule a recurring job with 5-field cron expression. Job sends a prompt to a session on schedule.",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Unique job name"},
                "cron_expr": {"type": "string", "description": "Cron expression (5-field: min hour day month weekday)"},
                "prompt": {"type": "string", "description": "Message to send when job fires"},
                "session_id": {"type": "string", "description": "Session to target (empty = create new each time)"},
                "model": {"type": "string", "description": "Model override for this job (empty = default model)"},
            },
            "required": ["name", "cron_expr", "prompt"],
        },
        tags=tags + ["add", "create"],
        timeout=15,
        parallel_safe=False,
        safety_level="safe",
        **common,
    )
    reg.register(
        name="list_scheduled_jobs",
        func=list_scheduled_jobs,
        description="List all scheduled cron jobs with their next run times.",
        parameters={"type": "object", "properties": {}},
        tags=tags + ["list", "show"],
        timeout=15,
        parallel_safe=True,
        **common,
    )
    reg.register(
        name="remove_scheduled_job",
        func=remove_scheduled_job,
        description="Remove a scheduled cron job by name.",
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Job name to remove"}},
            "required": ["name"],
        },
        tags=tags + ["remove", "delete", "cancel"],
        timeout=15,
        parallel_safe=False,
        **common,
    )
    reg.register(
        name="set_job_state",
        func=set_job_state,
        description="Pause or resume a scheduled cron job. Pass paused=true to pause, paused=false to resume.",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Job name"},
                "paused": {"type": "boolean", "description": "true to pause, false to resume"},
            },
            "required": ["name", "paused"],
        },
        tags=tags + ["pause", "resume", "stop", "start", "unpause"],
        timeout=15,
        parallel_safe=False,
        **common,
    )
    reg.register(
        name="update_scheduled_job",
        func=update_scheduled_job,
        description="Update an existing scheduled job's cron expression, prompt, or model. Only provided fields are changed.",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Job name to update"},
                "cron_expr": {"type": "string", "description": "New 5-field cron expression (optional)"},
                "prompt": {"type": "string", "description": "New prompt text (optional)"},
                "model": {"type": "string", "description": "New model override (optional)"},
            },
            "required": ["name"],
        },
        tags=tags + ["update", "edit", "modify", "change"],
        timeout=15,
        parallel_safe=False,
        safety_level="safe",
        **common,
    )
    reg.register(
        name="schedule_workflow",
        func=schedule_workflow,
        description=(
            "Schedule a named workflow to run automatically on a cron schedule. "
            "The workflow fires run_workflow() on each tick via a fresh session. "
            "Use list_scheduled_jobs / remove_scheduled_job / set_job_state to manage it."
        ),
        parameters={
            "type": "object",
            "properties": {
                "workflow_name": {"type": "string", "description": "Name of the workflow to schedule"},
                "cron_expr": {
                    "type": "string",
                    "description": "5-field cron expression (e.g. '0 9 * * 1' = every Monday at 9am UTC)",
                },
                "inputs": {
                    "type": "string",
                    "description": "Optional free-form inputs passed to the workflow on each run",
                },
                "title": {"type": "string", "description": "Optional job name (defaults to 'workflow:{name}')"},
            },
            "required": ["workflow_name", "cron_expr"],
        },
        tags=tags + ["workflow", "automate", "pipeline"],
        timeout=15,
        parallel_safe=False,
        safety_level="safe",
        **common,
    )
