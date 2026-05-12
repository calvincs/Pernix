"""Pernix — search_sessions + session_read tools.

FTS5 lookup over message history (current session by default; cross-session
via ``session_id="*"``) and direct by-id retrieval. These tools cover the
short-term-memory recovery path used when the context compiler emits a
[Context trim notice] for the current turn.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("pernix.tools.session_search")

_CROSS_SESSION_SENTINELS = {"*", "all", "ALL"}


def search_sessions(
    query: str,
    limit: int = 10,
    session_id: str | None = None,
    exclude_self: bool = True,
    _context: dict | None = None,
) -> str:
    """Search session message history by keyword. Returns ranked excerpts.

    Scope (controlled by ``session_id``):
      - ``None`` (default) → current session only.
      - ``"*"`` or ``"all"`` → all sessions (``exclude_self`` then controls
        whether the current session is included).
      - any explicit session id → restrict to that one session.
    """
    from db import models as db_models

    if not query or not query.strip():
        return "Error: query is required"

    limit = max(1, min(int(limit), 50))
    current_sid = (_context or {}).get("session_id") or ""

    include_sid = ""
    exclude_sid = ""

    if session_id is None:
        # Default: search the current session's own history.
        if not current_sid:
            return "Error: no current session context — pass session_id explicitly"
        include_sid = current_sid
        scope_label = "current session"
    elif session_id in _CROSS_SESSION_SENTINELS:
        # Cross-session — honor exclude_self for back-compat.
        if exclude_self and current_sid:
            exclude_sid = current_sid
        scope_label = "all other sessions" if exclude_self else "all sessions"
    else:
        # Accept either full id or unambiguous prefix — the agent often copies
        # back the short id it saw in earlier tool output.
        resolved = db_models.resolve_session_id(str(session_id))
        if not resolved:
            return (
                f"Error: session_id {session_id!r} does not match any session "
                "(prefix must be unambiguous; pass the full id if multiple match)."
            )
        include_sid = resolved
        scope_label = f"session {include_sid}"

    try:
        rows = db_models.search_messages_fts(
            query,
            limit=limit,
            exclude_session=exclude_sid,
            include_session=include_sid,
        )
    except Exception as e:
        logger.error("search_sessions failed: %s", e)
        return f"Error searching sessions: {e}"

    if not rows:
        return f"No matching messages found in {scope_label}."

    lines = [f"[scope: {scope_label}]"]
    for r in rows:
        content = (r["content"] or "").strip()
        title = (r["session_title"] or "untitled").replace('"', "'")
        stype = r.get("session_type") or "normal"
        created = (r.get("session_created_at") or "")[:10]
        updated = (r.get("session_updated_at") or "")[:10]
        date_str = f" {created}" if created else ""
        updated_str = f"→{updated}" if updated and updated != created else ""
        lines.append(
            f"[msg_id={r['msg_id']} session={r['session_id']} \"{title}\" {stype}{date_str}{updated_str}"
            f" role={r['role']} score={r['score']:.1f}] {content}"
        )
    return "\n".join(lines)


def session_read(msg_id: int, _context: dict | None = None) -> str:
    """Fetch the full content of a specific message by its msg_id."""
    from db import models as db_models

    try:
        mid = int(msg_id)
    except (TypeError, ValueError):
        return f"Error: msg_id must be an integer, got {msg_id!r}"

    try:
        row = db_models.get_message(mid)
    except Exception as e:
        logger.error("session_read(%s) failed: %s", mid, e)
        return f"Error reading message {mid}: {e}"

    if not row:
        return f"No message found with msg_id={mid}"

    sid = row.get("session_id") or ""
    role = row.get("role") or "?"
    created = (row.get("created_at") or "")[:19]
    tool_call_id = row.get("tool_call_id") or ""
    tool_calls = row.get("tool_calls") or ""
    content = row.get("content") or ""

    header_parts = [f"msg_id={mid}", f"session={sid[:8]}", f"role={role}"]
    if created:
        header_parts.append(f"created={created}")
    if tool_call_id and tool_call_id != "None":
        header_parts.append(f"tool_call_id={tool_call_id}")
    header = "[" + " ".join(header_parts) + "]"

    out = [header]
    if tool_calls:
        out.append(f"tool_calls: {tool_calls}")
    out.append(content)
    return "\n".join(out)


def register(reg) -> None:
    reg.register(
        name="search_sessions",
        func=search_sessions,
        description=(
            "Search verbatim message history (FTS5) — by default this session, "
            "or any other session by id, or all sessions via session_id='*'. "
            "Use this to RECOVER content trimmed from your view ([Context trim "
            "notice] points you here), to find exact tool outputs from earlier "
            "in the turn, or to reproduce past work across sessions. Each "
            "result includes a msg_id you can pass to session_read for full "
            "content. NOTE: for curated long-term memory (insights, decisions, "
            "summaries), use `recall` instead — that searches a different "
            "(memory) index, not raw transcript."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search keywords (FTS5 OR over tokens >=2 chars)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 10, clamped 1-50)",
                },
                "session_id": {
                    "type": "string",
                    "description": (
                        "Scope: omit (default) for the current session; '*' or "
                        "'all' for cross-session search; or a specific session id."
                    ),
                },
                "exclude_self": {
                    "type": "boolean",
                    "description": (
                        "Only consulted when session_id='*'/'all'. If true, the "
                        "current session is excluded from cross-session results."
                    ),
                },
            },
            "required": ["query"],
        },
        category="memory",
        tags=[
            "search",
            "sessions",
            "history",
            "past",
            "previous",
            "transcript",
            "conversation",
            "trim",
            "lost",
            "dropped",
            "recover",
            "msg_id",
            "this session",
            "current session",
            "before",
            "yesterday",
        ],
        timeout=30,
        parallel_safe=True,
    )

    reg.register(
        name="session_read",
        func=session_read,
        description=(
            "Fetch the FULL content of a specific message by its msg_id. Use "
            "this when [Context trim notice] names a specific msg_id you need "
            "to recover, or when search_sessions returns a snippet you want "
            "in full. Returns role, created_at, tool_call linkage, and the "
            "complete message body."
        ),
        parameters={
            "type": "object",
            "properties": {
                "msg_id": {
                    "type": "integer",
                    "description": "Numeric message id from the messages table",
                },
            },
            "required": ["msg_id"],
        },
        category="memory",
        tags=[
            "read",
            "message",
            "msg_id",
            "recover",
            "trim",
            "transcript",
            "fetch",
        ],
        timeout=30,
        parallel_safe=True,
    )
