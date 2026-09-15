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


# ---------------------------------------------------------------------------
# Proposal review from inside a session (2026-09-15)
#
# The Learning tab's "Chat" button opens a normal session about one pending
# proposal. The agent in that session needs to READ the row (not a pasted
# summary — it should be able to answer "what did that session actually do")
# and, when the person says so, DECIDE it. Same engine as the Approve button:
# approve applies through apply_batch with a rollbackable batch id, reject
# resolves the row. Nothing here is reachable from canary/worker/cron
# sessions — a self-test must never approve a rule about itself.
# ---------------------------------------------------------------------------

_PROPOSAL_ACTIONS = ("list", "show")
_DECISIONS = ("approve", "reject")


def _format_proposal(prop: dict, full: bool) -> str:
    from core.adaptive.explain import explain_as_text

    head = f"#{prop['id']} [{prop.get('status')}] from {prop.get('producer')} · created {prop.get('created_at')}"
    lines = [head, explain_as_text(prop)]
    if full:
        lines.append(f"RATIONALE (as written): {prop.get('rationale') or ''}")
        lines.append(f"EVIDENCE (as written): {prop.get('evidence_json') or '[]'}")
        lines.append(f"PAYLOAD (as written): {prop.get('payload_json') or '[]'}")
    return "\n".join(lines)


def adaptive_proposals(action: str = "list", proposal_id: int | None = None, _context: dict | None = None) -> str:
    action = (action or "list").strip().lower()
    if action not in _PROPOSAL_ACTIONS:
        return f"Error: action must be one of {', '.join(_PROPOSAL_ACTIONS)}."
    if action == "show":
        if proposal_id is None:
            return "Error: proposal_id is required for show."
        prop = db.adaptive_get_proposal(int(proposal_id))
        if prop is None:
            return f"Error: no proposal #{proposal_id}."
        return _format_proposal(prop, full=True)
    rows = db.adaptive_list_proposals(status="pending", limit=50)
    if not rows:
        return "No proposals are waiting for review."
    out = [f"{len(rows)} proposal(s) awaiting review (oldest last):"]
    for r in rows:
        out.append("")
        out.append(_format_proposal(r, full=False))
    return "\n".join(out)


def adaptive_proposal_decide(proposal_id: int, decision: str, _context: dict | None = None) -> str:
    decision = (decision or "").strip().lower()
    if decision not in _DECISIONS:
        return f"Error: decision must be one of {', '.join(_DECISIONS)}."
    prop = db.adaptive_get_proposal(int(proposal_id))
    if prop is None:
        return f"Error: no proposal #{proposal_id}."
    if prop.get("status") != "pending":
        return f"Nothing to do: proposal #{proposal_id} is already {prop.get('status')}."
    if decision == "reject":
        db.adaptive_resolve_proposal(int(proposal_id), "rejected")
        return f"Rejected proposal #{proposal_id}. Nothing was applied."
    from core.adaptive import AdaptiveError, approve_proposal, describe_resolution

    try:
        result = approve_proposal(int(proposal_id), actor="user")
    except AdaptiveError as e:
        return f"Error: could not approve #{proposal_id}: {e}"
    return f"Approved and applied proposal #{proposal_id}: {describe_resolution(prop, result)}"


def register(reg) -> None:
    if not settings.adaptive_enabled:
        return
    reg.register(
        name="adaptive_proposals",
        func=adaptive_proposals,
        description=(
            "Read the adaptive layer's pending proposals — the rules, notes, hints and "
            "self-tests the background passes (Dream, Refine) want to add or remove, "
            "shown in Self-tuning → Learning. action='list' gives every pending one "
            "with a plain-language WHAT / WHY / IF-YOU-DO-NOTHING; action='show' with "
            "proposal_id adds the raw payload, rationale and evidence. Use it when the "
            "user asks what a proposal means or whether to take it; follow evidence ids "
            "(session:…, pm:…) with search_sessions to see what actually happened."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(_PROPOSAL_ACTIONS)},
                "proposal_id": {"type": "integer", "description": "Required for show"},
            },
            "required": ["action"],
        },
        category="evaluation",
        tags=["adaptive", "proposal", "review", "learning", "self-improvement"],
        timeout=15,
        parallel_safe=True,
        safety_level="safe",
        denied_session_types={"canary", "worker", "cron"},
    )
    reg.register(
        name="adaptive_proposal_decide",
        func=adaptive_proposal_decide,
        description=(
            "Approve or reject one pending adaptive proposal — ONLY when the user has "
            "explicitly told you to in this conversation. Approve applies it at once "
            "through the same engine as the Learning tab's button (rollbackable from "
            "there); reject closes it and applies nothing. Never decide on your own "
            "initiative, and never to 'clean up' the queue."
        ),
        parameters={
            "type": "object",
            "properties": {
                "proposal_id": {"type": "integer"},
                "decision": {"type": "string", "enum": list(_DECISIONS)},
            },
            "required": ["proposal_id", "decision"],
        },
        category="evaluation",
        tags=["adaptive", "proposal", "approve", "reject", "learning"],
        timeout=30,
        parallel_safe=False,
        safety_level="caution",  # writes global prompt state (governed + rollbackable)
        denied_session_types={"canary", "worker", "cron"},
    )
    if not settings.adaptive_agent_notes_enabled:
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
