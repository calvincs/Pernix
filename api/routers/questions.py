"""Pernix — Async dialog question and notification endpoints."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import StreamingResponse

from core.events import queue_was_dropped
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
    try:
        admission = await manager.prompt(
            session_id, formatted, origin="answer", question_id=question_id, question_answer=answer
        )
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    if admission.rejected:
        raise HTTPException(409, detail=admission.reason)

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

    if not question:
        return {"status": "dismissed"}
    from sessions import state_v2 as sv2
    from sessions.manager import get_manager

    manager = get_manager()
    session = manager.get_or_create(question["session_id"])
    if sv2._current_state(session) is sv2.S.AWAITING_USER:
        formatted = "[User dismissed your question without answering]\n" + f"Q: {question['question']}"
        try:
            admission = await manager.prompt(
                session.session_id,
                formatted,
                origin="dismissal",
                question_id=question_id,
                question_answer="[dismissed]",
            )
        except ValueError as exc:
            raise HTTPException(409, detail=str(exc)) from exc
        if admission.rejected:
            raise HTTPException(409, detail=admission.reason)
    else:
        from db.database import connect_sessions

        with connect_sessions() as conn:
            conn.execute("DELETE FROM questions WHERE id = ? AND answered_at IS NULL", (question_id,))
    manager.emit(session.session_id, {"type": "dialog.dismissed", "question_id": question_id})
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
                    if queue_was_dropped(queue):
                        return
                    yield ": heartbeat\n\n"
                    continue

                event_type = event.get("type", "message")
                if event_type == "_shutdown":
                    return
                data = {k: v for k, v in event.items() if not k.startswith("_")}
                yield f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
                if queue_was_dropped(queue):
                    return

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
