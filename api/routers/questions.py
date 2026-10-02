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


def _drop_question_row(question_id: str) -> None:
    """Close an open question with no answer behind it.

    Dismiss is the user saying "never mind". It has no failure the user can
    act on, so every path through the endpoint ends with the row gone — the
    alternative is a question nobody will ever answer sitting in the tray and
    a session parked in AWAITING_USER behind it.
    """
    from db.database import connect_sessions

    with connect_sessions() as conn:
        conn.execute("DELETE FROM questions WHERE id = ? AND answered_at IS NULL", (question_id,))


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
    try:
        session = manager.get_or_create(question["session_id"])
    except ValueError:
        # The session row is gone (deleted while the question was open). The
        # old code returned dismissed here; get_or_create's ValueError turned
        # that into an unhandled 500 over a row we can simply drop.
        _drop_question_row(question_id)
        return {"status": "dismissed"}

    if sv2._current_state(session) is not sv2.S.AWAITING_USER:
        _drop_question_row(question_id)
        manager.emit(session.session_id, {"type": "dialog.dismissed", "question_id": question_id})
        return {"status": "dismissed"}

    formatted = "[User dismissed your question without answering]\n" + f"Q: {question['question']}"
    refusal: str | None = None
    try:
        admission = await manager.prompt(
            session.session_id,
            formatted,
            origin="dismissal",
            question_id=question_id,
            question_answer="[dismissed]",
        )
    except ValueError as exc:
        refusal = str(exc)
    else:
        if admission.rejected:
            refusal = admission.reason

    if refusal is not None:
        # queue_full / shutting_down / cancelling: the notice could not be
        # delivered, but the dismissal still happened. Returning 409 left the
        # question row open AND the session in AWAITING_USER, which nothing
        # else clears — the reaper only unsticks a session whose question row
        # is already gone. Drop the row, take the declared fallback edge, and
        # report the refusal as information rather than as a failure.
        _drop_question_row(question_id)
        if sv2._current_state(session) is sv2.S.AWAITING_USER:
            sv2.transition(session, sv2.S.IDLE_READY, "question-dismissed")
        manager.emit(session.session_id, {"type": "dialog.dismissed", "question_id": question_id})
        return {"status": "dismissed", "notice_delivered": False, "detail": refusal}

    manager.emit(session.session_id, {"type": "dialog.dismissed", "question_id": question_id})
    return {"status": "dismissed"}


@router.get("/api/notifications")
async def list_notifications(view: str = "bell", area: str = "", before: str = "", limit: int = 200):
    """view=bell (default, what needs a look) or view=log (the activity log:
    every row in every state, newest first, paged with `before`)."""
    if view not in ("bell", "log"):
        raise HTTPException(400, detail="view must be 'bell' or 'log'")
    limit = max(1, min(int(limit), 500))
    notifications = await asyncio.to_thread(db.list_notifications, view, area or None, before or None, limit)
    return {"notifications": notifications}


@router.get("/api/notifications/counts")
async def notification_counts():
    """Cheap badge numbers: needs_you (open interrupt rows), bell (open quiet
    rows), unread (never looked at, any tier)."""
    return await asyncio.to_thread(db.notification_counts)


@router.post("/api/notifications/dismiss-all")
async def dismiss_all_notifications():
    n = await asyncio.to_thread(db.dismiss_all_notifications)
    return {"status": "dismissed", "count": n}


@router.post("/api/notifications/read-all")
async def read_all_notifications():
    n = await asyncio.to_thread(db.mark_notifications_read, None)
    return {"status": "read", "count": n}


@router.post("/api/notifications/{notification_id}/dismiss")
async def dismiss_notification(notification_id: str):
    # Soft: the row leaves the bell and stays in the activity log.
    await asyncio.to_thread(db.dismiss_notification, notification_id)
    return {"status": "dismissed"}


@router.post("/api/notifications/{notification_id}/read")
async def read_notification(notification_id: str):
    await asyncio.to_thread(db.mark_notifications_read, [notification_id])
    return {"status": "read"}


@router.post("/api/notify")
async def send_notification(body: dict):
    """Send a notification from an external caller. Routed through the tier
    policy: high/urgent urgency is an interrupt (badge + push), anything else is
    a quiet bell item."""
    from core import notices

    title = body.get("title", "Pernix")
    msg = body.get("body", "")
    urgency = body.get("urgency", "normal")
    session_id = body.get("session_id")

    category = "external.message_urgent" if urgency in ("high", "urgent") else "external.message"
    nid = await asyncio.to_thread(notices.notify, category, title, msg, session_id=session_id or "")
    if session_id and nid:
        from sessions.manager import get_manager

        get_manager().emit(
            session_id,
            {
                "type": "dialog.notification",
                "notification_id": nid,
                "title": title,
                "body": msg,
                "urgency": urgency,
                "source_session_id": session_id,
                "tier": "interrupt" if category.endswith("_urgent") else "bell",
            },
        )
    return {"status": "sent", "notification_id": nid, "session_id": session_id}


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
