"""Pernix — Async dialog tool: ask_user."""

from __future__ import annotations

import logging

from db import models as db

logger = logging.getLogger("pernix.tools.dialog")


def ask_user(
    question: str = "",
    context: str = "",
    urgency: str = "normal",
    question_type: str = "question",
    # Common aliases the model may use instead of 'question'
    query: str = "",
    message: str = "",
    text: str = "",
    _context: dict | None = None,
) -> str:
    """Post a question for the user (non-blocking). The agent will be notified when answered."""
    # Accept common parameter aliases
    question = question or query or message or text
    if not question:
        return "Error: No question provided. Use the 'question' parameter."
    session_id = (_context or {}).get("session_id", "")
    if not session_id:
        return "Error: No session context"

    # Get session title for display
    session = db.get_session(session_id)
    session_title = session["title"] if session else ""
    session_type = session["session_type"] if session else "normal"

    qid = db.add_question(
        session_id=session_id,
        question=question,
        session_title=session_title,
        session_type=session_type,
        context=context,
        urgency=urgency,
        question_type=question_type,
    )

    # Emit session SSE event (triggers frontend question panel instantly)
    from sessions.manager import get_manager

    manager = get_manager()
    event_payload = {
        "type": "dialog.question",
        "question_id": qid,
        "question": question,
        "context": context,
        "urgency": urgency,
        "session_title": session_title,
    }
    manager.emit(session_id, event_payload)

    # Broadcast a notification event to ALL connected browsers/devices
    # (the dialog.question above only reaches browsers viewing this session)
    manager.broadcast(
        {
            "type": "dialog.notification",
            "title": f"Question from: {session_title}" if session_title else "Agent Question",
            "body": question[:200],
            "urgency": urgency,
            "source_session_id": session_id,
        }
    )

    # Emit on global bus (for headless/webhook consumers)
    from core.events import get_event_bus

    bus = get_event_bus()
    bus.emit({**event_payload, "session_id": session_id})

    # Transition the session into AWAITING_USER. Agent loop sees the new
    # state at its post-tool-round checkpoint (core/agent.py) and exits the
    # turn cleanly; post-hooks then run normally with termination_reason=
    # complete, after which the session sits in AWAITING_USER until the
    # /answer or /dismiss endpoint transitions it out.
    session_obj = manager.get(session_id)
    if session_obj is not None:
        from db import models as _db_models
        from sessions import state_v2 as sv2

        try:
            sv2.transition(session_obj, sv2.SessionStateV2.AWAITING_USER, "ask-user")
            _db_models.set_session_state(session_id, session_obj.state.value)
        except Exception:
            # Non-fatal: fall back to the legacy-mirror state already set by
            # the bridge, and let the agent loop observe waiting_for_input
            # via the property.
            pass

    return f"Question posted (id={qid}). You will be notified when the user responds."


def notify_user(
    title: str = "",
    body: str = "",
    urgency: str = "normal",
    _context: dict | None = None,
) -> str:
    """Send a browser/OS push notification to the user (non-blocking)."""
    if not title:
        return "Error: 'title' is required."
    session_id = (_context or {}).get("session_id", "")

    # Persist in DB so the bell panel can display it
    nid = db.add_notification(session_id=session_id, title=title, body=body, urgency=urgency)

    event_payload = {
        "type": "dialog.notification",
        "notification_id": nid,
        "title": title,
        "body": body,
        "urgency": urgency,
        "source_session_id": session_id,
    }

    # Broadcast to ALL sessions with active SSE subscribers so every
    # connected browser/device receives the notification.
    from sessions.manager import get_manager

    manager = get_manager()
    reached = manager.broadcast(event_payload)

    from core.events import get_event_bus

    get_event_bus().emit({**event_payload, "session_id": session_id})

    return f"Notification broadcast to {reached} connected client(s)."


def register(reg) -> None:
    reg.register(
        name="ask_user",
        func=ask_user,
        description="Post a question for the user. Non-blocking — the agent continues or pauses until the user answers via the API.",
        parameters={
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "The question to ask"},
                "context": {"type": "string", "description": "Additional context for the user"},
                "urgency": {
                    "type": "string",
                    "enum": ["normal", "high"],
                    "description": "Urgency level",
                },
                "question_type": {
                    "type": "string",
                    "enum": ["question", "statement"],
                    "description": "Whether this is a question or informational statement",
                },
            },
            "required": ["question"],
        },
        category="dialog",
        tags=["ask", "question", "user", "input", "dialog", "clarify"],
        timeout=15,
        parallel_safe=False,
    )
    reg.register(
        name="notify_user",
        func=notify_user,
        description="Send a browser push notification to get the user's attention. When clicked, brings them to this session. Use for important status updates that don't require a response.",
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Notification title (keep short, ~50 chars)"},
                "body": {"type": "string", "description": "Notification body text"},
                "urgency": {
                    "type": "string",
                    "enum": ["normal", "high"],
                    "description": "Urgency level — high adds extra vibration on mobile",
                },
            },
            "required": ["title"],
        },
        category="dialog",
        tags=["notify", "notification", "alert", "push", "attention", "ping"],
        timeout=10,
        parallel_safe=False,
    )
