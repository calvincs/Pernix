"""Regression: two remaining ways blocking work could still freeze the UI.

Follow-on to test_2026-08-11_tool_pool_starved_api.py, which moved TOOL
dispatch off asyncio's default ThreadPoolExecutor. A review of that fix found
the same starvation class still reachable by two other routes.

**1. Queue time was charged against the tool's own timeout.**
`asyncio.wait_for(loop.run_in_executor(...))` starts its clock at SUBMIT, but
the pools are bounded — the callable does not begin until a thread frees up.
Under heavy fan-out a tool could therefore be reported as "timed out after
300s" having never executed a single line. That is both false and
un-actionable: the fix for a saturated pool is a bigger pool, not a longer
per-tool timeout. Dispatch now waits for execution to actually begin before
starting the tool's clock, and reports saturation as saturation.

**2. Long background work still sat on the default executor.**
Tool dispatch was moved, but the idle-time subsystems were not: a dream deep
probe (a full multi-iteration RLM run, retried once), canary maintenance,
synthesis, memory dedup and the backup all went through `asyncio.to_thread`.
Each can hold one of the ~20 default threads for minutes while every API route
needs that same pool for its DB reads — the identical "UI hangs while the event
loop is idle" signature, reached through a different door. They now run on the
dedicated pool in core/pools.py.

Pinned because both regress silently: nothing fails under light load, and the
symptom only appears under concurrency in production.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from core import pools
from core.tools import executor as tool_executor

# --- 1. Queue wait must not consume the dispatch timeout -------------------


class _Metric:
    def __init__(self):
        self.timeouts = 0

    def record_success(self, *a):
        pass

    def record_failure(self, *a):
        pass

    def record_timeout(self, *a):
        self.timeouts += 1


class _Metrics:
    def __init__(self):
        self._m = _Metric()

    def __getitem__(self, _name):
        return self._m


class _ToolDef:
    def __init__(self, timeout=5, long_poll=False):
        self.timeout = timeout
        self.max_timeout = 0
        self.long_poll = long_poll
        self.denied_session_types = set()
        self.safety_level = "safe"
        self.source = "builtin"
        self.parallel_safe = False


class _Reg:
    def __init__(self, tool, fn):
        self._tool = tool
        self._fn = fn
        self.metrics = _Metrics()

    def get(self, name):
        return self._tool

    def is_disabled(self, name):
        return False

    def execute_sync(self, name, arguments, context):
        return self._fn()


@pytest.fixture
def tiny_tool_pool(monkeypatch):
    """One-thread tool pool, and no dispatch grace.

    max_workers=1 forces the queuing this file is about. The grace is zeroed
    because _resolve_timeout() adds _DISPATCH_TIMEOUT_GRACE_S (5s) on top of
    every declared timeout — with it in play a 1s tool really gets 6s, which
    is enough slack to mask the very bug being pinned.
    """
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setattr(tool_executor, "_DISPATCH_TIMEOUT_GRACE_S", 0)
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pernix-tool")
    monkeypatch.setattr(tool_executor, "_get_tool_executor", lambda: pool)
    yield pool
    pool.shutdown(wait=False)


def test_queued_tool_is_not_charged_for_time_it_spent_waiting(tiny_tool_pool):
    """The shipped bug: a tool queued behind a slow sibling timed out without
    ever running.

    One worker thread, two dispatches. The hog holds the only thread for 1.5s;
    the second tool declares a 1s timeout and then runs instantly. Old
    arithmetic: wait_for starts at submit, so at t=1.0s — still queued, zero
    lines executed — it was reported as "timed out after 1s". New arithmetic:
    the 1s budget starts at t≈1.5s when a thread finally picks it up, so it
    completes normally.
    """
    hog_started = threading.Event()
    ran = []

    def _hog():
        hog_started.set()
        time.sleep(1.5)  # > the second tool's 1s timeout
        return "hog done"

    def _quick():
        ran.append(threading.current_thread().name)
        return "quick done"

    async def _drive():
        hog = asyncio.create_task(tool_executor._execute_single("hog", {}, None, _Reg(_ToolDef(timeout=10), _hog)))
        # Only dispatch the second once the first genuinely holds the thread.
        await asyncio.get_running_loop().run_in_executor(None, hog_started.wait)
        quick = tool_executor._execute_single("quick", {}, None, _Reg(_ToolDef(timeout=1), _quick))
        return await asyncio.gather(hog, quick)

    hog_res, quick_res = asyncio.run(_drive())

    assert hog_res.was_error is False
    assert quick_res.was_error is False, (
        f"queued tool reported {quick_res.content!r} — the shipped bug charged "
        "queue time against the tool's own timeout, so it 'timed out' without "
        "ever executing"
    )
    assert quick_res.content == "quick done"
    assert ran, "the queued tool never executed at all"


def test_tool_that_really_is_slow_still_times_out(tiny_tool_pool):
    """The queue-wait split must not disarm the actual timeout."""

    def _slow():
        time.sleep(2)
        return "should not be returned"

    res = asyncio.run(tool_executor._execute_single("slow", {}, None, _Reg(_ToolDef(timeout=1), _slow)))
    assert res.was_error is True
    assert "timed out" in res.content


def test_saturated_pool_reports_saturation_not_a_tool_timeout(tiny_tool_pool, monkeypatch):
    """A dispatch that never gets a thread must say so.

    'Timed out after Ns' sends the reader after the tool; the real cause is
    thread exhaustion, which has a different fix.
    """
    monkeypatch.setattr(tool_executor, "_QUEUE_WAIT_CEILING_S", 0.3)
    release = threading.Event()

    def _hog():
        release.wait(timeout=5)
        return "hog done"

    def _never():  # pragma: no cover - must never be reached
        raise AssertionError("queued call should not have executed")

    async def _drive():
        hog = asyncio.create_task(tool_executor._execute_single("hog", {}, None, _Reg(_ToolDef(timeout=10), _hog)))
        await asyncio.sleep(0.1)  # let the hog claim the only thread
        blocked = await tool_executor._execute_single("blocked", {}, None, _Reg(_ToolDef(timeout=30), _never))
        release.set()
        await hog
        return blocked

    res = asyncio.run(_drive())

    assert res.was_error is True
    assert "never started" in res.content
    assert "saturated" in res.content
    assert "timed out after" not in res.content


# --- 2. Long background work runs off the default executor -----------------


def test_run_background_uses_the_dedicated_pool_not_the_default_one():
    """Asserted by thread name: `asyncio.to_thread` workers are named
    `ThreadPoolExecutor-N_M`; ours are `pernix-bg_M`."""
    seen = {}

    def _work():
        seen["thread"] = threading.current_thread().name
        return 42

    result = asyncio.run(pools.run_background(_work))

    assert result == 42
    assert seen["thread"].startswith("pernix-bg"), (
        f"background work ran on {seen['thread']!r} — it must not occupy the "
        "default executor that every API route needs for its DB reads"
    )
    assert not seen["thread"].startswith("ThreadPoolExecutor-")


def test_run_background_forwards_args_and_kwargs():
    def _work(a, b, *, c):
        return (a, b, c)

    assert asyncio.run(pools.run_background(_work, 1, 2, c=3)) == (1, 2, 3)


def test_run_background_propagates_exceptions():
    def _boom():
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        asyncio.run(pools.run_background(_boom))


def test_background_pool_is_distinct_from_the_tool_pools():
    """Three separate ceilings. Sharing any pair would let one class of work
    starve another — which is the whole point of splitting them."""
    assert pools.get_background_executor() is not tool_executor._get_tool_executor()
    assert pools.get_background_executor() is not tool_executor._get_long_poll_executor()


def test_long_background_callers_do_not_use_to_thread():
    """Guards the call sites themselves, not just the helper.

    The helper is useless if someone reintroduces `asyncio.to_thread` at these
    specific lines, and that is an easy edit to make by habit.
    """
    import pathlib
    import re

    repo = pathlib.Path(__file__).resolve().parent.parent.parent
    offenders = []
    # (file, callable-name-as-written-at-the-call-site)
    watched = [
        ("core/dream/probe.py", "_run_engine_blocking"),
        ("core/snooze.py", "run_maintenance"),
        ("core/snooze.py", "synthesis.run"),
        ("maintenance.py", "run_backup"),
        ("core/memory/sweeps.py", "_pairwise_dedup"),
        ("core/retention.py", "scan_canaries"),
    ]
    for rel, fn in watched:
        text = (repo / rel).read_text()
        if re.search(r"to_thread\(\s*" + re.escape(fn) + r"\b", text):
            offenders.append(f"{rel}:{fn}")
        assert re.search(
            r"run_background\(\s*" + re.escape(fn) + r"\b", text
        ), f"{rel} no longer routes {fn} through run_background()"
    assert not offenders, f"back on the default executor: {offenders}"
