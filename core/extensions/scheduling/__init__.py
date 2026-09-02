"""Pernix — Scheduling extension: cron job management via APScheduler.

A scheduled multi-step pipeline is an ordinary cron prompt that tells the agent
which skill to load and which steps to run — there is no separate pipeline
scheduler.
"""

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
# variant fields (kind, last_fired_at, ...) survive restarts.
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


def jobs_for_space(space_id: str) -> list[str]:
    """Names of jobs bound to a space (space_id rides in extra_meta)."""
    scheduler = _get_scheduler()
    if not scheduler or not space_id:
        return []
    return [j.id for j in scheduler.get_jobs() if j.kwargs.get("meta", {}).get("space_id") == space_id]


def unbind_space_jobs(space_id: str) -> int:
    """Drop the space binding from every bound job (detach-delete path).
    The jobs keep running; their future firings just land un-spaced."""
    scheduler = _get_scheduler()
    if not scheduler or not space_id:
        return 0
    changed = 0
    for job in scheduler.get_jobs():
        meta = job.kwargs.get("meta", {})
        if meta.get("space_id") == space_id:
            meta.pop("space_id", None)
            changed += 1
    if changed:
        _save_jobs()
    return changed


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
            # Round-trip every remaining meta key verbatim (last_fired_at,
            # kind, ...) — the load side mirrors this.
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


