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
    space_id = body.get("space_id") or None
    if space_id and not db.get_space(space_id):
        raise HTTPException(404, detail=f"Space {space_id} not found")
    sid = manager.create_session(
        title=body.get("title", "New session"),
        system_prompt=body.get("system_prompt", ""),
        session_type=session_type,
        parent_session_id=body.get("parent_session_id"),
        space_id=space_id,
    )
    return {"session_id": sid}


@router.get("/api/sessions")
async def list_sessions(limit: int = 50, offset: int = 0):
    import asyncio as _asyncio

    from sessions.policy import annotate_read_only

    rows = await _asyncio.to_thread(db.list_sessions_enriched, limit, offset)
    sessions = [annotate_read_only(s) for s in rows]
    spaces = await _asyncio.to_thread(db.list_spaces)
    return {"items": sessions, "count": len(sessions), "spaces": spaces}


@router.get("/api/sessions/search")
async def search_sessions(q: str = "", limit: int = 20):
    """Full-text search across all session messages (FTS5). Groups hits by
    session for the sidebar search box. The index has existed all along —
    this endpoint finally exposes it to the user."""
    if len(q.strip()) < 2:
        return {"results": []}
    import asyncio as _asyncio

    hits = await _asyncio.to_thread(db.search_messages_fts, q, limit)
    # Group by session, keep the best-ranked snippet per session.
    grouped: dict[str, dict] = {}
    for h in hits:
        sid = h["session_id"]
        if sid not in grouped:
            grouped[sid] = {
                "session_id": sid,
                "title": h["session_title"],
                "session_type": h["session_type"],
                "space_id": h.get("session_space_id"),
                "updated_at": h["session_updated_at"],
                "snippet": h["content"],
                "matches": 1,
            }
        else:
            grouped[sid]["matches"] += 1
    return {"results": list(grouped.values())}


@router.get("/api/sessions/{session_id}/goal")
async def get_session_goal(session_id: str):
    """Active goal + live burn for the session header (plan §12.6)."""
    import asyncio as _asyncio

    goal = await _asyncio.to_thread(db.get_active_goal, session_id)
    if not goal:
        return {"goal": None}
    tokens_used = await _asyncio.to_thread(db.goal_token_usage, int(goal["id"]))
    return {"goal": {**goal, "tokens_used": tokens_used}}


@router.get("/api/sessions/{session_id}/gates")
async def get_session_gates(session_id: str):
    """Deterministic gates registered on the session (plan §12.6)."""
    import asyncio as _asyncio

    return {"gates": await _asyncio.to_thread(db.get_gates, session_id, False)}


@router.get("/api/kernel/status")
async def kernel_status():
    """Live kernel counts + whether THIS deployment has kernels enabled."""
    from config import settings

    out = {"enabled": settings.session_kernel_enabled, "kernels": 0, "alive": 0, "max": settings.kernel_max_concurrent}
    if settings.session_kernel_enabled:
        try:
            from core.kernel import get_kernel_registry

            out.update(get_kernel_registry().stats())
        except Exception:
            pass
    return out


@router.get("/api/sessions/{session_id}/workers")
async def list_workers(session_id: str):
    """Workers spawned by this session, with live state — feeds the worker
    activity strip so a fan-out isn't an opaque 'awaiting workers' wait."""
    import asyncio as _asyncio

    from sessions import state_v2 as sv2

    rows = await _asyncio.to_thread(db.get_worker_sessions, session_id)
    manager = get_manager()
    out = []
    for r in rows:
        # get_worker_sessions returns ALL children; RLM view sessions
        # (session_type='rlm') are read-only trace anchors, not workers —
        # listing them here made the strip draw them as teal worker chips
        # that never retired (their finished state is 'idle', not
        # 'idle_ready'). The strip gets its RLM chips from /api/rlm/runs.
        if (r.get("session_type") or "worker") != "worker":
            continue
        w = manager.get(r["id"])
        state = sv2._current_state(w).value if w is not None else (r.get("state_v2") or r.get("state") or "unknown")
        out.append(
            {
                "id": r["id"],
                "title": r.get("title") or "worker",
                "state": state,
                "kind": r.get("worker_kind") or "",
                "model": r.get("model_override") or "",
                "termination_reason": (w.termination_reason if w is not None else None),
                "in_memory": w is not None,
                "created_at": r.get("created_at"),
                "updated_at": r.get("updated_at"),
            }
        )
    return {"workers": out}


@router.get("/api/sessions/{session_id}")
async def get_session(session_id: str, limit: int | None = None):
    """Session metadata + messages. With `limit`, only the newest N messages
    (oldest-first) plus a total count — the UI uses this so opening a long
    session doesn't load (and render) the entire unbounded transcript."""
    import asyncio as _asyncio

    from sessions.policy import annotate_read_only

    session = await _asyncio.to_thread(db.get_session, session_id)
    if not session:
        raise HTTPException(404, detail=f"Session {session_id} not found")
    annotate_read_only(session)
    messages = await _asyncio.to_thread(db.get_messages, session_id, limit)
    total = await _asyncio.to_thread(db.count_messages, session_id) if limit is not None else len(messages)
    return {**session, "messages": messages, "total_messages": total, "has_more": total > len(messages)}


