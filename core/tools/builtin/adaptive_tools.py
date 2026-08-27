"""Pernix — adaptive_note: the agent's own authorship valve (v3.1).

The adaptive layer's SOURCES always declared `agent`, but no path ever
minted one — every entry came from a background loop, and the moment of
insight ("next time do X") was lost to the refine lottery. This tool lets
the live agent capture it immediately, under the full machine-edit
governance stack: the content lint (an observation is refused with the
reason), the normal batch/proposal pipeline, the tripwire's post-batch
probe, journaled apply with one-click rollback, and a hard 2-mints-per-day
cap. Low-risk kinds only — an agent never writes policy about itself.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from config import settings
from db import models as db

logger = logging.getLogger("pernix.adaptive")

_DAILY_CAP = 2
_ALLOWED_KINDS = ("prompt_note", "routing_hint")


def _mints_today_key() -> str:
    return f"adaptive_agent_notes:{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"


def adaptive_note(kind: str, title: str, content: str, _context: dict | None = None) -> str:
    if not settings.adaptive_agent_notes_enabled:
        return "Error: adaptive_agent_notes_enabled is off."
    if kind not in _ALLOWED_KINDS:
        return f"Error: kind must be one of {', '.join(_ALLOWED_KINDS)} — agents never mint policy."
    key = _mints_today_key()
    try:
        used = int(db.get_snooze_state(key) or "0")
    except (TypeError, ValueError):
        used = 0
    if used >= _DAILY_CAP:
        return (
            f"Error: the {_DAILY_CAP}-notes-per-day cap is reached. If this insight is durable "
            "it will survive until tomorrow — or belongs in a memory lesson instead."
        )

    from core.adaptive.contract import queue_producer_edits

    result = queue_producer_edits(
        [
            {
                "action": "create",
                "kind": kind,
                "scope": "global",
                "title": title,
                "content": content,
                "evidence": ["agent:adaptive_note"],
            }
        ],
        "agent",
        session_id=(_context or {}).get("session_id", ""),
        rationale="agent-authored note (adaptive_note tool)",
    )
    if result["rejected"]:
        return f"Rejected: {result['rejected'][0]['reason']}"
    db.set_snooze_state(key, str(used + 1))
    if result["batch_id"]:
        return (
            f"Queued as batch {result['batch_id']} — applies at the next idle window, "
            "tripwire-watched, rollbackable from the Adaptive tab."
        )
    if result["proposal_id"]:
        return f"Queued as proposal #{result['proposal_id']} (veto window applies)."
    return "Nothing was queued (adaptive layer may be disabled)."


def register(reg) -> None:
    if not (settings.adaptive_enabled and settings.adaptive_agent_notes_enabled):
        return
    reg.register(
        name="adaptive_note",
        func=adaptive_note,
        description=(
            "Capture a durable, cross-session operational insight as an adaptive "
            "entry the moment you learn it: a routing_hint (tool/skill selection "
            "guidance for the planner) or a prompt_note (a short behavioral note "
            "for future turns). Content must be an INSTRUCTION — what to do and "
            "when — not an observation; narrative findings are refused. Max 2/day. "
            "One-off fixes belong in memory lessons, not here."
        ),
        parameters={
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": list(_ALLOWED_KINDS)},
                "title": {"type": "string", "description": "Short stable title (becomes the entry id)"},
                "content": {
                    "type": "string",
                    "description": "The instruction, e.g. 'When X: do Y' (prompt_note <= 400 chars)",
                },
            },
            "required": ["kind", "title", "content"],
        },
        category="evaluation",
        tags=["adaptive", "note", "hint", "learn", "policy", "self-improvement"],
        timeout=15,
        parallel_safe=False,
        safety_level="caution",  # writes global prompt state (governed + rollbackable)
        denied_session_types={"canary", "worker"},
    )
