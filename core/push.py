"""Pernix — VAPID key generation and Web Push sending."""

from __future__ import annotations

import asyncio
import json
import logging

logger = logging.getLogger("pernix.push")


def generate_vapid_keys() -> None:
    """Generate an EC P-256 VAPID key pair and persist to settings.json.

    Private key stored as base64url-encoded raw 32-byte scalar (no padding).
    This is the format Vapid.from_string() / from_raw() expects in pywebpush.
    Public key stored as base64url-encoded uncompressed point (65 bytes).
    """
    import base64

    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    from config import settings

    private_key = ec.generate_private_key(ec.SECP256R1())

    # Raw 32-byte private scalar — what Vapid.from_string() expects
    private_raw = private_key.private_numbers().private_value.to_bytes(32, "big")
    private_b64 = base64.urlsafe_b64encode(private_raw).rstrip(b"=")

    # Uncompressed public point (0x04 || x || y) — what the browser expects
    public_raw = private_key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    public_b64 = base64.urlsafe_b64encode(public_raw).rstrip(b"=")

    settings.vapid_private_key = private_b64.decode()
    settings.vapid_public_key = public_b64.decode()
    settings.save()


async def send_push(subscription: dict, title: str, body: str, session_id: str = "") -> bool:
    """Send a single Web Push message.

    Returns True on success, False if the subscription is stale (410 Gone).
    subscription: {"endpoint": ..., "p256dh": ..., "auth": ...}
    Raises on other network errors.
    """
    from pywebpush import WebPushException, webpush

    from config import settings

    payload = json.dumps({"title": title, "body": body, "session_id": session_id})
    try:
        await asyncio.to_thread(
            webpush,
            subscription_info={
                "endpoint": subscription["endpoint"],
                "keys": {
                    "p256dh": subscription["p256dh"],
                    "auth": subscription["auth"],
                },
            },
            data=payload,
            vapid_private_key=settings.vapid_private_key,
            vapid_claims={"sub": settings.vapid_subject},
        )
        return True
    except WebPushException as e:
        if e.response is not None and e.response.status_code in (401, 403, 410):
            return False  # credentials mismatch or expired — caller should delete
        logger.warning("WebPush failed for %s…: %s", subscription["endpoint"][:40], e)
        raise