def _count_missed_fires(trigger, last_fired_iso: str, now: datetime, cap: int = 1000) -> int:
    """Count scheduled fires strictly after last_fired and at/before now.

    `trigger` must be the job's LIVE trigger object (``job.trigger``), so the
    missed-run grid can never disagree with the grid that actually fires.
    This function used to rebuild a trigger from the cron expression with a
    hardcoded UTC timezone while the real jobs run on the container's local
    zone (from_crontab with no tz → America/Chicago) — five hours of every
    day the two grids disagreed, and any restart in that gap minted a
    phantom "missed run" catch-up for a slot that had already fired
    (2026-08-31: the 17:00 UTC curiosity-drive slot completed at 17:03, a
    18:30 UTC deploy restart saw the UTC grid's nonexistent 18:00 slot and
    dispatched a spurious coalesced run).

    Capped so a years-stale last_fired_at on an every-minute job can't spin."""
    try:
        prev = datetime.fromisoformat(last_fired_iso)
    except (ValueError, TypeError):
        return 0
    if prev.tzinfo is None:
        prev = prev.replace(tzinfo=timezone.utc)
    missed = 0
    while missed < cap:
        try:
            nxt = trigger.get_next_fire_time(prev, prev)
        except Exception:
            return 0
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
        # Only jobs whose callable IS _execute_cron_job — every job added
        # through _add_job_internal. What this excludes are the jobs on other
        # callables: heartbeats, canary sweeps/batches, telos slow ticks.
        # Those own their own cadence and must not be catch-up dispatched
        # through the prompt path.
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
        missed = _count_missed_fires(job.trigger, last, now)
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
    they reach no round boundary.

    A cron_runs row is written only for ticks that actually deliver (steer,
    queue, or dispatch). Coalesced no-op ticks write nothing: a 30s heartbeat
    recorded ~2,880 rows/day, almost all of them "nothing happened", which
    buried real job history and defeated the run-stats readout.
    """
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

    session = get_manager().get(sid)
    text = f"[heartbeat:{hb_name}] {instruction}"

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
    steering = mid_turn and delivery == "steer"
    # follow_up (explicit, or steer degraded in a parked state): queue for the
    # next idle dispatch; coalesce with any undelivered copy of the same
    # heartbeat.
    queueing = not steering and session is not None and (parked or mid_turn)

    turn = getattr(session, "current_turn_user_msg_id", None) if session is not None else None
    if steering and turn is not None and _heartbeat_last_turn.get(job_id) == turn:
        return  # coalesced: already steered this turn
    if queueing and any(f"[heartbeat:{hb_name}]" in getattr(p, "message", "") for p in session.pending_messages):
        return  # coalesced: prior tick still queued

    run_id = db.add_cron_run(job_id, sid, status="claimed", fire_time=datetime.now(timezone.utc).isoformat())
    try:
        db.update_cron_run(run_id, "running")
        if steering:
            await asyncio.to_thread(db.add_message, sid, "system", text, metadata=json.dumps({"heartbeat": hb_name}))
            _heartbeat_last_turn[job_id] = turn
            logger.info("Heartbeat %s steered into running turn of %s", job_id, sid[:12])
        elif queueing:
            session.pending_messages.append(PendingMessage(text, None, False))
            logger.info("Heartbeat %s queued as follow-up for %s (state=%s)", job_id, sid[:12], state)
        else:
            # Idle (or non-resident) session: a heartbeat tick IS the turn —
            # same dispatch, same bound, as a cron fire.
            await _dispatch_prompt(sid, text)
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


def _ensure_dispatch_session(session_id: str | None, title: str = "", space_id: str | None = None) -> str:
    """Resolve or create the session a scheduled dispatch will run in.

    session_type="cron" is what _is_unattended_session() keys off — without
    it scheduled jobs hit the dangerous-tool approval gate, call ask_user
    into the void, and wedge in AWAITING_USER forever. Jobs reusing an
    explicit session_id keep that session's type: a user-attended session
    shouldn't lose its gate just because a job also runs in it.

    space_id (v33): a space-bound job's fresh run session is created INSIDE
    the space, so the run inherits the space's directives, memory routing,
    workspace home and shared kernel. Still titled "Cron: …" — the 7-day
    machine-run sweep applies in spaces too, by decision.
    """
    from db import models as _db
    from sessions.manager import get_manager

    if session_id:
        # A pinned session the user has since deleted used to be returned
        # anyway: manager.prompt then raised on every tick, which meant an
        # error cron_run row and a high-urgency notification every time the
        # job fired — 96 a day for a */15 job, forever, with no way to
        # notice except the noise. Fall back to a fresh run session.
        if get_manager().get(session_id) is not None or _db.get_session(session_id) is not None:
            return session_id
        logger.warning(
            "Scheduled job's session %s no longer exists — running in a fresh session instead",
            session_id[:12],
        )
    return get_manager().create_session(title=title, session_type="cron", space_id=space_id)


# A charter that grants a write tool must grant its repair tool: remember()
# refuses near-duplicates with "supersede via update_memory(...)", and a run
# allowed to remember but not to update_memory has the repair path named in
# an error it cannot act on — the fact is then silently lost until another
# session relearns it (agent-ergonomics plan §4.5; live memory notes
# pernix.agent_engineering @1787710676 / @1787695237 record exactly this).
_TOOL_REPAIR_PAIRS: dict[str, tuple[str, ...]] = {
    "remember": ("update_memory", "recall"),
}


def _pair_repair_tools(allow: frozenset[str]) -> frozenset[str]:
    extra: set[str] = set()
    for tool, repairs in _TOOL_REPAIR_PAIRS.items():
        if tool in allow:
            extra.update(repairs)
    return allow | frozenset(extra)


async def _dispatch_prompt(
    session_id: str | None,
    prompt: str,
    title: str = "",
    model: str = "",
    allowed_tools: list | None = None,
) -> str:
    """Open-or-reuse a session and send it one prompt under the cron bound.

    The single dispatch path for scheduled work: cron fires and heartbeat
    ticks into an idle session are the same operation (a scheduled prompt IS
    the turn), so they share the session handling, the model override, and the
    cron_dispatch_timeout ceiling. Returns the session id used.

    allowed_tools, when set on the job, becomes the session's exclusive tool
    allow-list for the dispatched turn (enforced in the schema builder and the
    executor — see AgentSession.tool_allowlist). Set and cleared exactly like
    the model override so a reused session isn't left constrained.
    """
    from sessions.manager import get_manager

    manager = get_manager()
    # A session created for this dispatch is throwaway: nothing else will
    # ever run in it, so its overrides need no clearing.
    _created_fresh = not session_id
    session_id = _ensure_dispatch_session(session_id, title)
    session = manager.get(session_id)
    if session and model:
        session.model_override = model
    if session and allowed_tools:
        session.tool_allowlist = _pair_repair_tools(frozenset(str(t) for t in allowed_tools if t))

    try:
        await asyncio.wait_for(
            manager.prompt(session_id, prompt),
            timeout=settings.cron_dispatch_timeout,
        )
        # prompt() returns as soon as the turn TASK is created (manager.prompt
        # ends in asyncio.create_task and does not await it). The per-dispatch
        # overrides must outlive the TURN, not the enqueue — clearing right
        # here silently unconstrained every scheduled run (field case: runs
        # ecfd3f89c219 / 404eaba3c8d9 called file_edit/multiedit straight
        # through the E1 allow-list because it was already cleared before the
        # schema was built). Wait for the task, bounded by the same dispatch
        # ceiling; shielded so a timeout stops the WAIT, never the turn.
        session = manager.get(session_id)
        task = getattr(session, "task", None) if session else None
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=settings.cron_dispatch_timeout)
            except asyncio.TimeoutError:
                logger.warning(
                    "Scheduled dispatch for %s still running after %ds — "
                    "clearing per-dispatch overrides while the turn continues",
                    session_id[:12],
                    settings.cron_dispatch_timeout,
                )
            except Exception:
                pass  # _run_agent_safe owns its errors; the wait is best-effort
    finally:
        # Clear the model override and tool allow-list for reused sessions.
        #
        # Bound to the TURN, never to the timer above. When the shielded wait
        # times out (an orchestrating job legitimately running past
        # cron_dispatch_timeout) the turn is still going: clearing here handed
        # it the full tool surface mid-run and let its next LLM call fall back
        # to the default model, which violates the never-auto-switch rule. A
        # fresh session is thrown away after the run, so it needs no clearing
        # at all; a reused one gets a done-callback on its own task.
        session = manager.get(session_id)
        if session and (model or allowed_tools) and not _created_fresh:

            def _clear(_task=None, _s=session):
                if model:
                    _s.model_override = None
                if allowed_tools:
                    _s.tool_allowlist = None

            task = getattr(session, "task", None)
            if task is not None and not task.done():
                task.add_done_callback(_clear)
            else:
                _clear()
    return session_id


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
        db.update_cron_run(run_id, "running")
        # Bind the session id BEFORE dispatch: a timeout/error must still
        # reference the session that holds the partial transcript. The
        # back-fill matters for fresh-session jobs — the claim row above was
        # written before this session existed.
        session_id = _ensure_dispatch_session(session_id, title=f"Cron: {name}", space_id=meta.get("space_id"))
        db.update_cron_run(run_id, "running", session_id=session_id)
        await _dispatch_prompt(session_id, prompt, model=model, allowed_tools=meta.get("allowed_tools"))

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
        # session_id is whatever resolution reached before the failure —
        # back-fill it when we have one so the error row links its transcript.
        db.update_cron_run(run_id, "error", str(e), session_id=session_id)
        bus.emit({"type": "job.error", "job_name": name, "session_id": session_id, "error": str(e)})
        # Notify user about the failure via push/webhook
        _notify_job_failure(manager, bus, name, session_id, str(e))
    finally:
        get_snooze().notify_activity()


async def _init_scheduler_async():
    """Initialize scheduler from async context (main event loop)."""
    _get_scheduler()


# ---------------------------------------------------------------------------
# Canary triggers: scheduled (heartbeat) / post_batch / manual / full
# ---------------------------------------------------------------------------

# One sweep at a time. A second trigger firing mid-sweep skips rather than
# queues — canaries measure, they don't backlog. The exception is a sweep
# whose meta says must_run (full sweeps after a model swap or deploy, and
# coverage-triggered sweeps): those reschedule themselves instead of being
# silently eaten by a heartbeat that happened to be in flight.
_canary_sweep_lock = asyncio.Lock()

# Post-batch retry cap: ~1 hour of 5-minute retries before giving up.
_CANARY_BATCH_MAX_ATTEMPTS = 12
_CANARY_BATCH_RETRY_S = 300
# must_run lock-retry cap: ~20 minutes of 2-minute retries.
_CANARY_LOCK_MAX_ATTEMPTS = 10
_CANARY_LOCK_RETRY_S = 120


async def _execute_canary_sweep_job(meta: dict):
    """Sweep executor for every trigger. Never raises."""
    if not settings.canary_enabled:
        return
    if _canary_sweep_lock.locked():
        if meta.get("must_run"):
            attempts = int(meta.get("lock_attempts", 0)) + 1
            scheduler = _get_scheduler()
            if scheduler and attempts <= _CANARY_LOCK_MAX_ATTEMPTS:
                from datetime import timedelta

                from apscheduler.triggers.date import DateTrigger

                scheduler.add_job(
                    _execute_canary_sweep_job,
                    trigger=DateTrigger(run_date=datetime.now(timezone.utc) + timedelta(seconds=_CANARY_LOCK_RETRY_S)),
                    id=meta.get("job_id") or "_canary_must_run_retry",
                    replace_existing=True,
                    kwargs={"meta": {**meta, "lock_attempts": attempts}},
                )
                logger.info(
                    "Canary %s sweep waiting out the running sweep (attempt %d)",
                    meta.get("trigger", "?"),
                    attempts,
                )
                return
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


def _post_batch_targets(batch_id: str) -> list[str]:
    """Which canaries a post-batch probe runs, resolved at EXECUTION time
    (the suite may have changed during the up-to-an-hour idle deferral).

    Canaries whose `covers:` matches the batch's edit kinds come first —
    they are the ones actually testing what changed — then the
    sentinel-tagged ones ride along, capped at canary_post_batch_max. A
    missing or unreadable batch row falls back to sentinels; a suite with
    neither coverage nor sentinels falls back to the active non-flaky
    canaries so the tripwire is never left blind by omission.
    """
    import json as _json

    from core.canary import scan_canaries
    from db import models as db

    kinds: set[str] = set()
    try:
        batch = db.adaptive_get_batch(batch_id)
        for edit in _json.loads((batch or {}).get("payload_json") or "[]"):
            if isinstance(edit, dict) and edit.get("kind"):
                kinds.add(f"kind:{edit['kind']}")
    except Exception as e:
        logger.warning("Post-batch target resolution for %s fell back to sentinels: %s", batch_id, e)

    defs = scan_canaries()
    targets = [d.name for d in defs if kinds & set(d.covers)]
    targets += [d.name for d in defs if "sentinel" in d.tags and d.name not in targets]
    if not targets:
        targets = [d.name for d in defs if not d.parked and not d.flaky]
    return targets[: max(1, settings.canary_post_batch_max)]


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
    names = _post_batch_targets(str(meta.get("batch_id") or ""))
    if not names:
        logger.info("Post-batch canary sweep for %s: no canaries to run", meta.get("batch_id"))
        return
    await _execute_canary_sweep_job({**meta, "trigger": "post_batch", "names": names})


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


def enqueue_targeted_sweep(names: list[str], reason: str) -> bool:
    """Fire a coverage-triggered set of canaries as ONE job.

    One job, not one per name: enqueue_manual_canary calls landing at the
    same instant would race the skip-not-queue sweep lock and all but the
    first would be silently dropped. must_run so a heartbeat in flight
    defers rather than eats the probe.
    """
    names = [n for n in names if n]
    if not names or not settings.canary_enabled:
        return False
    scheduler = _get_scheduler()
    if not scheduler:
        return False
    from apscheduler.triggers.date import DateTrigger

    job_id = f"_canary_targeted_{reason}"
    scheduler.add_job(
        _execute_canary_sweep_job,
        trigger=DateTrigger(run_date=datetime.now(timezone.utc)),
        id=job_id,
        replace_existing=True,
        kwargs={
            "meta": {
                "kind": "canary",
                "transient": True,
                "trigger": "manual",
                "names": names,
                "must_run": True,
                "job_id": job_id,
            }
        },
    )
    return True


def enqueue_full_sweep(reason: str, delay_s: int = 0) -> bool:
    """A the-world-changed sweep (model swap, deploy, 'Run all'): every
    canary including parked ones, must_run so nothing in flight eats it."""
    if not settings.canary_enabled:
        return False
    scheduler = _get_scheduler()
    if not scheduler:
        return False
    from datetime import timedelta

    from apscheduler.triggers.date import DateTrigger

    job_id = f"_canary_full_{reason}"
    scheduler.add_job(
        _execute_canary_sweep_job,
        trigger=DateTrigger(run_date=datetime.now(timezone.utc) + timedelta(seconds=max(0, delay_s))),
        id=job_id,
        replace_existing=True,
        kwargs={
            "meta": {
                "kind": "canary",
                "transient": True,
                "trigger": "full",
                "must_run": True,
                "job_id": job_id,
                "reason": reason,
            }
        },
    )
    return True


def ensure_canary_schedule() -> None:
    """Install the nightly heartbeat job from settings (config is the truth —
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
        logger.info("Canary heartbeat scheduled: %s", settings.canary_schedule)
    except Exception as e:
        logger.warning("Failed to schedule canary sweep: %s", e)


# ---------------------------------------------------------------------------
# TELOS slow loops: daily retirement sweeps, weekly entropy control
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


# ---------------------------------------------------------------------------
# Job spec validation + isolated test-run (spec Feature 7)
# ---------------------------------------------------------------------------

# A test-run is a smoke check, not a production run — bounded well under the
# cron dispatch ceiling so a broken prompt fails fast.
_JOB_TEST_TIMEOUT_S = 300
_JOB_TEST_CANCEL_GRACE_S = 30


def validate_job_spec(
    cron_expr: str,
    prompt: str,
    model: str = "",
    allowed_tools: list | None = None,
) -> dict:
    """Structured validation of a job spec. Errors block; warnings don't.

    Errors are the locally-provable breaks (unparseable cron, empty prompt,
    a tool name the registry has never heard of). Model resolution is a
    warning only — the model registry can be stale or remote, and blocking a
    save on it would reject legitimate OpenRouter ids.
    """
    errors: list[str] = []
    warnings: list[str] = []
    from apscheduler.triggers.cron import CronTrigger

    try:
        CronTrigger.from_crontab(cron_expr)
    except (ValueError, KeyError) as e:
        errors.append(f"cron_expr: invalid ({e})")
    if len((prompt or "").strip()) < 10:
        errors.append("prompt: too short to be a real job charter (<10 chars)")
    if allowed_tools:
        try:
            from core.tools.registry import get_registry

            treg = get_registry()
            for t in allowed_tools:
                t = str(t)
                if not treg.exists(t):
                    errors.append(f"allowed_tools: unknown tool '{t}'")
                elif treg.is_disabled(t):
                    warnings.append(f"allowed_tools: tool '{t}' is currently disabled")
        except Exception as e:
            warnings.append(f"allowed_tools: registry unavailable — not validated ({e})")
    if model:
        try:
            from core.llm.client import get_llm_client

            mreg = get_llm_client().router.registry
            if not mreg.get_model_info(mreg.resolve_model_id(model)):
                warnings.append(f"model: '{model}' not found in the model registry (may still resolve at dispatch)")
        except Exception:
            warnings.append("model: registry unavailable — not validated")
    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "at": datetime.now(timezone.utc).isoformat(),
    }


