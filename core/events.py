"""Pernix — Global event bus for job and snooze lifecycle events.

Mirrors the per-session subscriber pattern in sessions/state.py but is
not tied to any single session.  Any component can emit, any SSE client
can subscribe.
"""

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger("pernix.events")

# ---------------------------------------------------------------------------
# Main-loop marshaling
# ---------------------------------------------------------------------------
# asyncio.Queue is NOT thread-safe: put_nowait from a tool/worker thread
# wakes waiting getters via loop.call_soon (not call_soon_threadsafe), which
# mutates the loop's ready queue without synchronization and never wakes the
# selector — events emitted from tool threads (ask_user, worker progress)
# could arrive seconds late or, in rare interleavings, corrupt loop state.
# All subscriber-queue deliveries route through run_on_loop().

_main_loop: asyncio.AbstractEventLoop | None = None


def set_main_loop(loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Capture the server's main event loop (called once at startup)."""
    global _main_loop
    _main_loop = loop if loop is not None else asyncio.get_running_loop()


def run_on_loop(fn, *args) -> None:
    """Execute fn(*args) on the main event loop.

    On the loop thread (or any running loop — tests run one per test) the
    call is inline. From a foreign thread it is marshaled via
    call_soon_threadsafe. With no loop available at all (sync unit tests),
    fall back to inline — matching the old behavior.
    """
    try:
        asyncio.get_running_loop()
        fn(*args)
        return
    except RuntimeError:
        pass
    loop = _main_loop
    if loop is not None and not loop.is_closed():
        loop.call_soon_threadsafe(fn, *args)
        return
    fn(*args)


def call_on_loop(fn, *args, loop: asyncio.AbstractEventLoop | None = None, timeout: float = 10.0):
    """Run fn(*args) on the event loop and return its result, blocking the
    calling thread until done.

    For tool-thread code that must mutate loop-affine state (session state
    transitions, watch-sets): sessions.state_v2.transition() is a multi-step
    read-modify-write whose serialization contract is "event loop only" —
    running it from a to_thread worker can interleave with loop-side
    transitions and reaper reads. On the loop thread (or any running loop)
    the call is inline.
    """
    try:
        asyncio.get_running_loop()
        return fn(*args)
    except RuntimeError:
        pass
    target = loop if loop is not None else _main_loop
    if target is None or target.is_closed():
        return fn(*args)

    async def _wrapper():
        return fn(*args)

    return asyncio.run_coroutine_threadsafe(_wrapper(), target).result(timeout)


class JobEventBus:
    """Global pub/sub for background task lifecycle events."""

    def __init__(self):
        import threading

        self._subscribers: list[asyncio.Queue] = []
        self._seq: int = 0
        self._buffer_lock = threading.Lock()

    def emit(self, event: dict) -> None:
        """Broadcast an event to all subscribers. Safe from any thread —
        seq assignment is lock-guarded and queue delivery is marshaled
        onto the main event loop."""
        with self._buffer_lock:
            self._seq += 1
            event["_seq"] = self._seq
            if "timestamp" not in event:
                event["timestamp"] = time.time()
        run_on_loop(self._deliver, event)

    def _deliver(self, event: dict) -> None:
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


_bus: JobEventBus | None = None


def get_event_bus() -> JobEventBus:
    global _bus
    if _bus is None:
        _bus = JobEventBus()
    return _bus
