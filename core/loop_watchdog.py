"""Log what the asyncio event loop is doing when it stops responding.

One blocking call on the loop freezes every session, scout and API route at
once, and the only symptom is a gap in the log. A daemon thread asks the loop
to answer every second. When no answer comes for STALL_S seconds it logs the
loop thread's stack, once per stall, then logs the length of the stall when
the loop comes back.
"""

from __future__ import annotations

import asyncio
import faulthandler
import logging
import signal
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


_IDLE_FRAMES = ("wait", "get", "select", "accept", "poll", "_worker", "sleep")


def _busy_threads(skip: set[int]) -> str:
    """One short stack per thread that is not parked. A thread that burns CPU
    starves the loop of the GIL, so the loop's own stack shows only the victim."""
    names = {t.ident: t.name for t in threading.enumerate()}
    out = []
    for ident, frame in sys._current_frames().items():
        if ident in skip or frame.f_code.co_name in _IDLE_FRAMES:
            continue
        where = " < ".join(
            f"{f.filename.rsplit('/', 1)[-1]}:{f.lineno} {f.name}"
            for f in reversed(traceback.extract_stack(frame)[-5:])
        )
        out.append(f"  [{names.get(ident, ident)}] {where}")
    return "\n".join(out) or "  (none)"


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
            logger.warning(
                "Event loop stalled for %.1fs. Loop thread stack:\n%s\nOther busy threads:\n%s",
                lag,
                _loop_thread_stack(thread_id),
                _busy_threads({thread_id, threading.get_ident()}),
            )
        elif lag < stall_s and stalled_at is not None:
            logger.warning("Event loop recovered after %.1fs", last_seen[0] - stalled_at)
            stalled_at = None


def start(stall_s: float = STALL_S, poll_s: float = _POLL_S) -> threading.Event:
    """Start watching the running loop. Set the returned event to stop."""
    loop = asyncio.get_running_loop()
    # `kill -USR1 <pid>` dumps every thread's stack to stderr. It needs no GIL
    # and no ptrace, so it works on a busy process inside the container.
    try:
        faulthandler.register(signal.SIGUSR1, file=sys.__stderr__, all_threads=True, chain=False)
    except (AttributeError, OSError, ValueError) as e:
        logger.debug("Stack dump signal not registered: %s", e)
    stop = threading.Event()
    threading.Thread(
        target=_watch,
        args=(loop, threading.get_ident(), stop, stall_s, poll_s),
        name="pernix-loop-watchdog",
        daemon=True,
    ).start()
    return stop
