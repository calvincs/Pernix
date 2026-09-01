"""Cancel did not stop a round of sequential tools.

`_execute_single` caught CancelledError and returned an error result, so
the cancel was consumed and the sequential loop dispatched the next tool;
a round of [bash, file_write, bash] cancelled during the first bash still
ran the write and the second bash to completion. The cancel now kills the
in-flight child and unwinds the round, and a cancel flag set between two
sequential calls stops the round before the next call starts.
"""

import asyncio
import threading

import pytest

from core.tools.executor import execute_tool_round
from core.tools.registry import ToolRegistry
from sessions.manager import SessionManager


@pytest.fixture
def mgr(monkeypatch):
    fresh = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", fresh)
    return fresh


def _sequential_registry(tools: dict) -> ToolRegistry:
    reg = ToolRegistry()
    for name, func in tools.items():
        reg.register(
            name=name,
            func=func,
            description=name,
            parameters={"type": "object", "properties": {}},
            parallel_safe=False,
            timeout=5,
        )
    return reg


async def test_cancel_mid_tool_unwinds_the_round_instead_of_running_the_next(mgr):
    started = threading.Event()
    release = threading.Event()
    ran: list[str] = []

    def slow():
        ran.append("slow")
        started.set()
        release.wait(5)
        return "done"

    def after():
        ran.append("after")
        return "ran anyway"

    reg = _sequential_registry({"slow": slow, "after": after})
    sid = mgr.create_session(title="cancel")
    task = asyncio.create_task(
        execute_tool_round(
            [{"name": "slow", "arguments": {}}, {"name": "after", "arguments": {}}],
            {"session_id": sid},
            reg,
        )
    )
    await asyncio.to_thread(started.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    assert ran == ["slow"], "the tool after the cancelled one must not run"


async def test_cancel_flag_between_sequential_calls_stops_the_round(mgr):
    ran: list[str] = []
    sid = mgr.create_session(title="cancel-flag")
    session = mgr.get(sid)

    def first():
        ran.append("first")
        session.cancel_requested = True  # the user pressed stop while this ran
        return "ok"

    def second():
        ran.append("second")
        return "ok"

    reg = _sequential_registry({"first": first, "second": second})
    with pytest.raises(asyncio.CancelledError):
        await execute_tool_round(
            [{"name": "first", "arguments": {}}, {"name": "second", "arguments": {}}],
            {"session_id": sid},
            reg,
        )
    assert ran == ["first"]
