"""Pernix — search_sessions tool. FTS5 lookup over past session messages."""

from __future__ import annotations

import logging

logger = logging.getLogger("pernix.tools.session_search")


def search_sessions(
    query: str,
    limit: int = 10,
    exclude_self: bool = True,
    _context: dict | None = None,
) -> str:
    """Search past chat sessions by keyword. Returns ranked excerpts."""
    from db import models as db_models

    if not query or not query.strip():
        return "Error: query is required"

    limit = max(1, min(int(limit), 50))

    exclude_sid = ""
    if exclude_self and _context and _context.get("session_id"):
        exclude_sid = _context["session_id"]

    try:
        rows = db_models.search_messages_fts(query, limit=limit, exclude_session=exclude_sid)
    except Exception as e:
        logger.error("search_sessions failed: %s", e)
        return f"Error searching sessions: {e}"

    if not rows:
        return "No matching messages found."

    lines = []
    for r in rows:
        content = (r["content"] or "").strip()
        title = (r["session_title"] or "untitled").replace('"', "'")
        stype = r.get("session_type") or "normal"
        created = (r.get("session_created_at") or "")[:10]
        updated = (r.get("session_updated_at") or "")[:10]
        date_str = f" {created}" if created else ""
        updated_str = f"→{updated}" if updated and updated != created else ""
        lines.append(
            f"[{r['session_id'][:8]} \"{title}\" {stype}{date_str}{updated_str}"
            f" role={r['role']} score={r['score']:.1f}] {content}"
        )
    return "\n".join(lines)


def register(reg) -> None:
    reg.register(
        name="search_sessions",
        func=search_sessions,
        description=(
            "Search past chat sessions (FTS5) by keyword. Returns ranked "
            "excerpts of user/assistant/tool messages from prior sessions. "
            "Use when stuck or when reproducing past work — e.g. 'how did "
            "we fix the OOM error', 'where did we set up the cron', 'what "
            "did I ask about ffmpeg yesterday'. Excludes the current session "
            "by default."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search keywords (FTS5 OR over words >2 chars)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 10, clamped 1-50)",
                },
                "exclude_self": {
                    "type": "boolean",
                    "description": "Exclude the current session (default true)",
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
            "recall",
            "lookup",
            "find",
            "before",
            "yesterday",
            "transcript",
            "conversation",
        ],
        timeout=30,
        parallel_safe=True,
    )