async def run_job_test(name: str) -> dict:
    """Dry-run one job's prompt in an isolated temp workspace.

    Mirrors the canary runner's mechanics (temp workspace override, real
    manager.prompt, wait on the task handle, cancel + grace on timeout) but
    runs under the job's OWN model and allow-list for fidelity. Never records
    a cron_runs row and never consumes the schedule. The transcript session
    (titled "Job test: <name>") is kept for inspection.
    """
    import shutil
    import tempfile
    from pathlib import Path

    from core.canary.runner import _wait_for_turn_end
    from core.events import get_event_bus
    from sessions import state_v2 as sv2
    from sessions.manager import get_manager

    saved = _read_jobs_json()
    job = next((j for j in saved if j["name"] == name), None)
    if job is None:
        return {"ok": False, "job": name, "error": f"job '{name}' not found"}

    validation = validate_job_spec(
        job.get("cron_expr", ""), job.get("prompt", ""), job.get("model", ""), job.get("allowed_tools")
    )
    bus = get_event_bus()
    bus.emit({"type": "job.test_started", "job_name": name})

    manager = get_manager()
    start = time.monotonic()
    tmp = Path(tempfile.mkdtemp(prefix=f"jobtest-{name[:24]}-"))
    result: dict = {"ok": False, "job": name, "validation": validation}
    sid = ""
    turn_started = False
    turn_ended = False
    try:
        sid = manager.create_session(title=f"Job test: {name}", session_type="cron")
        result["session_id"] = sid
        session = manager.get(sid)
        session.workspace_override = str(tmp)
        if job.get("model"):
            session.model_override = job["model"]
        if job.get("allowed_tools"):
            session.tool_allowlist = frozenset(str(t) for t in job["allowed_tools"] if t)

        await manager.prompt(sid, job.get("prompt", ""))
        turn_started = True
        turn_ended = await _wait_for_turn_end(session, time.monotonic() + _JOB_TEST_TIMEOUT_S)
        if not turn_ended:
            session.cancel_requested = True
            turn_ended = await _wait_for_turn_end(session, time.monotonic() + _JOB_TEST_CANCEL_GRACE_S)
            result["error"] = f"timeout after {_JOB_TEST_TIMEOUT_S}s"

        result["termination_reason"] = session.termination_reason
        if session.error:
            result["error"] = session.error
        if sv2._current_state(session) is sv2.SessionStateV2.AWAITING_USER:
            # An unattended fire would hang here forever — that IS the finding.
            result["error"] = "job asked a user question — an unattended run would wedge in AWAITING_USER"

        def _read_outcome() -> tuple[str, int]:
            preview = ""
            for m in reversed(db.get_messages(sid, last=50)):
                if m.get("role") == "assistant" and m.get("content"):
                    preview = m["content"][:400]
                    break
            tokens = int((db.get_session_usage(sid) or {}).get("total", 0))
            return preview, tokens

        try:
            _preview, _tokens = await asyncio.to_thread(_read_outcome)
            if _preview:
                result["answer_preview"] = _preview
            result["tokens"] = _tokens
        except Exception:
            pass
        result["ok"] = bool(turn_ended) and not result.get("error")
    except Exception as e:
        logger.exception("Job test '%s' failed", name)
        result["error"] = result.get("error") or str(e)
    finally:
        result["duration_s"] = round(time.monotonic() - start, 1)
        try:
            s = manager.get(sid) if sid else None
            if s is not None:
                s.workspace_override = None
                s.model_override = None
                s.tool_allowlist = None
        except Exception:
            pass
        # Same reclamation rule as the canary runner: never delete the temp
        # workspace under a still-running agent — leak and log instead.
        if turn_ended or not turn_started:
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            logger.warning("Job test '%s' never ended its turn — leaving %s in place", name, tmp)

    # Stamp the outcome onto the live job's meta (round-tripped by _save_jobs)
    # so the panel can show "last test: pass/fail" without a separate store.
    try:
        scheduler = _get_scheduler()
        live = scheduler.get_job(name) if scheduler else None
        if live is not None:
            live.kwargs.get("meta", {})["last_test"] = {
                "ok": result["ok"],
                "at": datetime.now(timezone.utc).isoformat(),
                "error": (result.get("error") or "")[:300],
                "duration_s": result["duration_s"],
            }
            _save_jobs()
    except Exception as e:
        logger.debug("Persisting job test outcome failed: %s", e)

    bus.emit(
        {
            "type": "job.test_done",
            "job_name": name,
            "ok": result["ok"],
            "error": result.get("error") or "",
            "duration_s": result["duration_s"],
            "session_id": result.get("session_id") or "",
        }
    )
    logger.info(
        "Job test '%s' %s (%.0fs)%s",
        name,
        "PASSED" if result["ok"] else "FAILED",
        result["duration_s"],
        f" — {result.get('error')}" if result.get("error") else "",
    )
    return result


