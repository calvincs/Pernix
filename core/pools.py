"""Thread pools that keep long blocking work off asyncio's default executor.

Background to the whole file: `asyncio.to_thread()` dispatches to the event
loop's DEFAULT ThreadPoolExecutor, sized `min(32, cpu_count + 4)` — 20 threads
on the deployment box. Every API route also hops that pool for its DB reads
(`await asyncio.to_thread(db...)` in api/routers/*, ~150 call sites), so
anything else parked there is in direct competition with the web UI for the
same 20 slots. When they run out the symptom is a UI that looks hung while the
event loop is idle and CPU sits near zero, recovering in batches as slots free.

core/tools/executor.py carved tool dispatch out for exactly this reason. This
module closes the same hole for the OTHER long occupants — the idle-time
background subsystems. They are far rarer than tool calls but individually much
longer: a dream deep-probe is a full multi-iteration RLM run (and retries once),
a backup walks a multi-gigabyte data directory, a canary maintenance sweep and
a memory dedup pass are both LLM-driven. Any one of them can hold a default
thread for minutes.

Short DB reads deliberately stay on `asyncio.to_thread`. They are measured in
milliseconds, there are ~150 of them, and moving them here would just relocate
the queue rather than shorten it. The rule this module encodes is about
DURATION, not about which subsystem the caller belongs to: if the callable can
plausibly hold its thread for seconds or longer, it belongs on this pool.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from concurrent.futures import ThreadPoolExecutor

from config import settings

logger = logging.getLogger("pernix.pools")

_background_executor: ThreadPoolExecutor | None = None


def get_background_executor() -> ThreadPoolExecutor:
    """The shared pool for long-running background work.

    Deliberately small. Occupants are heavyweight and idle-time-only, so the
    useful property here is a hard ceiling on how many can run at once, not
    throughput — a bounded pool also stops a stuck sweep from being retried
    into unbounded thread growth. Sized from settings on first use; a running
    process keeps the size it started with.
    """
    global _background_executor
    if _background_executor is None:
        _background_executor = ThreadPoolExecutor(
            max_workers=max(2, int(settings.background_executor_workers)),
            thread_name_prefix="pernix-bg",
        )
    return _background_executor


async def run_background(fn, /, *args, **kwargs):
    """Run a long blocking callable on the background pool.

    Drop-in for `asyncio.to_thread(fn, *args, **kwargs)` — same signature, same
    await semantics — differing only in which pool absorbs the block. Like
    to_thread, the call cannot be cancelled once the thread has entered it;
    cancelling the awaiting task abandons the result but does not stop the work.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(get_background_executor(), functools.partial(fn, *args, **kwargs))


def shutdown_background_executor(wait: bool = False) -> None:
    """Release the pool. Used by tests; harmless if it was never created."""
    global _background_executor
    if _background_executor is not None:
        _background_executor.shutdown(wait=wait)
        _background_executor = None
