"""Pernix — Scheduling extension: cron job management via APScheduler."""

from __future__ import annotations

import asyncio
import json
import logging
import re
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
            # Heartbeat jobs use their own trigger/executor — an empty
            # cron_expr would crash CronTrigger.from_crontab below.
            if extra.get("kind") == "heartbeat":
                meta = {"name": job["name"], "cron_expr": "", "prompt": "", "model": "", "session_id": None}
                meta.update(extra)
                try:
                    _add_heartbeat_job_internal(job["name"], meta)
                    if job.get("paused") and _scheduler:
                        _scheduler.pause_job(job["name"])
                except Exception as hb_err:
                    logger.warning("Failed to restore heartbeat %s: %s", job["name"], hb_err)
                continue
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
        # Only jobs whose callable IS _execute_cron_job. That covers plain
        # prompt jobs and workflow jobs alike (schedule_workflow goes through
        # _add_job_internal, which always registers _execute_cron_job) —
        # what it excludes are the jobs on other callables: heartbeats, canary
        # sweeps/batches, telos slow ticks. Those own their own cadence and
        # must not be catch-up dispatched through the prompt path.
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


# ---------------------------------------------------------------------------
# Heartbeats (adaptation plan 3c) — recurring instructions steered into
# RUNNING work. Cron spawns turns; a heartbeat steers one. Two namespaces:
# the user's (one per session, set via API only) and the agent's own
# (multiple; the agent can never see or touch the user's).
# ---------------------------------------------------------------------------

# Coalescing: job_id -> the turn (current_turn_user_msg_id) it last steered.
# One steer per job per turn; undelivered pending follow-ups also coalesce.
_heartbeat_last_turn: dict[str, int | None] = {}


def _parse_every(every: str) -> tuple[str, object]:
    """'30s'/'5m'/'2h' -> ('interval', seconds); else ('cron', expr)."""
    e = (every or "").strip()
    m = re.match(r"^(\d+)\s*([smh])$", e)
    if m:
        mult = {"s": 1, "m": 60, "h": 3600}[m.group(2)]
        return "interval", max(30, int(m.group(1)) * mult)  # floor 30s
    return "cron", e


def _heartbeat_job_id(owner: str, session_id: str, name: str = "") -> str:
    suffix = f"_{re.sub(r'[^a-z0-9-]', '-', name.lower())[:24]}" if name else ""
    return f"hb_{owner}_{session_id[:12]}{suffix}"


def _add_heartbeat_job_internal(job_id: str, meta: dict):
    """Register a heartbeat with APScheduler (interval or cron trigger)."""
    scheduler = _get_scheduler()
    if not scheduler:
        return
    kind, val = _parse_every(meta.get("every", "5m"))
    if kind == "interval":
        from apscheduler.triggers.interval import IntervalTrigger

        trigger = IntervalTrigger(seconds=int(val))
    else:
        from apscheduler.triggers.cron import CronTrigger

        trigger = CronTrigger.from_crontab(str(val))
    scheduler.add_job(
        _execute_heartbeat_job,
        trigger=trigger,
        id=job_id,
        replace_existing=True,
        coalesce=True,
        misfire_grace_time=60,
        kwargs={"meta": meta},
    )


