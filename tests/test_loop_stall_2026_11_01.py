"""A blocked event loop must be named in the log, and deep_recall must not block it."""

import asyncio
import logging
import threading
import time

import core.tools.builtin.memory_tools as mt
from core import loop_watchdog


def test_watchdog_logs_the_blocking_stack_and_the_recovery(caplog):
    def _blocking_call_for_test():
        time.sleep(0.5)

    async def main():
        stop = loop_watchdog.start(stall_s=0.2, poll_s=0.05)
        await asyncio.sleep(0.1)
        _blocking_call_for_test()
        await asyncio.sleep(0.2)
        stop.set()

    with caplog.at_level(logging.WARNING, logger="pernix.watchdog"):
        asyncio.run(main())

    text = caplog.text
    assert "Event loop stalled" in text
    assert "_blocking_call_for_test" in text
    assert "Event loop recovered" in text


def test_watchdog_is_silent_on_a_healthy_loop(caplog):
    async def main():
        stop = loop_watchdog.start(stall_s=0.2, poll_s=0.05)
        await asyncio.sleep(0.4)
        stop.set()

    with caplog.at_level(logging.WARNING, logger="pernix.watchdog"):
        asyncio.run(main())
    assert "stalled" not in caplog.text


def test_deep_recall_tool_runs_off_the_event_loop(monkeypatch):
    seen = {}

    class _Resp:
        def __init__(self, tool_calls, content=""):
            self.tool_calls = tool_calls
            self.content = content

    class _Call:
        id = "1"
        name = "search_memory"
        arguments = '{"query": "x"}'

    class _Client:
        calls = 0

        async def chat(self, *a, **k):
            _Client.calls += 1
            return _Resp([_Call()]) if _Client.calls == 1 else _Resp([], "done")

    def _tool(*a, **k):
        seen["thread"] = threading.get_ident()
        return "result"

    monkeypatch.setattr("core.llm.client.get_llm_client", lambda: _Client())
    monkeypatch.setattr(mt, "_execute_deep_recall_tool", _tool)

    async def main():
        seen["loop"] = threading.get_ident()
        return await mt._deep_recall_async("q", "")

    assert asyncio.run(main()) == "done"
    assert seen["thread"] != seen["loop"]


def test_watchdog_names_a_busy_thread():
    done = threading.Event()

    def _burn_cpu_for_test():
        while not done.is_set():
            sum(range(1000))

    th = threading.Thread(target=_burn_cpu_for_test, name="burner", daemon=True)
    th.start()
    try:
        time.sleep(0.05)
        text = loop_watchdog._busy_threads({threading.get_ident()})
    finally:
        done.set()
        th.join()
    assert "burner" in text
    assert "_burn_cpu_for_test" in text


def test_fallback_report_runs_off_the_event_loop(monkeypatch):
    from core.scout import runner

    seen = {}

    def _fallback(*a, **k):
        seen["thread"] = threading.get_ident()
        return "report"

    monkeypatch.setattr(runner, "_build_fallback_report", _fallback)
    monkeypatch.setattr(runner, "should_bypass_scout", lambda *a: True)

    class _Brief:
        turn_count = 0

    async def main():
        seen["loop"] = threading.get_ident()
        return await runner.run_scout("hi", "sid", session_brief=_Brief())

    assert asyncio.run(main()) == "report"
    assert seen["thread"] != seen["loop"]