def test_job(name: str, _context: dict | None = None) -> str:
    """Tool wrapper: run a job's prompt once in an isolated workspace."""
    import asyncio

    ctx = _context or {}
    loop = ctx.get("_loop")
    if not loop:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return "Error: no event loop available for test_job"
    try:
        result = asyncio.run_coroutine_threadsafe(run_job_test(name), loop).result(
            timeout=_JOB_TEST_TIMEOUT_S + _JOB_TEST_CANCEL_GRACE_S + 60
        )
    except Exception as e:
        return f"Error: job test did not complete: {e}"
    lines = [f"Job test '{name}': {'PASS' if result.get('ok') else 'FAIL'} ({result.get('duration_s', 0)}s)"]
    if result.get("error"):
        lines.append(f"Error: {result['error']}")
    v = result.get("validation") or {}
    for e in v.get("errors", []):
        lines.append(f"spec error: {e}")
    for w in v.get("warnings", []):
        lines.append(f"spec warning: {w}")
    if result.get("termination_reason"):
        lines.append(f"termination: {result['termination_reason']}")
    if result.get("answer_preview"):
        lines.append(f"answer preview: {result['answer_preview'][:300]}")
    if result.get("session_id"):
        lines.append(f"transcript session: {result['session_id']}")
    lines.append("(dry run — no cron_runs row recorded, schedule untouched, temp workspace)")
    return "\n".join(lines)