async def _execute_heartbeat_job(meta: dict):
    """Deliver one heartbeat tick. Steer = a role=system row the next round's
    compile picks up; follow_up = queued for the next idle dispatch. Parked
    states (AWAITING_WORKERS/AWAITING_USER) degrade steer to follow_up —
    they reach no round boundary."""
    from sessions import state_v2 as sv2
    from sessions.manager import get_manager
    from sessions.state import PendingMessage

    job_id = meta["name"]
    sid = meta.get("heartbeat_session_id", "")
    instruction = meta.get("instruction", "")
    delivery = meta.get("delivery", "steer")
    hb_name = meta.get("hb_name", "heartbeat")
    if not sid or not instruction:
        return

    run_id = db.add_cron_run(job_id, sid, status="claimed", fire_time=datetime.now(timezone.utc).isoformat())
    manager = get_manager()
    session = manager.get(sid)
    text = f"[heartbeat:{hb_name}] {instruction}"

    try:
        db.update_cron_run(run_id, "running")
        state = sv2._current_state(session) if session is not None else None
        mid_turn = state in (
            sv2.SessionStateV2.SCOUTING,
            sv2.SessionStateV2.PROCESSING,
            sv2.SessionStateV2.COMPACTING,
        )
        parked = state in (
            sv2.SessionStateV2.AWAITING_WORKERS,
            sv2.SessionStateV2.AWAITING_USER,
            sv2.SessionStateV2.PAUSED,
            sv2.SessionStateV2.PAUSE_REQUESTED,
            sv2.SessionStateV2.CANCELLING,
            sv2.SessionStateV2.FINALIZING,
        )

        if mid_turn and delivery == "steer":
            turn = getattr(session, "current_turn_user_msg_id", None)
            if _heartbeat_last_turn.get(job_id) == turn and turn is not None:
                db.update_cron_run(run_id, "completed", "coalesced: already steered this turn")
                return
            await asyncio.to_thread(db.add_message, sid, "system", text, metadata=json.dumps({"heartbeat": hb_name}))
            _heartbeat_last_turn[job_id] = turn
            logger.info("Heartbeat %s steered into running turn of %s", job_id, sid[:12])
        elif session is not None and (parked or mid_turn):
            # follow_up (explicit, or steer degraded in a parked state):
            # queue for the next idle dispatch; coalesce with any undelivered
            # copy of the same heartbeat.
            prefix = f"[heartbeat:{hb_name}]"
            already = any(prefix in getattr(p, "message", "") for p in session.pending_messages)
            if already:
                db.update_cron_run(run_id, "completed", "coalesced: prior tick still queued")
                return
            session.pending_messages.append(PendingMessage(text, None, False))
            logger.info("Heartbeat %s queued as follow-up for %s (state=%s)", job_id, sid[:12], state)
        else:
            # Idle (or non-resident) session: a heartbeat tick IS the turn.
            await manager.prompt(sid, text)
        db.update_cron_run(run_id, "completed")
    except Exception as e:
        logger.warning("Heartbeat %s delivery failed: %s", job_id, e)
        db.update_cron_run(run_id, "error", str(e))


