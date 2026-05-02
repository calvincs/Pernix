"""Pernix — Global event bus for job and snooze lifecycle events.

Mirrors the per-session subscriber pattern in sessions/state.py but is
not tied to any single session.  Any component can emit, any SSE client
can subscribe.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

logger = logging.getLogger("pernix.events")


class JobEventBus:
    """Global pub/sub for background task lifecycle events."""

    def __init__(self):
        self._subscribers: list[asyncio.Queue] = []
        self._events: deque = deque(maxlen=500)
        self._seq: int = 0

    def emit(self, event: dict) -> None:
        """Broadcast an event to all subscribers."""
        self._seq += 1
        event["_seq"] = self._seq
        if "timestamp" not in event:
            event["timestamp"] = time.time()
        self._events.append(event)

        dead = []
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def subscribe(self) -> asyncio.Queue:
        """Create a new subscriber queue."""
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        """Remove a subscriber queue."""
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    @property
    def recent_events(self) -> list[dict]:
        """Return buffered events (for replay on reconnect)."""
        return list(self._events)


_bus: JobEventBus | None = None


def get_event_bus() -> JobEventBus:
    global _bus
    if _bus is None:
        _bus = JobEventBus()
    return _bus