@router.get("/api/sessions/{session_id}/status")
async def get_session_status(session_id: str):
    manager = get_manager()
    status = manager.get_status(session_id)
    if status["status"] == "unknown":
        # Check DB
        import asyncio as _asyncio

        session = await _asyncio.to_thread(db.get_session, session_id)
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

    # Header takes precedence (native browser auto-reconnect uses it).
    # Query-param fallback exists because JS-instantiated EventSource (used by
    # the client's stale-stream watchdog) cannot set request headers; without
    # this fallback every watchdog reconnect would skip replay and lose events.
    last_id = 0
    raw = request.headers.get("Last-Event-ID") or request.query_params.get("last_event_id", "0")
    try:
        last_id = int(raw)
    except (ValueError, TypeError):
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
            except Exception:
                pass

    session.error = None
    session.last_scout_report = None
    session.emit_event({"type": "session.cancelled"})
    return {"status": "cancelled"}


def _kill_session_process(session):
    """Kill every tracked subprocess for this session.

    Cancel is session-wide, so it sweeps all registrations rather than a single
    slot — concurrent bash calls each register their own, and cancelling the
    session must not leave the others running.
    """
    import os
    import signal

    for proc in session.all_processes():
        if proc is None or proc.poll() is not None:
            continue
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
    session._active_processes.clear()


@router.post("/api/sessions/{session_id}/clear")
async def clear_session(session_id: str):
    import asyncio as _asyncio

    await _asyncio.to_thread(db.clear_messages_only, session_id)
    await _asyncio.to_thread(db.update_session, session_id, title="New session")
    return {"status": "cleared"}


@router.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    manager = get_manager()
    manager.delete_session(session_id)
    return {"status": "deleted"}


@router.get("/api/sessions/{session_id}/pending")
async def list_pending_messages(session_id: str):
    """List messages queued behind the running turn (in-memory deque).

    Entries are PendingMessage records on session.pending_messages; only
    those with a persisted msg_id are listed — those are the ones rendered
    in the transcript and removable. Synthetic entries (worker resume,
    worker timeout) carry no DB row and are skipped."""
    import asyncio as _asyncio

    from sessions.state import PendingMessage

    if not await _asyncio.to_thread(db.get_session, session_id):
        raise HTTPException(404, detail=f"Session {session_id} not found")
    session = get_manager().get(session_id)
    pending = []
    if session:
        for raw in session.pending_messages:
            entry = PendingMessage.coerce(raw)
            if entry.msg_id is not None:
                pending.append({"message_id": entry.msg_id, "preview": (entry.message or "")[:200]})
    return {"session_id": session_id, "pending": pending}


@router.delete("/api/sessions/{session_id}/pending/{message_id}")
async def remove_pending_message(session_id: str, message_id: int):
    """Remove a queued message before the agent picks it up.

    Deletes both the in-memory deque entry and the persisted DB row — the
    row must go too, or the orphan-recovery sweep would re-queue it on the
    next prompt/restart. Runs on the event loop, so removal can't interleave
    with _process_pending's popleft."""
    session = get_manager().get(session_id)
    if not session:
        raise HTTPException(404, detail=f"Session {session_id} not found in memory")
    import asyncio as _asyncio

    from sessions.state import PendingMessage

    for entry in list(session.pending_messages):
        if PendingMessage.coerce(entry).msg_id == message_id:
            session.pending_messages.remove(entry)
            await _asyncio.to_thread(db.delete_message, message_id)
            if session.last_user_msg_id == message_id:
                session.last_user_msg_id = None
            session.emit_event(
                {
                    "type": "session.queue_removed",
                    "message_id": message_id,
                    "queue_depth": len(session.pending_messages),
                }
            )
            return {"status": "removed", "message_id": message_id, "queue_depth": len(session.pending_messages)}
    raise HTTPException(404, detail=f"Message {message_id} is not queued (already picked up?)")


@router.patch("/api/sessions/{session_id}")
async def patch_session(session_id: str, body: dict = {}):
    """Update user-facing session attributes.

    Accepted keys (absent keys are left unchanged):
      * title          — rename; must be a non-empty string.
      * pinned         — bool; pinned sessions sort to the top of the sidebar.
      * model_override — model id string sets a persistent per-session
                         override; "" or null clears it. Lives on the
                         in-memory session (not persisted across restart),
                         and unlike agent-initiated switch_model it is NOT
                         reverted at turn end.
    """
    import asyncio as _asyncio

    if not await _asyncio.to_thread(db.get_session, session_id):
        raise HTTPException(404, detail=f"Session {session_id} not found")

    result: dict = {"session_id": session_id}

    if "title" in body:
        title = body["title"]
        if not isinstance(title, str) or not title.strip():
            raise HTTPException(400, detail="title must be a non-empty string")
        title = title.strip()[:300]
        await _asyncio.to_thread(db.set_session_meta, session_id, title=title)
        result["title"] = title
        get_manager().emit(session_id, {"type": "session.title", "title": title})

    if "pinned" in body:
        pinned = bool(body["pinned"])
        await _asyncio.to_thread(db.set_session_meta, session_id, pinned=pinned)
        result["pinned"] = pinned

    if "space_id" in body:
        # Move to space (validated id) or remove from space (null/"").
        space_id = body["space_id"] or None
        if space_id and not await _asyncio.to_thread(db.get_space, space_id):
            raise HTTPException(404, detail=f"Space {space_id} not found")
        await _asyncio.to_thread(db.set_session_meta, session_id, space_id=space_id)
        live = get_manager().get(session_id)
        if live is not None:
            from sessions.manager import _apply_space_fields

            live.space_id = None
            live.workspace_home = None
            _apply_space_fields(live, space_id)
        result["space_id"] = space_id

    if "model_override" in body:
        override = body["model_override"]
        if override is not None and not isinstance(override, str):
            raise HTTPException(400, detail="model_override must be a string or null")
        override = (override or "").strip() or None
        session = get_manager().get_or_create(session_id)
        session.model_override = override
        if override is None:
            session.context_budget_override = None
        result["model_override"] = override

    return result