def _set_heartbeat(owner: str, session_id: str, instruction: str, every: str, delivery: str, name: str = "") -> str:
    if delivery not in ("steer", "follow_up"):
        return "Error: delivery must be steer or follow_up."
    kind, val = _parse_every(every)
    if kind == "cron":
        try:
            from apscheduler.triggers.cron import CronTrigger

            CronTrigger.from_crontab(str(val))
        except Exception:
            return f"Error: '{every}' is neither a duration (30s/5m/2h) nor a valid cron expression."
    job_id = _heartbeat_job_id(owner, session_id, name)
    meta = {
        "name": job_id,
        "cron_expr": "",
        "prompt": "",
        "model": "",
        "session_id": None,
        "kind": "heartbeat",
        "owner": owner,
        "hb_name": name or ("user" if owner == "user" else "pulse"),
        "heartbeat_session_id": session_id,
        "instruction": instruction,
        "every": every,
        "delivery": delivery,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _add_heartbeat_job_internal(job_id, meta)
    _save_jobs()
    return job_id


def _clear_heartbeat(owner: str, session_id: str, name: str = "") -> bool:
    scheduler = _get_scheduler()
    if not scheduler:
        return False
    job_id = _heartbeat_job_id(owner, session_id, name)
    job = scheduler.get_job(job_id)
    if job is None:
        return False
    scheduler.remove_job(job_id)
    _heartbeat_last_turn.pop(job_id, None)
    _save_jobs()
    return True


def _list_heartbeats(owner: str, session_id: str) -> list[dict]:
    scheduler = _get_scheduler()
    if not scheduler:
        return []
    out = []
    for job in scheduler.get_jobs():
        meta = job.kwargs.get("meta", {})
        if meta.get("kind") != "heartbeat":
            continue
        if meta.get("owner") != owner or meta.get("heartbeat_session_id") != session_id:
            continue  # namespace separation: the agent never sees the user's
        out.append(
            {
                "name": meta.get("hb_name", ""),
                "instruction": meta.get("instruction", ""),
                "every": meta.get("every", ""),
                "delivery": meta.get("delivery", "steer"),
            }
        )
    return out


# Public user-heartbeat surface (API only; one per session).
def set_user_heartbeat(session_id: str, instruction: str, every: str = "5m", delivery: str = "steer") -> str:
    return _set_heartbeat("user", session_id, instruction, every, delivery)


def clear_user_heartbeat(session_id: str) -> bool:
    return _clear_heartbeat("user", session_id)


def get_user_heartbeat(session_id: str) -> dict | None:
    hbs = _list_heartbeats("user", session_id)
    return hbs[0] if hbs else None


# Agent-facing tools (registered below when heartbeats_enabled).
def set_heartbeat(
    name: str, instruction: str, every: str = "5m", delivery: str = "steer", _context: dict | None = None
) -> str:
    session_id = (_context or {}).get("session_id", "")
    if not session_id:
        return "Error: set_heartbeat requires a session context."
    if not name or not instruction:
        return "Error: name and instruction are required."
    result = _set_heartbeat("agent", session_id, instruction, every, delivery, name=name)
    if result.startswith("Error:"):
        return result
    return (
        f"Heartbeat '{name}' set: every {every}, delivery={delivery}. It steers a reminder "
        f"into running work (or queues it when the session is parked/idle). It cannot touch "
        f"the user's heartbeat."
    )


def clear_heartbeat(name: str, _context: dict | None = None) -> str:
    session_id = (_context or {}).get("session_id", "")
    if not session_id:
        return "Error: clear_heartbeat requires a session context."
    if _clear_heartbeat("agent", session_id, name=name):
        return f"Heartbeat '{name}' cleared."
    return f"Error: no agent heartbeat named '{name}' for this session."


def list_heartbeats(_context: dict | None = None) -> str:
    session_id = (_context or {}).get("session_id", "")
    if not session_id:
        return "Error: list_heartbeats requires a session context."
    hbs = _list_heartbeats("agent", session_id)
    if not hbs:
        return "No agent heartbeats for this session. (The user's heartbeat, if any, is not visible here.)"
    return "\n".join(f"- {h['name']}: every {h['every']} [{h['delivery']}] — {h['instruction']}" for h in hbs)


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
# Canary triggers (adaptation plan 3.5): scheduled / post_batch / manual
# ---------------------------------------------------------------------------

# One sweep at a time (canary_max_concurrent=1). A second trigger firing
# mid-sweep skips rather than queues — canaries measure, they don't backlog.
_canary_sweep_lock = asyncio.Lock()

# Post-batch retry cap: ~1 hour of 5-minute retries before giving up.
_CANARY_BATCH_MAX_ATTEMPTS = 12
_CANARY_BATCH_RETRY_S = 300


async def _execute_canary_sweep_job(meta: dict):
    """Scheduled/manual sweep executor. Never raises."""
    if not settings.canary_enabled:
        return
    if _canary_sweep_lock.locked():
        logger.info("Canary sweep skipped: another sweep is running")
        return
    async with _canary_sweep_lock:
        try:
            from core.canary import run_sweep

            await run_sweep(
                trigger=meta.get("trigger", "scheduled"),
                batch_id=meta.get("batch_id"),
                names=meta.get("names"),
            )
        except Exception as e:
            logger.error("Canary sweep failed: %s", e)


async def _execute_canary_batch_job(meta: dict):
    """Post-batch sweep executor: waits for an idle window by rescheduling.

    Enqueued by a Phase 4 apply (including approved-proposal applies). NEVER
    dispatched inline from a snooze activity — inline dispatch would cancel
    the cycle that produced the batch. Real user work defers the sweep;
    after _CANARY_BATCH_MAX_ATTEMPTS deferrals it runs anyway (batch-tagged
    data is worth more than a perfect idle window).
    """
    if not settings.canary_enabled:
        return
    from sessions.manager import get_manager

    attempts = int(meta.get("attempts", 0)) + 1
    if get_manager().has_active_work() and attempts < _CANARY_BATCH_MAX_ATTEMPTS:
        scheduler = _get_scheduler()
        if scheduler:
            from datetime import timedelta

            from apscheduler.triggers.date import DateTrigger

            retry_meta = dict(meta)
            retry_meta["attempts"] = attempts
            scheduler.add_job(
                _execute_canary_batch_job,
                trigger=DateTrigger(run_date=datetime.now(timezone.utc) + timedelta(seconds=_CANARY_BATCH_RETRY_S)),
                id=f"_canary_batch_{meta.get('batch_id', '?')}",
                replace_existing=True,
                kwargs={"meta": retry_meta},
            )
            logger.info(
                "Post-batch canary sweep deferred (attempt %d): active work present",
                attempts,
            )
        return
    await _execute_canary_sweep_job({**meta, "trigger": "post_batch"})


def enqueue_post_batch_sweep(batch_id: str, delay_s: int = 60) -> bool:
    """Queue a batch-tagged sweep for the next idle window (Phase 4 hook)."""
    if not settings.canary_enabled:
        return False
    scheduler = _get_scheduler()
    if not scheduler:
        return False
    from datetime import timedelta

    from apscheduler.triggers.date import DateTrigger

    scheduler.add_job(
        _execute_canary_batch_job,
        trigger=DateTrigger(run_date=datetime.now(timezone.utc) + timedelta(seconds=max(1, delay_s))),
        id=f"_canary_batch_{batch_id}",
        replace_existing=True,
        kwargs={"meta": {"kind": "canary", "transient": True, "batch_id": batch_id}},
    )
    return True


def enqueue_manual_canary(name: str) -> bool:
    """Fire a single canary ASAP without blocking the caller's turn."""
    scheduler = _get_scheduler()
    if not scheduler:
        return False
    from apscheduler.triggers.date import DateTrigger

    scheduler.add_job(
        _execute_canary_sweep_job,
        trigger=DateTrigger(run_date=datetime.now(timezone.utc)),
        id=f"_canary_manual_{name}",
        replace_existing=True,
        kwargs={"meta": {"kind": "canary", "transient": True, "trigger": "manual", "names": [name]}},
    )
    return True


def ensure_canary_schedule() -> None:
    """Install the nightly sweep job from settings (config is the truth —
    the job is transient, recreated each boot, never persisted to JSON)."""
    if not settings.canary_enabled:
        return
    scheduler = _get_scheduler()
    if not scheduler:
        return
    try:
        from apscheduler.triggers.cron import CronTrigger

        scheduler.add_job(
            _execute_canary_sweep_job,
            trigger=CronTrigger.from_crontab(settings.canary_schedule, timezone="UTC"),
            id="_canary_sweep",
            replace_existing=True,
            coalesce=True,
            misfire_grace_time=3600,
            kwargs={"meta": {"kind": "canary", "transient": True, "trigger": "scheduled"}},
        )
        logger.info("Canary sweep scheduled: %s", settings.canary_schedule)
    except Exception as e:
        logger.warning("Failed to schedule canary sweep: %s", e)


# ---------------------------------------------------------------------------
# TELOS slow loops: daily ordo/binding, weekly hevel/reconcile/entropy
# ---------------------------------------------------------------------------

_telos_slow_lock = asyncio.Lock()


async def _execute_telos_slow_job(meta: dict):
    """Daily TELOS slow-loop executor. Never raises."""
    if not settings.telos_enabled:
        return
    if _telos_slow_lock.locked():
        logger.info("TELOS slow loops skipped: another pass is running")
        return
    async with _telos_slow_lock:
        try:
            from core.telos import run_slow_loops

            stats = await run_slow_loops(force_weekly=bool(meta.get("force_weekly")))
            logger.info("TELOS slow loops done: %s", {k: v for k, v in stats.items() if v})
        except Exception as e:
            logger.error("TELOS slow loops failed: %s", e)


def ensure_telos_schedule() -> None:
    """Install the daily slow-loop job from settings (config is the truth —
    transient, recreated each boot, never persisted to JSON)."""
    if not settings.telos_enabled:
        return
    scheduler = _get_scheduler()
    if not scheduler:
        return
    try:
        from apscheduler.triggers.cron import CronTrigger

        scheduler.add_job(
            _execute_telos_slow_job,
            trigger=CronTrigger.from_crontab(settings.telos_schedule, timezone="UTC"),
            id="_telos_slow",
            replace_existing=True,
            coalesce=True,
            misfire_grace_time=3600,
            kwargs={"meta": {"kind": "telos", "transient": True}},
        )
        logger.info("TELOS slow loops scheduled: %s", settings.telos_schedule)
    except Exception as e:
        logger.warning("Failed to schedule TELOS slow loops: %s", e)


def enqueue_manual_telos(force_weekly: bool = False) -> bool:
    """Fire the slow-loop pass ASAP without blocking the caller's turn."""
    scheduler = _get_scheduler()
    if not scheduler:
        return False
    from apscheduler.triggers.date import DateTrigger

    scheduler.add_job(
        _execute_telos_slow_job,
        trigger=DateTrigger(run_date=datetime.now(timezone.utc)),
        id="_telos_manual",
        replace_existing=True,
        kwargs={"meta": {"kind": "telos", "transient": True, "force_weekly": force_weekly}},
    )
    return True


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

    Creates an ordinary cron job on the existing scheduler infrastructure: each
    tick opens a fresh session and sends it the English prompt "Run workflow
    <name> with inputs: ..." — it does NOT call run_workflow() directly. The
    workflow name/inputs are also stamped into the job meta
    (workflow_name/workflow_inputs) for bookkeeping. Whether the agent actually
    invokes the workflow is therefore a model decision, not a mechanical one.
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

    if settings.heartbeats_enabled:
        hb_tags = tags + ["heartbeat", "steer", "reminder", "pulse", "long-running"]
        reg.register(
            name="set_heartbeat",
            func=set_heartbeat,
            description=(
                "Set a recurring heartbeat for THIS session: an instruction steered into "
                "running work at the next round boundary (delivery=steer, the default) or "
                "queued for the next idle moment (delivery=follow_up). Distinct from cron — "
                "cron spawns new turns; a heartbeat nudges the current one. `every` accepts "
                "durations (30s/5m/2h, floor 30s) or a 5-field cron expression. Parked "
                "sessions (awaiting workers/user) degrade steer to follow_up. You cannot "
                "read or modify the user's own heartbeat."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Short heartbeat name"},
                    "instruction": {"type": "string", "description": "The recurring reminder text"},
                    "every": {"type": "string", "description": "Duration (5m) or cron expression (default 5m)"},
                    "delivery": {"type": "string", "description": "steer (default) | follow_up"},
                },
                "required": ["name", "instruction"],
            },
            tags=hb_tags,
            timeout=30,
            parallel_safe=False,
            safety_level="caution",
            **common,
        )
        reg.register(
            name="clear_heartbeat",
            func=clear_heartbeat,
            description="Remove one of this session's agent heartbeats by name.",
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Heartbeat name"}},
                "required": ["name"],
            },
            tags=hb_tags,
            timeout=30,
            parallel_safe=False,
            safety_level="safe",
            **common,
        )
        reg.register(
            name="list_heartbeats",
            func=list_heartbeats,
            description="List this session's agent heartbeats (the user's is never shown).",
            parameters={"type": "object", "properties": {}},
            tags=hb_tags,
            timeout=30,
            parallel_safe=True,
            safety_level="safe",
            **common,
        )

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
