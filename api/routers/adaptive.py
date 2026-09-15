"""Pernix — Adaptive Layer endpoints (adaptation plan 4f)."""

from __future__ import annotations

import asyncio as _asyncio

from fastapi import APIRouter, HTTPException

from config import settings
from db import models as db

router = APIRouter(tags=["adaptive"])


@router.get("/api/adaptive/entries")
async def list_entries(kind: str = "", status: str = db.ADAPTIVE_LIVE_STATUS, limit: int = 200):
    """Entries by kind and status.

    `status` takes one status or a comma-separated list; empty means every
    status. The default is the LIVE set — `active,trial` — because a trial
    entry (W6) is in the prompt on half the turns, and a listing that showed
    only `active` would report the store as empty while the agent was reading
    from it.
    """
    rows = await _asyncio.to_thread(
        db.adaptive_list_entries, kind or None, None, status or None, max(1, min(limit, 500))
    )
    # Per-entry usage counters (the v3.1 usefulness signal) ride along so
    # the panel can show which entries actually earn their prompt space.
    try:
        signals = await _asyncio.to_thread(db.get_signals_by_subjects, [("adaptive_entry", r["id"]) for r in rows])
        by_id = {s["subject"]: s for s in signals}
        for r in rows:
            s = by_id.get(r["id"])
            r["usage"] = (
                {"uses": s["reinforcements"], "successes": s["successes"], "failures": s["failures"]} if s else None
            )
    except Exception:
        pass  # counters are decoration; the listing must never fail over them
    # Receipts: does this entry cite anything the system actually recorded?
    # Graded from the creating event on read (no column), so it moves when a
    # resolver lands rather than being frozen at create time.
    try:
        from core.adaptive.receipts import grade as _grade

        grades = await _asyncio.to_thread(lambda: {r["id"]: _grade(r["id"]) for r in rows})
        for r in rows:
            r["evidence_grade"] = grades.get(r["id"])
    except Exception:
        pass  # same posture as the counters above
    return {"enabled": settings.adaptive_enabled, "auto_apply": settings.adaptive_auto_apply, "entries": rows}


@router.post("/api/adaptive/entries")
async def create_entry_route(body: dict = {}):
    """Direct authorship (v3.1): the human writes an entry; being the human
    IS the approval step — no proposal detour, no lint (the lint substitutes
    for this judgment, not the other way around). Journaled like any edit."""
    from core.adaptive import AdaptiveError, create_entry

    try:
        return await _asyncio.to_thread(
            create_entry,
            str(body.get("kind") or ""),
            str(body.get("title") or ""),
            str(body.get("content") or ""),
            str(body.get("scope") or "global"),
        )
    except AdaptiveError as e:
        raise HTTPException(400, detail=str(e)) from e


@router.delete("/api/adaptive/entries/{entry_id}")
async def delete_entry_route(entry_id: str):
    """Release valve: soft-delete an entry (journaled, rollback-able) so a
    per-kind cap wedged full of stale machine entries can be freed."""
    from core.adaptive import AdaptiveError, delete_entry

    try:
        return await _asyncio.to_thread(delete_entry, entry_id, "human")
    except AdaptiveError as e:
        raise HTTPException(404, detail=str(e)) from e


@router.get("/api/adaptive/events")
async def list_events(batch_id: str = "", entry_id: str = "", limit: int = 100):
    rows = await _asyncio.to_thread(
        db.adaptive_list_events, batch_id or None, entry_id or None, max(1, min(limit, 500))
    )
    return {"events": rows}


@router.get("/api/adaptive/batches")
async def list_batches(status: str = "", limit: int = 100):
    rows = await _asyncio.to_thread(db.adaptive_list_batches, status or None, max(1, min(limit, 500)))
    return {"batches": rows}


PROPOSAL_STATUSES = ("pending", "approved", "auto_approved", "auto_applied", "rejected", "expired")


@router.get("/api/adaptive/proposals")
async def list_proposals(status: str = "pending", limit: int = 100, id: int | None = None):
    """Proposals by status — one of PROPOSAL_STATUSES, or "all".

    An unknown status is a 400 that names the enum: it used to return an
    empty list, which reads as "no data" (the agent on the live box tried
    `?status=applied`, saw [], and concluded resolved rows are deleted).
    `id` fetches one row whatever its status. Every row is annotated with
    `summary`, `auto_approve_exempt` and `auto_approve_after` so a reader can
    tell what a proposal is and what the veto-window drain will do with it.
    """
    from core.adaptive import annotate_proposal

    if id is not None:
        row = await _asyncio.to_thread(db.adaptive_get_proposal, id)
        if row is None:
            raise HTTPException(404, detail=f"no proposal {id}")
        return {"proposals": [annotate_proposal(row)], "statuses": list(PROPOSAL_STATUSES), "status": "any"}
    if status not in PROPOSAL_STATUSES and status not in ("", "all"):
        raise HTTPException(
            400,
            detail=f"unknown status {status!r}; use one of {', '.join(PROPOSAL_STATUSES)}, or 'all'",
        )
    wanted = None if status in ("", "all") else status
    rows = await _asyncio.to_thread(db.adaptive_list_proposals, wanted, max(1, min(limit, 500)))
    return {
        "proposals": [annotate_proposal(r) for r in rows],
        "statuses": list(PROPOSAL_STATUSES),
        "status": status or "all",
    }


