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

    # Unattended sessions (cron, and workers spawned from cron) have no user
    # present to answer — parking in AWAITING_USER would stall the scheduled
    # job indefinitely (the reaper never reaps AWAITING_USER by design).
    # Mirrors the dangerous-tool gate in core/tools/executor.py, which skips
    # the ask_user → approve flow for the same reason.
    from core.tools.executor import _is_unattended_session

    if _is_unattended_session(session_id):
        return (
            "Error: This session runs unattended (cron-initiated) — no user is "
            "present to answer, and waiting would stall the job indefinitely. "
            "Make a reasonable decision autonomously and proceed with the task. "
            "Use notify_user to tell the user what you decided and why."
        )

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

    # Statements are informational — deliver them to the question panel but do
    # NOT park the session in AWAITING_USER. Pausing on announcements ("I'll
    # retry the cast now…") suspends real work until the user dismisses the
    # bubble: session 0dbee64fcd43 stalled repeatedly on exactly this.
    if question_type == "statement":
        return (
            f"Statement delivered (id={qid}). The session is NOT paused — "
            f"continue working now. If the user replies, their message will "
            f"arrive as a new prompt."
        )

    # Transition the session into AWAITING_USER. Agent loop sees the new
    # state at its post-tool-round checkpoint (core/agent.py) and exits the
    # turn cleanly; post-hooks then run normally with termination_reason=
    # complete, after which the session sits in AWAITING_USER until the
    # /answer or /dismiss endpoint transitions it out.
    session_obj = manager.get(session_id)
    if session_obj is not None:
        from core.events import call_on_loop
        from db import models as _db_models
        from sessions import state_v2 as sv2

        try:
            # ask_user runs on a tool thread; transition() is loop-affine
            # (multi-step read-modify-write serialized by the event loop).
            call_on_loop(
                sv2.transition,
                session_obj,
                sv2.SessionStateV2.AWAITING_USER,
                "ask-user",
                loop=(_context or {}).get("_loop"),
            )
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


_APPROVALS_PATH = None  # resolved lazily from settings


def _approvals_path():
    """Return the path to the persistent tool approvals file."""
    global _APPROVALS_PATH
    if _APPROVALS_PATH is None:
        from pathlib import Path as _Path

        _APPROVALS_PATH = _Path("data") / "tool_approvals.json"
    return _APPROVALS_PATH


def _load_approvals() -> dict:
    """Load persisted approved scopes: {tool_name: [scope, ...]}."""
    import json as _json

    p = _approvals_path()
    if not p.exists():
        return {}
    try:
        return _json.loads(p.read_text()) or {}
    except (ValueError, OSError):
        return {}


def _save_approvals(store: dict) -> None:
    """Persist the approved scopes store to disk."""
    import json as _json

    p = _approvals_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_json.dumps(store, indent=2))
    except OSError:
        pass  # best-effort; in-session approval still works


def _scope_key(scope: str) -> str:
    """Normalise a scope string for storage/lookup."""
    return scope.strip().lower()


