"""Pernix — VAPID key generation and Web Push sending."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from urllib.parse import urlsplit

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


# Process-lifetime outcome counters, published on /api/health. A phone that
# never buzzes is otherwise indistinguishable from a push service that never
# accepts our pushes: neither leaves a trace without these.
_STATS: dict[str, int] = {"push_sent_ok": 0, "push_rejected": 0, "push_gone": 0, "push_failed": 0}


def push_stats() -> dict[str, int]:
    """Snapshot of the send counters since process start."""
    return dict(_STATS)


@dataclass(frozen=True)
class PushResult:
    """Outcome of one send. Truthy only on success, so `if not result` still reads as "did not arrive".

    gone     -- 404/410: the browser dropped the subscription; delete it.
    rejected -- 401/403: the push service refused OUR credentials (usually the
                VAPID subject). The subscription is fine; deleting it would
                destroy a good phone registration and hide the mis-configuration.
    """

    ok: bool
    gone: bool = False
    rejected: bool = False
    status: int | None = None
    reason: str = ""

    def __bool__(self) -> bool:
        return self.ok


def endpoint_host(endpoint: str) -> str:
    """Host part of a push endpoint. The path is a per-device capability token, so it is never logged."""
    return urlsplit(endpoint or "").hostname or "?"


def _response_reason(response) -> str:
    try:
        text = (response.text or "").strip() or (response.reason or "")
    except Exception:
        text = ""
    return " ".join(str(text).split())[:200]


async def send_push(subscription: dict, title: str, body: str, session_id: str = "") -> PushResult:
    """Send a single Web Push message.

    subscription: {"endpoint": ..., "p256dh": ..., "auth": ...}
    Returns a PushResult for any answer the push service gave (success, gone,
    rejected). Raises on network errors and on other HTTP failures, as before.
    """
    from pywebpush import WebPushException, webpush

    from config import settings

    host = endpoint_host(subscription.get("endpoint", ""))
    payload = json.dumps({"title": title, "body": body, "session_id": session_id})
    try:
        response = await asyncio.to_thread(
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
    except WebPushException as e:
        status = e.response.status_code if e.response is not None else None
        reason = _response_reason(e.response) if e.response is not None else " ".join(str(e).split())[:200]
        if status in (404, 410):
            _STATS["push_gone"] += 1
            logger.warning("WebPush to %s: HTTP %s (subscription gone) %s", host, status, reason)
            return PushResult(ok=False, gone=True, status=status, reason=reason)
        if status in (401, 403):
            _STATS["push_rejected"] += 1
            logger.warning("WebPush to %s rejected our credentials: HTTP %s %s", host, status, reason)
            return PushResult(ok=False, rejected=True, status=status, reason=reason)
        _STATS["push_failed"] += 1
        logger.warning("WebPush to %s failed: HTTP %s %s", host, status, reason)
        raise
    except Exception as e:
        _STATS["push_failed"] += 1
        logger.warning("WebPush to %s failed: %s", host, " ".join(str(e).split())[:200])
        raise
    _STATS["push_sent_ok"] += 1
    logger.debug("WebPush to %s sent: HTTP %s", host, getattr(response, "status_code", None))
    return PushResult(ok=True, status=getattr(response, "status_code", None))