@router.get("/api/adaptive/proposals/{proposal_id}")
async def get_proposal(proposal_id: int):
    from core.adaptive import annotate_proposal

    row = await _asyncio.to_thread(db.adaptive_get_proposal, proposal_id)
    if row is None:
        raise HTTPException(404, detail=f"no proposal {proposal_id}")
    return annotate_proposal(row)


@router.post("/api/adaptive/proposals/{proposal_id}/approve")
async def approve(proposal_id: int):
    """Apply-on-approve: executes the batch through the same apply engine
    as auto-applies and enqueues a batch-tagged canary sweep."""
    from core.adaptive import AdaptiveError, approve_proposal

    try:
        result = await _asyncio.to_thread(approve_proposal, proposal_id, "user")
    except AdaptiveError as e:
        raise HTTPException(400, detail=str(e)) from e
    return result


@router.post("/api/adaptive/proposals/{proposal_id}/discuss")
async def discuss(proposal_id: int):
    """Open a normal chat session about one proposal.

    The Learning tab's cards are the row that produced them — enough to
    audit, not enough to decide by. This mints a session titled after the
    proposal and hands back the first message for the composer to send, so
    the person can ask the agent what a proposal means, what the session it
    came from actually did, and whether to take it — in the same window
    every other conversation happens in. The agent reads the row through
    the `adaptive_proposals` tool and can act on `adaptive_proposal_decide`
    only when told to; the opener says so.
    """
    from core.adaptive.explain import producer_label
    from sessions.manager import get_manager

    prop = await _asyncio.to_thread(db.adaptive_get_proposal, proposal_id)
    if prop is None:
        raise HTTPException(404, detail=f"no proposal {proposal_id}")
    from core.adaptive import describe_proposal

    short = describe_proposal(prop)
    short = short.split(":", 1)[1].strip() if ":" in short else short
    title = f"Proposal #{proposal_id} — {short}"[:80]
    sid = get_manager().create_session(title=title, session_type="normal")
    opener = (
        f"I'm looking at adaptive proposal #{proposal_id} in Self-tuning → Learning "
        f"(from {producer_label(prop.get('producer'))}). Read it with "
        f'adaptive_proposals(action="show", proposal_id={proposal_id}) and explain it to me in plain '
        "language: what would change, why the system suggested it, what happens if I approve or reject it, "
        "and what you would do. Keep it short. I'll decide after — don't approve or reject anything unless I say so."
    )
    return {"session_id": sid, "opener": opener, "title": title}


@router.post("/api/adaptive/proposals/{proposal_id}/reject")
async def reject(proposal_id: int):
    prop = await _asyncio.to_thread(db.adaptive_get_proposal, proposal_id)
    if prop is None:
        raise HTTPException(404, detail=f"no proposal {proposal_id}")
    if prop.get("status") != "pending":
        raise HTTPException(400, detail=f"proposal is {prop.get('status')}, not pending")
    await _asyncio.to_thread(db.adaptive_resolve_proposal, proposal_id, "rejected")
    return {"status": "rejected"}


@router.post("/api/adaptive/rollback")
async def rollback_route(body: dict = {}):
    """Roll back a batch (batch_id) or a single event (event_id)."""
    from core.adaptive import AdaptiveError, rollback

    batch_id = (body.get("batch_id") or "").strip() or None
    event_id = body.get("event_id")
    try:
        result = await _asyncio.to_thread(rollback, batch_id, int(event_id) if event_id else None, "user")
    except AdaptiveError as e:
        raise HTTPException(400, detail=str(e)) from e
    return result


@router.post("/api/adaptive/batches/{batch_id}/dismiss")
async def dismiss_suspect(batch_id: str):
    """Human dismiss of a tripwire flag: suspect → applied, cleared_at set.

    cleared_at is what makes the dismiss durable — the tripwire sweep skips
    cleared batches, so it cannot re-flag this one on the same evidence.
    """
    batch = await _asyncio.to_thread(db.adaptive_get_batch, batch_id)
    if batch is None:
        raise HTTPException(404, detail=f"no batch {batch_id}")
    if batch.get("status") != "suspect":
        raise HTTPException(400, detail=f"batch is {batch.get('status')}, not suspect")
    from db.models import _now

    await _asyncio.to_thread(db.adaptive_update_batch, batch_id, "applied", None, _now())
    return {"status": "applied", "cleared": True}
