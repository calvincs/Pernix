"""Pernix — Async dialog question and notification endpoints."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import StreamingResponse

from db import models as db

router = APIRouter(tags=["questions"])


@router.get("/api/questions")
async def list_questions():
    questions = db.get_questions()
    return {"questions": questions}


@router.post("/api/questions/{question_id}/answer")
async def answer_question(question_id: str, body: dict):
    """Answer a question and deliver to the session as a follow-up message."""
    questions = db.get_questions()
    question = next((q for q in questions if q["id"] == question_id), None)
    if not question:
        raise HTTPException(404, detail="Question not found")

    answer = body.get("answer", "")
    session_id = question["session_id"]

    # Delete first (atomic guard) — if another request already handled it, stop
    from db.database import connect_sessions

    with connect_sessions() as conn:
        cur = conn.execute("DELETE FROM questions WHERE id = ?", (question_id,))
        if cur.rowcount == 0:
            raise HTTPException(409, detail="Question already handled")

    # Format as a user message
    context_field = question.get("context", "")
    formatted = (
        f"[User answered your question]\n"
        f"Q: {question['question']}\n" + (f"Context: {context_field}\n" if context_field else "") + f"A: {answer}"
    )

    # Deliver to session. manager.prompt() will accept because v2
    # AWAITING_USER mirrors legacy state=IDLE. _run_agent_safe detects
    # that the starting state was AWAITING_USER and uses reason=
    # "answer-received" for the first transition, which sets parent_turn_id
    # in the state_log so consumers can link the answer turn back to
    # the ask_user turn.
    from sessions.manager import get_manager

    manager = get_manager()
    await manager.prompt(session_id, formatted)

    # Notify all connected clients so other tabs can close the modal and update the chat
    manager.emit(
        session_id,
        {
            "type": "dialog.answered",
            "question_id": question_id,
            "question": question["question"],
            "answer": answer,
        },
    )

    return {"status": "answered", "session_id": session_id}


@router.post("/api/questions/{question_id}/dismiss")
async def dismiss_question(question_id: str):
    # Look up question before deleting so we have the text for the agent message.
    questions = db.get_questions()
    question = next((q for q in questions if q["id"] == question_id), None)

    # Atomic delete — safe against concurrent dismiss/answer
    from db.database import connect_sessions

    with connect_sessions() as conn:
        cur = conn.execute("DELETE FROM questions WHERE id = ?", (question_id,))
        already_handled = cur.rowcount == 0

    if not question or already_handled:
        return {"status": "dismissed"}

    from sessions import state_v2 as sv2
    from sessions.manager import get_manager

    manager = get_manager()
    session_id = question["session_id"]

    # Notify clients first so the modal closes and the bubble is marked
    # dismissed before the agent's follow-up turn arrives.
    manager.emit(session_id, {"type": "dialog.dismissed", "question_id": question_id})

    # Deliver a dismissal message to the agent so it can continue the
    # workflow rather than staying silently blocked.
    session_obj = manager.get(session_id)
    if session_obj is not None:
        current = sv2._current_state(session_obj)
        if current == sv2.SessionStateV2.AWAITING_USER:
            formatted = "[User dismissed your question without answering]\n" f"Q: {question['question']}"
            try:
                await manager.prompt(session_id, formatted)
            except Exception:
                # Fallback: transition to idle so the session isn't stuck
                # with no question row in AWAITING_USER.
                try:
                    sv2.transition(session_obj, sv2.SessionStateV2.IDLE_READY, "question-dismissed")
                    db.set_session_state(session_id, session_obj.state.value)
                except Exception:
                    pass

    return {"status": "dismissed"}


@router.get("/api/notifications")
async def list_notifications():
    notifications = db.get_notifications()
    return {"notifications": notifications}


@router.post("/api/notifications/{notification_id}/dismiss")
async def dismiss_notification(notification_id: str):
    db.delete_notification(notification_id)
    return {"status": "dismissed"}


@router.post("/api/notify")
async def send_notification(body: dict):
    """Send a browser push notification (stores in DB + broadcasts via SSE)."""
    title = body.get("title", "Pernix")
    msg = body.get("body", "")
    urgency = body.get("urgency", "normal")
    session_id = body.get("session_id")

    nid = db.add_notification(session_id=session_id or "", title=title, body=msg, urgency=urgency)

    from sessions.manager import get_manager

    manager = get_manager()

    event_payload = {
        "type": "dialog.notification",
        "notification_id": nid,
        "title": title,
        "body": msg,
        "urgency": urgency,
        "source_session_id": session_id,
    }

    if session_id:
        manager.emit(session_id, event_payload)

    reached = manager.broadcast(event_payload)

    return {"status": "sent", "notification_id": nid, "session_id": session_id, "clients_reached": reached}


@router.get("/api/notifications/events")
async def notification_events(request: Request):
    """Global SSE stream for notifications — connects on page load, no session required."""
    from api.streaming import get_shutdown_event
    from sessions.manager import get_manager

    manager = get_manager()
    queue = manager.subscribe_global()
    shutdown = get_shutdown_event()

    async def stream():
        try:
            while not shutdown.is_set():
                try:
                    # asyncio.timeout() over wait_for — see api/streaming.py
                    # for the rationale (cleaner cancellation propagation,
                    # no orphaned inner Task on disconnect).
                    async with asyncio.timeout(30):
                        event = await queue.get()
                except asyncio.TimeoutError:
                    if shutdown.is_set():
                        return
                    yield ": heartbeat\n\n"
                    continue

                event_type = event.get("type", "message")
                if event_type == "_shutdown":
                    return
                data = {k: v for k, v in event.items() if not k.startswith("_")}
                yield f"event: {event_type}\ndata: {json.dumps(data)}\n\n"

        except (asyncio.CancelledError, GeneratorExit):
            pass
        finally:
            manager.unsubscribe_global(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