def schedule_job(
    name: str,
    cron_expr: str,
    prompt: str,
    session_id: str = "",
    model: str = "",
    space_id: str = "",
    _context: dict | None = None,
) -> str:
    """Schedule a recurring job with cron expression.

    space_id binds the job to a space — every firing runs in a fresh cron
    session created inside that space. When the CALLER is a space session
    and no space_id is given, the job inherits the caller's space (work
    scheduled from inside a space stays in the space); pass space_id="none"
    to schedule an unbound job from a space session.
    """
    import asyncio

    # Full spec validation up front (spec Feature 7): a job that can't fire
    # correctly should fail at save time, not weeks later on schedule.
    v = validate_job_spec(cron_expr, prompt, model=model)
    if not v["ok"]:
        return "Error: job spec invalid — " + "; ".join(v["errors"])

    ctx = _context or {}
    loop = ctx.get("_loop")

    # Resolve the space binding: explicit id > caller's space > none.
    resolved_space: str | None = None
    if space_id and space_id.lower() != "none":
        from db import models as _db

        if not _db.get_space(space_id):
            return f"Error: space '{space_id}' not found"
        resolved_space = space_id
    elif not space_id:
        try:
            from core.spaces import get_session_space

            caller_space = get_session_space(ctx.get("session_id", ""))
            resolved_space = caller_space["id"] if caller_space else None
        except Exception:
            resolved_space = None

    extra_meta: dict = {"validation": v}
    if resolved_space:
        extra_meta["space_id"] = resolved_space

    try:
        if loop and not _scheduler:
            future = asyncio.run_coroutine_threadsafe(_init_scheduler_async(), loop)
            future.result(timeout=10)

        _add_job_internal(name, cron_expr, prompt, session_id=session_id or None, model=model, extra_meta=extra_meta)
        _save_jobs()
        msg = f"Job '{name}' scheduled: {cron_expr}"
        if v["warnings"]:
            msg += " (warnings: " + "; ".join(v["warnings"]) + ")"
        return msg
    except Exception as e:
        return f"Error scheduling job: {e}"