def approve_dangerous_tool(
    tool_name: str = "",
    scope: str = "",
    persistent: bool = False,
    _context: dict | None = None,
) -> str:
    """Register user approval for a specific dangerous tool action.

    REQUIRED call sequence (first time):
      1. ask_user() — describe the EXACT action (command, URL, file path).
         Show the user precisely what will be executed, not just the tool name.
      2. approve_dangerous_tool(tool_name, scope) — records approval.
      3. Call the dangerous tool.

    Previously approved scopes are remembered across sessions: if the user
    approved "run ps aux to list processes" before, calling this tool with the
    same scope skips the ask_user step automatically.

    Args:
        tool_name:  Exact tool name (e.g. 'bash', 'browse_web').
        scope:      Description of the SPECIFIC action (e.g. 'run ps aux to list
                    processes', 'fetch https://example.com'). Must match what was
                    told to the user. Previously approved scopes are recognised
                    automatically — no need to call ask_user again for those.
        persistent: False (default) — approval consumed after ONE use so a
                    different action on the same tool needs its own approval.
                    True — stays for the session; use only for repetitive low-risk
                    actions (e.g. 'browse several pages while researching').
    """
    if not tool_name:
        return "Error: tool_name is required."
    if not scope:
        return (
            "Error: scope is required. Describe the SPECIFIC action being approved "
            "(e.g. 'run ps aux to list processes', not just 'bash'). "
            "Previously approved scopes are remembered — use the same description."
        )

    session_id = (_context or {}).get("session_id", "")
    if not session_id:
        return "Error: No session context."

    norm_scope = _scope_key(scope)

    # Check the persistent approval store first. If this exact scope was
    # approved in a prior session, skip the ask_user validation — the user
    # already said yes to this action and doesn't want to be asked again.
    stored = _load_approvals()
    previously_approved = norm_scope in [_scope_key(s) for s in stored.get(tool_name, [])]

    if not previously_approved:
        # New action — validate that ask_user was called recently so the
        # user was shown what the agent intends to do.
        import json as _json

        from db import models as _db

        messages = _db.get_messages(session_id, last=40)
        ask_user_idx = -1
        answer_idx = -1
        recent = messages[-40:]
        for i, m in enumerate(recent):
            role = m.get("role")
            if role == "assistant":
                found = False
                # Primary: check tool_calls column — Pernix stores tool-use blocks
                # here as [{"id": ..., "name": ..., "arguments": ...}, ...]
                tool_calls_raw = m.get("tool_calls")
                if tool_calls_raw:
                    try:
                        tcs = _json.loads(tool_calls_raw)
                        if isinstance(tcs, list):
                            found = any(isinstance(tc, dict) and tc.get("name") == "ask_user" for tc in tcs)
                    except (ValueError, TypeError):
                        pass
                # Fallback: content field (legacy or alternative message formats)
                if not found:
                    try:
                        parts = _json.loads(m.get("content") or "[]")
                        if isinstance(parts, list):
                            found = any(isinstance(p, dict) and p.get("name") == "ask_user" for p in parts)
                    except (ValueError, TypeError):
                        pass
                if found:
                    ask_user_idx = i
            elif role == "user":
                content = m.get("content") or ""
                if content.startswith("[User answered your question]"):
                    answer_idx = i

        if ask_user_idx < 0:
            return (
                f"Error: Cannot approve '{tool_name}' (scope: {scope!r}) without "
                f"first asking the user. Call ask_user() with the exact action, "
                f"then call this tool after the user responds. "
                f"(Previously approved scopes skip this step automatically.)"
            )
        if answer_idx < ask_user_idx:
            # ask_user was called but the user has not (yet) answered it —
            # an unanswered or dismissed question must not unlock the tool.
            return (
                f"Error: Cannot approve '{tool_name}' (scope: {scope!r}) — the user "
                f"has not answered your question yet. Wait for their reply; approval "
                f"requires an actual answer, not just having asked."
            )

        # Persist this new approval so future sessions don't need to re-ask.
        if tool_name not in stored:
            stored[tool_name] = []
        if scope not in stored[tool_name]:
            stored[tool_name].append(scope)
        _save_approvals(stored)

    from sessions.manager import get_manager

    session_obj = get_manager().get(session_id)
    if session_obj is None:
        return "Error: Session not found."

    session_obj._approved_dangerous_tools[tool_name] = {
        "scope": scope,
        "persistent": persistent,
    }

    recalled = " (recalled from prior session — no re-confirmation needed)" if previously_approved else ""
    if persistent:
        return (
            f"Tool '{tool_name}' approved persistently for this session"
            f"{recalled} (scope: {scope!r}). Approval will not be consumed on use."
        )
    return (
        f"Tool '{tool_name}' approved for one use"
        f"{recalled} (scope: {scope!r}). Approval is consumed after the next call; "
        f"a different action on the same tool will need its own approval."
    )


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
                    "description": (
                        "'question' pauses the session until the user answers. "
                        "'statement' is informational only — it is shown to the "
                        "user but the session does NOT pause; keep working."
                    ),
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
    # approve_dangerous_tool exists to service the executor's dangerous-tool
    # gate. Under --dangerous the gate never fires, so registering it would
    # only teach the model a permission ritual with no enforcement behind it
    # (session 0dbee64fcd43: 13 ask_user + approve rounds for caution-level
    # bash calls that were never gated). The flag is process-lifetime, so
    # skipping registration at startup suppresses the ritual for good.
    from config import settings

    if settings.auto_approve_dangerous:
        return

    reg.register(
        name="approve_dangerous_tool",
        func=approve_dangerous_tool,
        description=(
            "Register user approval for a specific dangerous tool action. "
            "Must be called AFTER ask_user() where you described the EXACT action "
            "(command, URL, file path — not just the tool name). "
            "Approval is one-time-use by default: approving 'bash' for 'ps aux' does NOT "
            "cover a later 'mv' call — each distinct action requires its own ask_user + approval. "
            "Use persistent=True only for genuinely repetitive low-risk actions."
        ),
        parameters={
            "type": "object",
            "properties": {
                "tool_name": {
                    "type": "string",
                    "description": "Exact tool name to approve (e.g. 'bash', 'browse_web')",
                },
                "scope": {
                    "type": "string",
                    "description": (
                        "Description of the SPECIFIC action being approved — must match "
                        "what you told the user in ask_user "
                        "(e.g. 'run ps aux to list processes', 'fetch https://example.com')"
                    ),
                },
                "persistent": {
                    "type": "boolean",
                    "description": (
                        "False (default): approval consumed after one use. "
                        "True: approval persists for the session. "
                        "Only use True for repetitive low-risk actions."
                    ),
                },
            },
            "required": ["tool_name", "scope"],
        },
        safety_level="safe",
        category="dialog",
        tags=["approve", "dangerous", "confirm", "permission", "unlock"],
        timeout=5,
        parallel_safe=False,
    )
