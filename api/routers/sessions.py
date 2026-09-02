"""Pernix — Session management endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from api.streaming import event_stream, sse_response
from db import models as db
from sessions.manager import get_manager

router = APIRouter(tags=["sessions"])

# Matches HISTORY_PAGE in static/js/app.js — the transcript window the client
# asks for, and the fallback when a caller passes a cursor with no size.
DEFAULT_HISTORY_PAGE = 200


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
async def list_sessions(limit: int = 50, offset: int = 0, archived: bool = False):
    """One page of sessions, newest first, plus how many there are in total.

    `total`/`has_more` are what let the sidebar offer the page behind this one
    instead of a dead "showing the 500 most recent" note: sessions past the
    horizon used to be reachable only by full-text search, which is no help
    when what you remember is the session, not a phrase inside it.

    `has_more` is measured against the requested window, not the rows
    returned — list_sessions_enriched unions space sessions back in past the
    recency cut, so the response can be longer than `limit` while the page
    itself is still exactly `limit` deep.

    Archived sessions are absent by default: leaving the list is what
    archiving IS. `archived=1` returns the same shape over that set instead,
    and `total`/`has_more` then count only it. `archived_count` rides on
    both answers so the sidebar can offer "Archived (N)" without a second
    round trip — and, when it is zero, say nothing at all.
    """
    import asyncio as _asyncio

    from sessions.policy import annotate_read_only

    rows = await _asyncio.to_thread(db.list_sessions_enriched, limit, offset, archived=archived)
    sessions = [annotate_read_only(s) for s in rows]
    spaces = await _asyncio.to_thread(db.list_spaces)
    total = await _asyncio.to_thread(db.count_sessions, archived=archived)
    archived_count = total if archived else await _asyncio.to_thread(db.count_sessions, archived=True)
    return {
        "items": sessions,
        "count": len(sessions),
        "spaces": spaces,
        "total": total,
        "has_more": (offset + limit) < total,
        "archived": archived,
        "archived_count": archived_count,
    }


@router.get("/api/sessions/search")
async def search_sessions(q: str = "", limit: int = 20):
    """Full-text search across all session messages (FTS5). Groups hits by
    session for the sidebar search box. The index has existed all along —
    this endpoint finally exposes it to the user.

    Archived sessions are deliberately still findable here — search is the
    promise that archiving hides a conversation without losing it — so each
    hit carries `archived` and the sidebar marks those rows."""
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
                "archived": bool(h.get("session_archived")),
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
async def get_session(session_id: str, limit: int | None = None, before_id: int | None = None):
    """Session metadata + messages. With `limit`, only the newest N messages
    (oldest-first) plus a total count — the UI uses this so opening a long
    session doesn't load (and render) the entire unbounded transcript.

    `before_id` pages further back: the newest `limit` rows OLDER than that
    id, and nothing the client already holds. That is what makes "load
    earlier" a prepend instead of a re-render of the whole transcript.

    `has_more` answers "is there anything behind the page I just got" —
    computed from the oldest row returned, so it stays correct on both the
    first page and every page after it.

    The row carries `archived_at` (NULL when live) alongside `read_only` /
    `read_only_reason`: this is how the client knows to show Restore rather
    than just a disabled composer, and it is the only lookup that finds a
    session the sidebar list no longer contains.
    """
    import asyncio as _asyncio

    from sessions.policy import annotate_read_only

    session = await _asyncio.to_thread(db.get_session, session_id)
    if not session:
        raise HTTPException(404, detail=f"Session {session_id} not found")
    annotate_read_only(session)
    # A cursor with no window size is a whole-transcript read wearing a page's
    # clothes; give it the default page instead of ignoring it.
    if before_id is not None and limit is None:
        limit = DEFAULT_HISTORY_PAGE
    messages = await _asyncio.to_thread(db.get_messages, session_id, limit, before_id)
    total = await _asyncio.to_thread(db.count_messages, session_id) if limit is not None else len(messages)
    has_more = False
    if limit is not None and messages:
        oldest_id = messages[0].get("id")
        if oldest_id is not None:
            has_more = await _asyncio.to_thread(db.count_messages, session_id, int(oldest_id)) > 0
    return {**session, "messages": messages, "total_messages": total, "has_more": has_more}


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
        # event_seq is stated as null rather than omitted: the client used
        # to read a missing value as 0, which looks exactly like "the server
        # restarted and its counter reset" and triggered a spurious
        # transcript reload plus a scroll jump every time someone came back
        # to a tab whose idle session had simply been reaped from memory.
        return {"session_id": session_id, "status": "idle", "in_memory": False, "event_seq": None}
    return {**status, "in_memory": True}


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
    """Kill every tracked subprocess for this session (shared with the
    manager's cancel path; escalates TERM -> KILL)."""
    from sessions.manager import kill_session_processes

    kill_session_processes(session)


def require_idle(session_id: str, action: str) -> None:
    """409 unless the session is idle enough to have its transcript rewritten.

    Retry and clear delete messages the running turn is still working from:
    the agent's next round then compiles a history with no root for its own
    tool calls, and manager.prompt QUEUES the re-prompt behind the live turn
    instead of replacing it, so the user gets the work twice. Compaction has
    always guarded this way; these two did not.
    """
    from sessions import state_v2 as _sv2

    session = get_manager().get(session_id)
    if session is None:
        return  # not resident: no turn can be running
    current = _sv2._current_state(session)
    if current not in (_sv2.SessionStateV2.IDLE_READY, _sv2.SessionStateV2.AWAITING_USER):
        raise HTTPException(409, detail=f"Session is {current.value}; cancel it before you {action}")


@router.post("/api/sessions/{session_id}/clear")
async def clear_session(session_id: str):
    import asyncio as _asyncio

    require_idle(session_id, "clear it")
    await _asyncio.to_thread(db.clear_messages_only, session_id)
    await _asyncio.to_thread(db.update_session, session_id, title="New session")
    return {"status": "cleared"}


@router.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    # Async form: cancels the turn and kills its subprocesses on the loop,
    # then does the DB cascade and file cleanup off it.
    await get_manager().delete_session_async(session_id)
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
      * space_id       — move to a space (validated id) or, with null/"",
                         remove from one.
      * archived       — bool; true stamps archived_at with now, false
                         clears it. An archived session leaves the sidebar
                         and its space group, keeps every message, stays
                         searchable, and opens read-only with a Restore
                         control. Delete remains a separate, explicit act.
      * model_override — model id string sets a persistent per-session
                         override; "" or null clears it. Lives on the
                         in-memory session (not persisted across restart),
                         and unlike agent-initiated switch_model it is NOT
                         reverted at turn end.

    Nothing here bumps updated_at (set_session_meta's contract): recency
    ordering is what the sidebar's buckets and the idle horizon are computed
    from, so archiving must not reshuffle the list and restoring must put a
    session back exactly where it was.
    """
    import asyncio as _asyncio

    from sessions.policy import annotate_read_only

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

    if "archived" in body:
        archived = bool(body["archived"])
        await _asyncio.to_thread(db.set_session_meta, session_id, archived=archived)
        result["archived"] = archived
        # Same shape as the title update: the sidebar repaints from its own
        # optimistic state, and this is what tells the OTHER tab (and the
        # session that is open in it) that the composer has just changed
        # sides.
        row = await _asyncio.to_thread(db.get_session, session_id) or {}
        verdict = annotate_read_only(dict(row))
        get_manager().emit(
            session_id,
            {
                "type": "session.archived",
                "archived": archived,
                "archived_at": verdict.get("archived_at"),
                # The client must not re-derive "is this read-only" a third
                # time: a session can be read-only for reasons archiving does
                # not own (dream journals, RLM views), and sessions.policy is
                # the one place that rule lives.
                "read_only": verdict.get("read_only"),
                "read_only_reason": verdict.get("read_only_reason"),
            },
        )

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


def _non_negative_int(value, name: str) -> int:
    """A purge knob, or a 400. Silently coercing garbage here would delete
    the wrong set of sessions and report a number for it."""
    if isinstance(value, bool) or isinstance(value, (list, dict)):
        raise HTTPException(400, detail=f"{name} must be a non-negative integer")
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise HTTPException(400, detail=f"{name} must be a non-negative integer")
    if n < 0:
        raise HTTPException(400, detail=f"{name} must be a non-negative integer")
    return n


@router.post("/api/sessions/archive-idle")
async def archive_idle(body: dict = {}):
    """Archive ordinary chats idle for more than `days` — or say what it would.

    Body: ``{days: <session_archive_idle_days>, space_id: null, dry_run:
    false}``. `space_id` narrows the sweep to one space, which is what the
    space header's "Archive idle sessions..." asks for; omit it to sweep
    everything.

    Nothing is deleted and nothing is lost: the sessions leave the sidebar
    and their space group, keep every message, stay searchable, and come
    back with one PATCH. Pinned chats are exempt.

    A dry run computes exactly the same set as the real one, so the count in
    the confirmation dialog is a promise this endpoint keeps. Returns
    ``{count, ids, sample, days, dry_run}``.
    """
    import asyncio as _asyncio

    from config import settings as _settings
    from core import retention

    body = body or {}
    raw_days = body.get("days", None)
    days = _non_negative_int(raw_days if raw_days is not None else _settings.session_archive_idle_days, "days")
    space_id = body.get("space_id") or None
    if space_id and not await _asyncio.to_thread(db.get_space, space_id):
        raise HTTPException(404, detail=f"Space {space_id} not found")
    return await _asyncio.to_thread(
        retention.archive_idle_sessions,
        days,
        dry_run=bool(body.get("dry_run", False)),
        space_id=space_id,
    )


@router.post("/api/sessions/purge")
async def purge_sessions(body: dict = {}):
    """Bulk-delete stale ordinary sessions — or, with dry_run, say what it would.

    Body: ``{keep_days: 7, keep_min: 5, dry_run: false}``. keep_days 0 means
    "everything already idle"; keep_min is how many of the stale candidates
    survive regardless, newest first.

    Candidates are ordinary user chats only: unpinned, spaceless, session_type
    'normal', last touched before the cutoff. Typed sessions (canary, worker,
    cron, rlm, snooze) each have their own retention horizon in
    core/retention.py and are counted, not deleted. The scan is the whole
    table — it used to be the 1,000 most recently updated rows, which hid
    exactly the oldest sessions a purge is aimed at.

    Both modes compute the same set from the same query, so a dry run is a
    promise the real run keeps.
    """
    body = body or {}
    keep_days = _non_negative_int(body.get("keep_days", 7), "keep_days")
    keep_min = _non_negative_int(body.get("keep_min", 5), "keep_min")
    dry_run = bool(body.get("dry_run", False))

    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()

    import asyncio as _asyncio

    found = await _asyncio.to_thread(db.list_purge_candidates, cutoff)
    candidates = found["candidates"]
    to_delete = candidates[keep_min:]

    purged = 0
    if not dry_run:
        manager = get_manager()
        for s in to_delete:
            manager.delete_session(s["id"])
            purged += 1

    return {
        "dry_run": dry_run,
        "keep_days": keep_days,
        "keep_min": keep_min,
        "cutoff": cutoff,
        "candidates": len(candidates),
        "would_delete": len(to_delete),
        "purged": purged,
        "sample": [{k: s[k] for k in ("id", "title", "updated_at", "message_count")} for s in to_delete[:10]],
        "skipped": found["skipped"],
    }