def update_scheduled_job(
    name: str,
    cron_expr: str | None = None,
    prompt: str | None = None,
    model: str | None = None,
    space_id: str | None = None,
    _context: dict | None = None,
) -> str:
    """Update an existing scheduled job. Only provided fields are changed.

    space_id: None = unchanged; ""/"none" = unbind; an id = rebind
    (validated). Rides extra_meta like every non-structural field."""
    scheduler = _get_scheduler()
    if not scheduler:
        return "Scheduler not available"

    # Read current job from JSON
    saved = _read_jobs_json()
    current = next((j for j in saved if j["name"] == name), None)
    if not current:
        return f"Error: Job '{name}' not found"

    if space_id is not None:
        if not space_id or space_id.lower() == "none":
            current.pop("space_id", None)
        else:
            from db import models as _db

            if not _db.get_space(space_id):
                return f"Error: space '{space_id}' not found"
            current["space_id"] = space_id

    # Merge changes
    new_cron = cron_expr if cron_expr is not None else current.get("cron_expr", "")
    new_prompt = prompt if prompt is not None else current.get("prompt", "")
    new_model = model if model is not None else current.get("model", "")
    was_paused = current.get("paused", False)

    # Re-validate the merged spec (spec Feature 7). Block ONLY on errors the
    # edit itself introduces (a changed cron/prompt) — a job whose
    # pre-existing prompt or allow-list wouldn't pass today's rules must stay
    # editable, or the fix for an invalid job would be refused by its own
    # invalidity. The full result still lands on the job for the UI badge.
    v = validate_job_spec(new_cron, new_prompt, model=new_model, allowed_tools=current.get("allowed_tools"))
    blocking: list[str] = []
    if cron_expr is not None:
        blocking += [e for e in v["errors"] if e.startswith("cron_expr")]
    if prompt is not None:
        blocking += [e for e in v["errors"] if e.startswith("prompt")]
    if blocking:
        return "Error: job spec invalid — " + "; ".join(blocking)
    current["validation"] = v

    try:
        # Re-add with updated parameters (replace_existing=True in _add_job_internal).
        # Non-structural fields round-trip via extra_meta, exactly as _load_jobs
        # does — without this, ANY update silently stripped allowed_tools,
        # last_fired_at, session_mode and created_at from the job (field case:
        # a cron_expr edit dropped the curiosity deep-dive's allow-list).
        extra = {k: v for k, v in current.items() if k not in _ENTRY_STRUCTURAL_KEYS}
        _add_job_internal(
            name, new_cron, new_prompt, session_id=current.get("session_id"), model=new_model, extra_meta=extra
        )
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
                # Spec-validation + last dry-run outcomes (spec Feature 7).
                # None for pre-feature jobs — the UI renders those as
                # "unvalidated", which is the honest state.
                "validation": job.get("validation"),
                "last_test": job.get("last_test"),
                "allowed_tools": job.get("allowed_tools"),
                "space_id": job.get("space_id"),
            }
        )
    return result


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


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
        description=(
            "Schedule a recurring job with 5-field cron expression. Job sends a prompt to a session on "
            "schedule. For a multi-step pipeline, write the prompt to load the relevant skill and follow "
            "its steps, spawning workers for the heavy ones."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Unique job name"},
                "cron_expr": {"type": "string", "description": "Cron expression (5-field: min hour day month weekday)"},
                "prompt": {"type": "string", "description": "Message to send when job fires"},
                "session_id": {"type": "string", "description": "Session to target (empty = create new each time)"},
                "model": {"type": "string", "description": "Model override for this job (empty = default model)"},
                "space_id": {
                    "type": "string",
                    "description": "Bind the job to a space: each firing runs in a fresh session inside it. "
                    "Empty = inherit the calling session's space (if any); 'none' = explicitly unbound",
                },
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
        name="test_job",
        func=test_job,
        description=(
            "Dry-run a scheduled job's prompt ONCE in an isolated temp workspace, under the "
            "job's own model and tool allow-list. Reports pass/fail, spec validation, and an "
            "answer preview — without recording a run or touching the schedule. Use after "
            "schedule_job/update_scheduled_job to prove the job actually works before it fires."
        ),
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Job name to test"}},
            "required": ["name"],
        },
        tags=tags + ["test", "dry-run", "validate", "verify", "smoke"],
        timeout=_JOB_TEST_TIMEOUT_S + _JOB_TEST_CANCEL_GRACE_S + 90,
        parallel_safe=False,
        long_poll=True,
        safety_level="safe",
        denied_session_types={"worker", "canary"},
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
