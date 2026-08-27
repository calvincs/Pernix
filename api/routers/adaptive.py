"""Pernix — Adaptive Layer endpoints (adaptation plan 4f)."""

from __future__ import annotations

import asyncio as _asyncio

from fastapi import APIRouter, HTTPException

from config import settings
from db import models as db

router = APIRouter(tags=["adaptive"])


@router.get("/api/adaptive/entries")
async def list_entries(kind: str = "", status: str = "active", limit: int = 200):
    rows = await _asyncio.to_thread(
        db.adaptive_list_entries, kind or None, None, status or None, max(1, min(limit, 500))
    )
    # Per-entry usage counters (the v3.1 usefulness signal) ride along so
    # the panel can show which entries actually earn their prompt space.
    try:
        signals = await _asyncio.to_thread(
            db.get_signals_by_subjects, [("adaptive_entry", r["id"]) for r in rows]
        )
        by_id = {s["subject"]: s for s in signals}
        for r in rows:
            s = by_id.get(r["id"])
            r["usage"] = (
                {"uses": s["reinforcements"], "successes": s["successes"], "failures": s["failures"]} if s else None
            )
    except Exception:
        pass  # counters are decoration; the listing must never fail over them
    return {"enabled": settings.adaptive_enabled, "auto_apply": settings.adaptive_auto_apply, "entries": rows}


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
