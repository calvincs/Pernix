"""Pernix — Session management endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from api.streaming import event_stream, sse_response
from db import models as db
from sessions.manager import get_manager

router = APIRouter(tags=["sessions"])


@router.post("/api/sessions")
async def create_session(body: dict = {}):
    manager = get_manager()
    session_type = body.get("session_type", "normal")
    if session_type not in ("normal", "worker", "cron"):
        session_type = "normal"
    sid = manager.create_session(
        title=body.get("title", "New session"),
        system_prompt=body.get("system_prompt", ""),
        session_type=session_type,
        parent_session_id=body.get("parent_session_id"),
    )
    return {"session_id": sid}


@router.get("/api/sessions")
async def list_sessions(limit: int = 50, offset: int = 0):
    sessions = db.list_sessions_enriched(limit=limit, offset=offset)
    return {"items": sessions, "count": len(sessions)}


@router.get("/api/sessions/{session_id}")
async def get_session(session_id: str):
    session = db.get_session(session_id)
    if not session:
        raise HTTPException(404, detail=f"Session {session_id} not found")
    messages = db.get_messages(session_id)
    return {**session, "messages": messages}


@router.get("/api/sessions/{session_id}/status")
async def get_session_status(session_id: str):
    manager = get_manager()
    status = manager.get_status(session_id)
    if status["status"] == "unknown":
        # Check DB
        session = db.get_session(session_id)
        if not session:
            raise HTTPException(404, detail=f"Session {session_id} not found")
        return {"session_id": session_id, "status": "idle", "in_memory": False}
    return status


@router.get("/api/sessions/{session_id}/events")
async def session_events(session_id: str, request: Request):
    """Persistent SSE event stream. Supports Last-Event-ID reconnection."""
    manager = get_manager()
    try:
        session = manager.get_or_create(session_id)
    except ValueError:
        raise HTTPException(404, detail=f"Session {session_id} not found")

    last_id = 0
    raw = request.headers.get("Last-Event-ID", "0")
    try:
        last_id = int(raw)
    except ValueError:
        pass

    return sse_response(event_stream(session, last_event_id=last_id))


@router.post("/api/sessions/{session_id}/cancel")
async def cancel_session(session_id: str):
    manager = get_manager()
    session = manager.get(session_id)
    if not session:
        raise HTTPException(404, detail=f"Session {session_id} not found in memory")

    # 1. Set cooperative cancellation flag (checked by agent loop + tools)
    session.cancel_requested = True

    # 2. Cascade cancel to all worker sessions
    for wid in list(session.worker_ids):
        worker = manager.get(wid)
        if worker:
            worker.cancel_requested = True
            worker.pending_messages.clear()
            if worker.task and not worker.task.done():
                worker.task.cancel()

    # 3. Clear pending message queue (prevent re-processing after cancel).
    # Record the dropped count as a transcript-visible notice so readers can
    # tell the queue was abandoned (not silently lost). The "notice" role is
    # filtered from LLM context by core/context/compiler.py.
    dropped = len(session.pending_messages)
    session.pending_messages.clear()
    if dropped > 0:
        try:
            db.add_message(
                session_id,
                "notice",
                f"[{dropped} queued message(s) dropped — session cancelled]",
            )
        except Exception as _e:
            import logging

            logging.getLogger("pernix.api").debug("Cancel-queue notice insert skipped: %s", _e)
        try:
            session.emit_event(
                {
                    "type": "session.queue_dropped",
                    "count": dropped,
                    "reason": "cancelled",
                }
            )
        except Exception:
            pass

    # 4. Kill any tracked subprocess (bash commands, etc.)
    _kill_session_process(session)

    # 5. Cancel the parent asyncio task — the v2 state machine's
    # CancelledError path inside _run_agent_safe will transition
    # through CANCELLING → IDLE_READY cleanly (with post-hooks skipped).
    if session.task and not session.task.done():
        session.task.cancel()
    else:
        # No running task (e.g. session parked in AWAITING_USER after
        # ask_user). The CancelledError path won't fire, so transition
        # explicitly so the state log + SSE event go out.
        from sessions import state_v2 as sv2

        current = sv2._current_state(session)
        if current == sv2.SessionStateV2.AWAITING_USER:
            try:
                sv2.transition(
                    session,
                    sv2.SessionStateV2.CANCELLING,
                    "cancel-requested",
                    termination_reason=sv2.TerminationReason.CANCELLED,
                )
                sv2.transition(
                    session,
                    sv2.SessionStateV2.IDLE_READY,
                    "cancel-complete",
                )
                db.set_session_state(session_id, session.state.value)
            except Exception:
                pass

    session.error = None
    session.last_scout_report = None
    session.emit_event({"type": "session.cancelled"})
    return {"status": "cancelled"}


def _kill_session_process(session):
    """Kill any tracked subprocess for this session."""
    import os
    import signal

    proc = getattr(session, "_active_process", None)
    if proc and proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
        session._active_process = None


@router.post("/api/sessions/{session_id}/clear")
async def clear_session(session_id: str):
    db.clear_messages_only(session_id)
    db.update_session(session_id, title="New session")
    return {"status": "cleared"}


@router.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    manager = get_manager()
    manager.delete_session(session_id)
    return {"status": "deleted"}


@router.get("/api/sessions/{session_id}/state-log")
async def get_session_state_log(session_id: str, since_id: int = 0, limit: int = 500):
    """Return the persisted state-machine transition log for this session.

    Backing store: `session_state_log` (migration v13). Rows are append-only
    and written inside sessions.state_v2.transition(). In Stage 0 of the
    state-machine migration this endpoint may return an empty list until
    the mutator starts being called (Stage 1+)."""
    session = db.get_session(session_id)
    if not session:
        raise HTTPException(404, detail=f"Session {session_id} not found")
    limit = max(1, min(limit, 5000))
    entries = db.get_state_log(session_id, since_id=since_id, limit=limit)
    return {"session_id": session_id, "count": len(entries), "entries": entries}


@router.post("/api/sessions/{session_id}/workers/{worker_id}/pause")
async def http_pause_worker(session_id: str, worker_id: str):
    """Pause a worker. Routes through the same state-machine transition as
    the `pause_worker` agent tool, so the UI and tool paths behave identically."""
    manager = get_manager()
    parent = manager.get(session_id)
    if not parent:
        raise HTTPException(404, detail=f"Session {session_id} not found")
    if worker_id not in parent.worker_ids:
        raise HTTPException(404, detail=f"Worker {worker_id} not a child of {session_id}")
    if not manager.get(worker_id):
        raise HTTPException(404, detail=f"Worker {worker_id} not in memory")
    from core.extensions.orchestration import pause_worker as _pw

    msg = _pw(worker_id)
    return {"status": "pause_requested", "worker_id": worker_id, "detail": msg}


@router.post("/api/sessions/{session_id}/workers/{worker_id}/resume")
async def http_resume_worker(session_id: str, worker_id: str):
    """Resume a paused worker. Mirror of pause above."""
    manager = get_manager()
    parent = manager.get(session_id)
    if not parent:
        raise HTTPException(404, detail=f"Session {session_id} not found")
    if worker_id not in parent.worker_ids:
        raise HTTPException(404, detail=f"Worker {worker_id} not a child of {session_id}")
    if not manager.get(worker_id):
        raise HTTPException(404, detail=f"Worker {worker_id} not in memory")
    from core.extensions.orchestration import resume_worker as _rw

    msg = _rw(worker_id)
    return {"status": "resumed", "worker_id": worker_id, "detail": msg}


@router.post("/api/sessions/purge")
async def purge_sessions(body: dict = {}):
    """Bulk delete old sessions."""
    keep_days = body.get("keep_days", 7)
    keep_min = body.get("keep_min", 5)

    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()

    sessions = db.list_sessions(limit=1000)
    # Sort by updated_at, keep at least keep_min
    candidates = [s for s in sessions if (s.get("updated_at") or "") < cutoff]
    to_delete = candidates[keep_min:] if len(candidates) > keep_min else []

    manager = get_manager()
    for s in to_delete:
        manager.delete_session(s["id"])

    return {"purged": len(to_delete)}
