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
from core.events import get_event_bus

logger = logging.getLogger("pernix.notify")


class NotificationDispatcher:
    """Watches the global event bus and dispatches to notification handlers."""

    def __init__(self):
        self._handlers: list = []
        self._task: asyncio.Task | None = None
        self._queue: asyncio.Queue | None = None

    def start(self) -> None:
        """Subscribe to the global bus and begin processing events."""
        bus = get_event_bus()
        self._queue = bus.subscribe()
        if settings.notify_webhook_url:
            self._handlers.append(self._send_webhook)
        if settings.vapid_private_key:
            self._handlers.append(self._send_web_push)
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
        from core.push import send_push
        from db import models as db

        subscriptions = db.get_push_subscriptions()
        if not subscriptions:
            return
        etype = event.get("type")
        if etype == "dialog.question":
            session_title = event.get("session_title") or ""
            title = f"Question: {session_title}" if session_title else "Agent Question"
            body = event.get("question") or ""
        else:
            title = event.get("title") or "Pernix"
            body = event.get("body") or ""
        session_id = event.get("source_session_id") or event.get("session_id") or ""
        stale = []
        for sub in subscriptions:
            try:
                ok = await send_push(sub, title, body, session_id)
                if not ok:
                    stale.append(sub["endpoint"])
            except Exception as e:
                logger.warning("Web Push send failed: %s", e)
        for ep in stale:
            db.delete_push_subscription(ep)
            logger.info("Removed stale push subscription: %s…", ep[:40])

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
