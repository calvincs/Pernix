"""Pernix — Session tools extension: cross-session introspection."""

from __future__ import annotations

from db import models as db


def list_recent_sessions(limit: int = 10, _context: dict | None = None) -> str:
    """List recent sessions with titles and types."""
    sessions = db.list_sessions(limit=limit)
    if not sessions:
        return "No sessions found."
    lines = []
    for s in sessions:
        lines.append(f"- {s['id'][:8]} \"{s['title']}\" ({s['session_type']}) — {s.get('updated_at', '')[:16]}")
    return "\n".join(lines)


def read_session_summary(session_id: str, _context: dict | None = None) -> str:
    """Read a summary of a session's conversation."""
    session = db.get_session(session_id)
    if not session:
        return f"Session {session_id} not found."

    messages = db.get_messages(session_id)

    lines = [
        f"Session: {session['title']} (type={session['session_type']})",
        f"Messages: {len(messages)}",
    ]

    # Last few messages
    recent = [m for m in messages[-5:] if m["role"] in ("user", "assistant")]
    if recent:
        lines.append("\nRecent messages:")
        for m in recent:
            lines.append(f"  [{m['role']}] {(m.get('content') or '')[:200]}")

    return "\n".join(lines)


def register(reg) -> None:
    common = {"category": "session", "source": "extension"}
    tags = ["session", "history", "recent", "previous", "past"]

    reg.register(
        name="list_recent_sessions",
        func=list_recent_sessions,
        description="List recent sessions with titles, types, and timestamps.",
        parameters={
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "Max results (default 10)"}},
        },
        tags=tags + ["list", "all"],
        timeout=15,
        parallel_safe=True,
        **common,
    )
    reg.register(
        name="read_session_summary",
        func=read_session_summary,
        description="Read a summary of another session's conversation.",
        parameters={
            "type": "object",
            "properties": {"session_id": {"type": "string", "description": "Session ID to read"}},
            "required": ["session_id"],
        },
        tags=tags + ["read", "summary", "detail"],
        timeout=30,
        parallel_safe=True,
        **common,
    )
