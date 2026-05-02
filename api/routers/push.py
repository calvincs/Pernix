"""Pernix — Web Push (VAPID) subscription endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from config import settings
from db import models as db

router = APIRouter(tags=["push"])


@router.get("/api/push/vapid-public-key")
async def get_vapid_key():
    """Return the VAPID public key for browser push subscription."""
    if not settings.vapid_public_key:
        raise HTTPException(503, detail="VAPID not configured")
    return {"publicKey": settings.vapid_public_key}


@router.post("/api/push/subscribe")
async def subscribe(body: dict):
    """Store a browser push subscription (upsert by endpoint)."""
    endpoint = body.get("endpoint")
    p256dh = (body.get("keys") or {}).get("p256dh")
    auth = (body.get("keys") or {}).get("auth")
    if not (endpoint and p256dh and auth):
        raise HTTPException(400, detail="Missing endpoint or keys")
    db.upsert_push_subscription(endpoint, p256dh, auth)
    return {"status": "subscribed"}


@router.delete("/api/push/subscribe")
async def unsubscribe(body: dict):
    """Remove a push subscription by endpoint."""
    endpoint = body.get("endpoint")
    if not endpoint:
        raise HTTPException(400, detail="Missing endpoint")
    db.delete_push_subscription(endpoint)
    return {"status": "unsubscribed"}
