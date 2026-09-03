"""Concurrent server mutations left an orphan supervisor.

add/remove/toggle/reload all `await close()` and then spawn. Two callers
interleaving at that await — the UI saving a server while the agent runs
mcp_reload_server on it — each closed the same old connection and spawned
a new one, but only the last landed in self.connections. The other kept a
live supervisor task (and, for stdio, a child process) that nothing could
reach or shut down.

Each server name now has its own lock, so the mutators serialize per
server while staying concurrent across servers.
"""

import asyncio
import inspect

from core.extensions.mcp.manager import MCPManager


def test_each_mutator_takes_the_lock():
    for name in ("add_server", "remove_server", "toggle_server", "reload_server"):
        src = inspect.getsource(getattr(MCPManager, name))
        assert "self._lock_for(" in src, f"{name} can still interleave with another mutator"


def test_the_lock_is_per_server_name():
    mgr = MCPManager()
    a1 = mgr._lock_for("alpha")
    a2 = mgr._lock_for("alpha")
    b = mgr._lock_for("beta")
    assert a1 is a2, "the same server must reuse its lock"
    assert a1 is not b, "different servers must not block each other"


async def test_two_mutations_on_one_server_do_not_interleave():
    mgr = MCPManager()
    order = []

    async def guarded(tag):
        async with mgr._lock_for("srv"):
            order.append(f"{tag}:enter")
            await asyncio.sleep(0)  # the await the real code has at close()
            order.append(f"{tag}:exit")

    await asyncio.gather(guarded("first"), guarded("second"))
    assert order in (
        ["first:enter", "first:exit", "second:enter", "second:exit"],
        ["second:enter", "second:exit", "first:enter", "first:exit"],
    ), f"interleaved: {order}"


async def test_different_servers_still_run_concurrently():
    mgr = MCPManager()
    both_inside = asyncio.Event()
    seen = []

    async def hold(name):
        async with mgr._lock_for(name):
            seen.append(name)
            if len(seen) == 2:
                both_inside.set()
            await asyncio.wait_for(both_inside.wait(), timeout=1)

    await asyncio.gather(hold("alpha"), hold("beta"))
    assert both_inside.is_set()


def test_the_transport_open_is_bounded():
    src = inspect.getsource(MCPManager) + inspect.getsource(
        __import__("core.extensions.mcp.manager", fromlist=["MCPConnection"]).MCPConnection
    )
    assert (
        "self._enter_transport(stack), timeout=settings.mcp_connect_timeout" in src
    ), "the legacy SSE transport waits on the SDK's 300s read timeout otherwise"
