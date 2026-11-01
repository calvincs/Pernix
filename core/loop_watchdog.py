"""Log what the asyncio event loop is doing when it stops responding.

One blocking call on the loop freezes every session, scout and API route at
once, and the only symptom is a gap in the log. A daemon thread asks the loop
to answer every second. When no answer comes for STALL_S seconds it logs the
loop thread's stack, once per stall, then logs the length of the stall when
the loop comes back.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import traceback

logger = logging.getLogger("pernix.watchdog")

STALL_S = 5.0
_POLL_S = 1.0


def _loop_thread_stack(thread_id: int) -> str:
    frame = sys._current_frames().get(thread_id)
    if frame is None:
        return "(no frame)"
    return "".join(traceback.format_stack(frame)[-12:])


def _watch(loop: asyncio.AbstractEventLoop, thread_id: int, stop: threading.Event, stall_s: float, poll_s: float):
    last_seen = [time.monotonic()]

    def _pong() -> None:
        last_seen[0] = time.monotonic()

    stalled_at: float | None = None
    while not stop.wait(poll_s):
        try:
            loop.call_soon_threadsafe(_pong)
        except RuntimeError:
            return  # loop closed
        lag = time.monotonic() - last_seen[0]
        if lag >= stall_s and stalled_at is None:
            stalled_at = last_seen[0]
            logger.warning("Event loop stalled for %.1fs. Loop thread stack:\n%s", lag, _loop_thread_stack(thread_id))
        elif lag < stall_s and stalled_at is not None:
            logger.warning("Event loop recovered after %.1fs", last_seen[0] - stalled_at)
            stalled_at = None


def start(stall_s: float = STALL_S, poll_s: float = _POLL_S) -> threading.Event:
    """Start watching the running loop. Set the returned event to stop."""
    loop = asyncio.get_running_loop()
    stop = threading.Event()
    threading.Thread(
        target=_watch,
        args=(loop, threading.get_ident(), stop, stall_s, poll_s),
        name="pernix-loop-watchdog",
        daemon=True,
    ).start()
    return stop
