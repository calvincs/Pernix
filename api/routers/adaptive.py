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
    return {"enabled": settings.adaptive_enabled, "auto_apply": settings.adaptive_auto_apply, "entries": rows}


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


@router.get("/api/adaptive/proposals")
async def list_proposals(status: str = "pending", limit: int = 100):
    rows = await _asyncio.to_thread(db.adaptive_list_proposals, status or None, max(1, min(limit, 500)))
    return {"proposals": rows}


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
    """Human dismiss of a tripwire flag: suspect → applied, cleared_at set."""
    batch = await _asyncio.to_thread(db.adaptive_get_batch, batch_id)
    if batch is None:
        raise HTTPException(404, detail=f"no batch {batch_id}")
    if batch.get("status") != "suspect":
        raise HTTPException(400, detail=f"batch is {batch.get('status')}, not suspect")
    from db.models import _now

    await _asyncio.to_thread(db.adaptive_update_batch, batch_id, "applied", None, _now())
    return {"status": "applied", "cleared": True}
