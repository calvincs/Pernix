"""Pernix — Session tools extension: cross-session introspection."""

from __future__ import annotations

from db import models as db


def list_recent_sessions(limit: int = 10, _context: dict | None = None) -> str:
    """List recent sessions with titles, types, message counts, and timestamps."""
    sessions = db.list_sessions_enriched(limit=limit)
    if not sessions:
        return "No sessions found."
    lines = []
    for s in sessions:
        msg_count = s.get("message_count", 0)
        tokens = s.get("total_tokens", 0)
        tokens_str = f"  {tokens:,} tokens" if tokens else ""
        parent = f"  parent={s['parent_session_id'][:8]}" if s.get("parent_session_id") else ""
        first = s.get("first_message") or ""
        first_str = f'  "{first[:80]}"' if first else ""
        lines.append(
            f"- {s['id'][:8]} [{s['session_type']}] \"{s['title']}\""
            f"  {(s.get('created_at') or '')[:16]} → {(s.get('updated_at') or '')[:16]}"
            f"  {msg_count} msgs{tokens_str}{parent}{first_str}"
        )
    return "\n".join(lines)


def read_session_summary(session_id: str, recent: int = 5, _context: dict | None = None) -> str:
    """Read a rich summary of a session: metadata, token usage, and recent conversation."""
    session = db.get_session(session_id)
    if not session:
        # Try prefix match — caller may pass first 8 chars
        all_sessions = db.list_sessions(limit=200)
        matches = [s for s in all_sessions if s["id"].startswith(session_id)]
        if len(matches) == 1:
            session = matches[0]
            session_id = session["id"]
        elif len(matches) > 1:
            ids = ", ".join(s["id"][:8] for s in matches)
            return f"Ambiguous prefix '{session_id}' matches: {ids}"
        else:
            return f"Session '{session_id}' not found."

    usage = db.get_session_usage(session_id)
    messages = db.get_messages(session_id)

    # --- Metadata block ---
    created = (session.get("created_at") or "")[:19]
    updated = (session.get("updated_at") or "")[:19]
    parent = session.get("parent_session_id") or ""
    subtitle = session.get("subtitle") or ""
    state = session.get("state_v2") or session.get("state") or "unknown"

    lines = [
        f"Session: {session['id'][:8]}  \"{session['title']}\"",
    ]
    if subtitle:
        lines.append(f"Subtitle: {subtitle}")
    lines += [
        f"Type: {session['session_type']}  |  State: {state}",
        f"Started: {created}  |  Last active: {updated}",
    ]
    if parent:
        lines.append(f"Parent session: {parent[:8]}")

    # Token usage
    total_tok = usage.get("total", 0)
    cost = usage.get("cost", 0.0)
    calls = usage.get("calls", 0)
    if total_tok:
        lines.append(
            f"Tokens: {total_tok:,} total  ({usage.get('prompt',0):,} prompt / "
            f"{usage.get('completion',0):,} completion / "
            f"{usage.get('cache_read',0):,} cache_read)  "
            f"Est. cost: ${cost:.4f}  LLM calls: {calls}"
        )

    # Message counts
    user_msgs = [m for m in messages if m["role"] == "user"]
    asst_msgs = [m for m in messages if m["role"] == "assistant"]
    lines.append(f"Messages: {len(user_msgs)} user / {len(asst_msgs)} assistant / {len(messages)} total")

    # First user message — establishes what the session was about
    if user_msgs:
        first_content = (user_msgs[0].get("content") or "").strip()[:300]
        lines += ["", "First message:", f"  [user] {first_content}"]

    # Recent conversation turns
    recent_msgs = [m for m in messages[-recent * 4 :] if m["role"] in ("user", "assistant")][-recent:]
    if recent_msgs:
        lines.append(f"\nRecent {len(recent_msgs)} messages:")
        for m in recent_msgs:
            content = (m.get("content") or "").strip()[:400]
            lines.append(f"  [{m['role']}] {content}")

    return "\n".join(lines)


def register(reg) -> None:
    common = {"category": "session", "source": "extension"}
    tags = ["session", "history", "recent", "previous", "past"]

    reg.register(
        name="list_recent_sessions",
        func=list_recent_sessions,
        description=(
            "List recent sessions with titles, types, timestamps, message counts, "
            "token usage, and the opening message of each session."
        ),
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
        description=(
            "Read a rich summary of a specific session: metadata (type, state, "
            "start/end times, parent), token usage, message counts, the opening "
            "message, and the most recent conversation turns. "
            "Accepts full session ID or an 8-char prefix."
        ),
        parameters={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session ID or 8-char prefix to look up",
                },
                "recent": {
                    "type": "integer",
                    "description": "Number of recent user/assistant messages to include (default 5)",
                },
            },
            "required": ["session_id"],
        },
        tags=tags + ["read", "summary", "detail", "metadata", "tokens", "cost"],
        timeout=30,
        parallel_safe=True,
        **common,
    )
