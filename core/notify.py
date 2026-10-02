"""Pernix — Notification dispatcher for agent questions and alerts.

Subscribes to the global event bus and dispatches notifications to
configured channels (webhook, future: SMS/email/push).
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request

from config import settings

# Notification urgencies in ascending order; the push floor compares ranks.
_URGENCY_RANK = {"low": 0, "normal": 1, "high": 2, "urgent": 3}
from core.events import get_event_bus

logger = logging.getLogger("pernix.notify")

# Consecutive 401/403 answers from one endpoint before the user hears about it.
_PUSH_REJECT_NOTICE_AFTER = 3


def _vapid_subject_problem(subject: str) -> str:
    """Why this VAPID subject is likely to be refused, or "" when it looks like a real contact."""
    s = (subject or "").strip()
    if not s:
        return "is empty"
    if "localhost" in s.lower():
        return f"is {s!r}, which points at localhost"
    if not s.lower().startswith(("mailto:", "https:")):
        return f"is {s!r}, which is neither a mailto: nor an https: URL"
    return ""


def _warn_if_vapid_subject_suspect(subject: str) -> None:
    # Apple answers a bad subject with 403 BadJwtToken on every push, which
    # looks exactly like a dead phone unless someone says so at boot.
    problem = _vapid_subject_problem(subject)
    if problem:
        logger.warning(
            "vapid_subject %s; Apple and Google push services may reject every push (HTTP 403). "
            "Set it to a real mailto: or https: contact.",
            problem,
        )


def _notice_push_rejected(host: str, status: int | None, reason: str) -> None:
    """Bell notice for a push service that keeps refusing our credentials. One call site, easy to reroute."""
    from db import models as db

    detail = f"HTTP {status} {reason}".strip()
    try:
        db.add_notification(
            title=f"Push rejected by {host}",
            body=(
                f"{host} answered {detail}. Check vapid_subject in settings "
                "(it must be a real mailto: or https: contact, not localhost)."
            ),
            urgency="normal",
            dedup_key=f"push_rejected:{host}",
        )
    except Exception as e:
        logger.warning("Could not record push-rejected notice: %s", e)


class NotificationDispatcher:
    """Watches the global event bus and dispatches to notification handlers."""

    def __init__(self):
        self._handlers: list = []
        self._task: asyncio.Task | None = None
        self._queue: asyncio.Queue | None = None
        # endpoint -> consecutive 401/403 answers; a success or deletion clears it.
        self._push_rejects: dict[str, int] = {}
        # Endpoints already given their bell notice in this process.
        self._push_reject_noticed: set[str] = set()

    def start(self) -> None:
        """Subscribe to the global bus and begin processing events."""
        bus = get_event_bus()
        self._queue = bus.subscribe()
        if settings.notify_webhook_url:
            self._handlers.append(self._send_webhook)
        if settings.vapid_private_key:
            self._handlers.append(self._send_web_push)
            _warn_if_vapid_subject_suspect(settings.vapid_subject)
        self._task = asyncio.create_task(self._process_events())
        self._task.add_done_callback(self._on_task_done)
        logger.info("Notification dispatcher started (%d handler(s))", len(self._handlers))

    def register_handler(self, handler) -> None:
        """Register an additional async notification handler."""
        self._handlers.append(handler)

    @staticmethod
    def _on_task_done(task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception():
            logger.error("Notification dispatcher died: %s", task.exception())

    async def stop(self) -> None:
        """Cancel the event processing task."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._queue:
            from core.events import get_event_bus

            get_event_bus().unsubscribe(self._queue)

    async def _process_events(self) -> None:
        consecutive_errors = 0
        while True:
            try:
                event = await self._queue.get()
                if event.get("type") not in ("dialog.question", "dialog.notification"):
                    continue
                for handler in self._handlers:
                    try:
                        await handler(event)
                    except Exception as e:
                        logger.warning("Notification handler failed: %s", e)
                consecutive_errors = 0
            except asyncio.CancelledError:
                break
            except Exception as e:
                consecutive_errors += 1
                logger.error("Notification dispatcher error (%d): %s", consecutive_errors, e)
                if consecutive_errors >= 10:
                    logger.error("Too many consecutive errors, stopping dispatcher")
                    break
                await asyncio.sleep(1)

    async def _send_webhook(self, event: dict) -> None:
        url = settings.notify_webhook_url
        if not url:
            return
        # Defense-in-depth: validate even admin-configured URLs
        try:
            from core.extensions.web import _validate_url

            url = _validate_url(url)
        except (ValueError, Exception) as e:
            logger.warning("Webhook URL validation failed: %s", e)
            return
        payload = json.dumps({k: v for k, v in event.items() if not k.startswith("_")}).encode()
        if len(payload) > 64 * 1024:  # 64KB cap
            payload = json.dumps({"type": event.get("type"), "error": "payload_too_large"}).encode()
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._post, url, payload),
                timeout=settings.notify_webhook_timeout,
            )
            logger.debug("Webhook POST to %s succeeded", url)
        except Exception as e:
            logger.warning("Webhook POST to %s failed: %s", url, e)

    async def _send_web_push(self, event: dict) -> None:
        from core.push import PushResult, endpoint_host, send_push
        from db import models as db

        subscriptions = db.get_push_subscriptions()
        if not subscriptions:
            return
        etype = event.get("type")
        # A phone buzz is the most expensive notification there is. Questions
        # always go through; the rest must clear the configured floor.
        floor = _URGENCY_RANK.get(str(getattr(settings, "push_urgency_floor", "normal") or "normal"), 1)
        if etype != "dialog.question" and _URGENCY_RANK.get(str(event.get("urgency") or "normal"), 1) < floor:
            return
        if etype == "dialog.question":
            session_title = event.get("session_title") or ""
            title = f"Question: {session_title}" if session_title else "Agent Question"
            body = event.get("question") or ""
        else:
            title = event.get("title") or "Pernix"
            body = event.get("body") or ""
        session_id = event.get("source_session_id") or event.get("session_id") or ""
        gone = []
        for sub in subscriptions:
            ep = sub.get("endpoint", "")
            try:
                result = await send_push(sub, title, body, session_id)
            except Exception as e:
                logger.warning("Web Push send failed: %s", e)
                continue
            if isinstance(result, bool):  # a bare bool (older fakes/callers): True = sent, False = gone
                result = PushResult(ok=result, gone=not result)
            if result.ok:
                self._push_rejects.pop(ep, None)
            elif result.gone:
                gone.append(ep)
            elif result.rejected:
                self._note_push_rejected(ep, result)
        for ep in gone:
            self._push_rejects.pop(ep, None)
            db.delete_push_subscription(ep)
            logger.info("Removed gone push subscription on %s", endpoint_host(ep))

    def _note_push_rejected(self, endpoint: str, result) -> None:
        """Count a 401/403 for this endpoint and raise one bell notice once it is clearly persistent.

        The subscription is kept: a 401/403 is the push service refusing our
        VAPID credentials, which a settings fix repairs without the phone
        having to re-subscribe. Three in a row rules out a one-off blip.
        """
        from core.push import endpoint_host

        count = self._push_rejects.get(endpoint, 0) + 1
        self._push_rejects[endpoint] = count
        if count < _PUSH_REJECT_NOTICE_AFTER or endpoint in self._push_reject_noticed:
            return
        self._push_reject_noticed.add(endpoint)
        _notice_push_rejected(endpoint_host(endpoint), result.status, result.reason)

    @staticmethod
    def _post(url: str, payload: bytes) -> None:
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)


_dispatcher: NotificationDispatcher | None = None


def get_dispatcher() -> NotificationDispatcher:
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = NotificationDispatcher()
    return _dispatcher