@router.get("/api/sessions/{session_id}/state-log")
async def get_session_state_log(
    session_id: str,
    since_id: int = 0,
    before_id: int = 0,
    limit: int = 500,
    tail: bool = False,
):
    """Return the persisted state-machine transition log for this session.

    Backing store: `session_state_log` (migration v13). Rows are append-only
    and written inside sessions.state_v2.transition(). In Stage 0 of the
    state-machine migration this endpoint may return an empty list until
    the mutator starts being called (Stage 1+).

    `tail=true` returns the newest `limit` rows; `before_id` pages backward
    from a tail window. Rows are always oldest-first within the window."""
    import asyncio as _asyncio

    session = await _asyncio.to_thread(db.get_session, session_id)
    if not session:
        raise HTTPException(404, detail=f"Session {session_id} not found")
    limit = max(1, min(limit, 5000))
    entries = await _asyncio.to_thread(
        db.get_state_log, session_id, since_id=since_id, before_id=before_id, limit=limit, tail=tail
    )
    return {"session_id": session_id, "count": len(entries), "entries": entries}


@router.post("/api/sessions/{session_id}/pause")
async def http_pause_session(session_id: str):
    """Pause ANY session (not just workers) at its next pre-round checkpoint.
    The agent loop's pause gate is type-agnostic; for minutes-long turns
    'pause and let me redirect' is gentler than cancel."""
    manager = get_manager()
    if not manager.get(session_id):
        raise HTTPException(404, detail=f"Session {session_id} not found in memory")
    from core.extensions.orchestration import pause_worker as _pause

    msg = _pause(session_id)
    return {"status": "pause_requested", "session_id": session_id, "detail": msg}


@router.post("/api/sessions/{session_id}/resume")
async def http_resume_session(session_id: str):
    """Resume a paused session. Mirror of pause above."""
    manager = get_manager()
    if not manager.get(session_id):
        raise HTTPException(404, detail=f"Session {session_id} not found in memory")
    from core.extensions.orchestration import resume_worker as _resume

    msg = _resume(session_id)
    return {"status": "resumed", "session_id": session_id, "detail": msg}


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
async def http_resume_worker(session_id: str, worker_id: str, body: dict = {}):
    """Resume a paused worker — or REVIVE a terminated/reaped one.

    Parentage is checked against the DB row, not the in-memory worker_ids
    list: after a server restart the parent's in-memory list is empty, and
    revival (spec Feature 5) exists precisely for that case. Optional body
    {"note": "..."} is injected into the continuation turn."""
    import asyncio as _asyncio

    manager = get_manager()
    if not manager.get(session_id):
        raise HTTPException(404, detail=f"Session {session_id} not found in memory")
    row = await _asyncio.to_thread(db.get_session, worker_id)
    if not row:
        raise HTTPException(404, detail=f"Worker {worker_id} not found")
    if row.get("parent_session_id") != session_id:
        raise HTTPException(404, detail=f"Worker {worker_id} not a child of {session_id}")
    from core.extensions.orchestration import resume_worker as _rw

    msg = _rw(worker_id, note=str((body or {}).get("note") or ""))
    return {"status": "resumed", "worker_id": worker_id, "detail": msg}


@router.post("/api/sessions/purge")
async def purge_sessions(body: dict = {}):
    """Bulk delete old sessions."""
    keep_days = body.get("keep_days", 7)
    keep_min = body.get("keep_min", 5)

    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()

    import asyncio as _asyncio

    sessions = await _asyncio.to_thread(db.list_sessions, 1000)
    # Sort by updated_at, keep at least keep_min. Space sessions are
    # long-lived by contract (v33) — the bulk sweep never touches them;
    # they go only via explicit delete or their space's cascade delete.
    candidates = [s for s in sessions if (s.get("updated_at") or "") < cutoff and not s.get("space_id")]
    to_delete = candidates[keep_min:] if len(candidates) > keep_min else []

    manager = get_manager()
    for s in to_delete:
        manager.delete_session(s["id"])

    return {"purged": len(to_delete)}
