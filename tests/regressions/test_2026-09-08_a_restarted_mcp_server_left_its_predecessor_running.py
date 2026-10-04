"""A restarted MCP server left its predecessor running.

`shutdown()` published `started = False`, snapshotted the connections, awaited
their closes, and only then cleared the dict. `start()` set `started = True`
and `_spawn`ed straight into that same dict. Neither took a lock — the
per-server locks serialize CRUD on one server and say nothing about a global
transition — so toggling mcp_enabled off and back on in Settings interleaved
them: the enable saw a stopped manager, spawned its replacements, and the old
shutdown's `connections.clear()` wiped them out of the dict on its way past.

The reproduction ended exactly as filed: `started=True, tracked=[],
live_supervisors=1`. `remove_server('srv')` returned False. A tool call found
`connections.get('srv') is None` with `started=True` and told the model the
server was "no longer configured; this tool is stale" — while the stdio child
it was talking about was still running, unowned and unreachable.

Three doors onto the same defect, all verified:

* the shipped shutdown path orphans with no concurrency at all —
  `api/app.py` bounds it with `wait_for(..., timeout=8)`, the cancellation
  lands on the gather, and `connections.clear()` never runs;
* `reload_server` reaches it from the other side, holding only its own
  server's lock while a shutdown empties the dict under it;
* `start()` was never idempotent, so a second call orphaned the first
  generation on its own.

Two mechanisms now, deliberately not one lock. `_lifecycle` serializes
start against shutdown. `_generation` gives every connection an owner: a CRUD
call captures the generation before it awaits a close and passes it to
`_spawn`, which refuses outright if a global transition has since retired it.
Neither global transition ever waits on a per-server lock, so the order cannot
deadlock. And ownership is released only after teardown is guaranteed —
shutdown untracks in a `finally`, force-closing anything the gather did not
finish.
"""

from __future__ import annotations

import asyncio
import inspect
import time

import pytest

from core.extensions.mcp.config import MCPServerConfig
from core.extensions.mcp.manager import MCPConnection, MCPManager, MCPUnavailable


def _cfg(name: str) -> MCPServerConfig:
    return MCPServerConfig(name=name, transport="stdio", command="/bin/true")


class Fleet:
    """Every supervisor the test ever started, with a scriptable teardown.

    Stands in for the real `_supervise` loop: same shape (ready, hold the
    stack open until a drop is requested, exit when closing), no transport.
    `hold_names` parks a connection on its way out, which is where a shutdown
    is when a concurrent enable slips past it.
    """

    def __init__(self, monkeypatch) -> None:
        self.started: list[MCPConnection] = []
        self.hold_names: set[str] = set()
        self.hold = asyncio.Event()
        self.holding = asyncio.Event()
        monkeypatch.setattr(MCPConnection, "_supervise", self._supervise())

    def _supervise(self):
        fleet = self

        async def _run(conn: MCPConnection) -> None:
            fleet.started.append(conn)
            conn.status = "ready"
            conn._session = object()
            conn.connected_at = time.time()
            conn._resolve_waiters()
            try:
                while not conn._closing:
                    await conn._wait_event("_drop_evt", None)
                if conn.cfg.name in fleet.hold_names:
                    fleet.holding.set()
                    await fleet.hold.wait()
            finally:
                conn.status = "stopped"

        return _run

    @property
    def live(self) -> list[MCPConnection]:
        return [c for c in self.started if c._task is not None and not c._task.done()]


@pytest.fixture
def fleet(monkeypatch):
    return Fleet(monkeypatch)


@pytest.fixture
def one_server(monkeypatch):
    """A single configured server, and a record of every persist."""
    saved: list[dict] = []
    monkeypatch.setattr("core.extensions.mcp.manager.load_server_configs", lambda: {"srv": _cfg("srv")})
    monkeypatch.setattr("core.extensions.mcp.manager.save_server_configs", lambda cfgs: saved.append(dict(cfgs)))
    monkeypatch.setattr("config.settings.mcp_enabled", True)
    monkeypatch.setattr("config.settings.mcp_stdio_enabled", True)
    return saved


async def _settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


# --- the filed race ---------------------------------------------------------


async def test_an_enable_during_shutdown_leaves_no_orphan(fleet, one_server):
    """Block the old connection's close, enable concurrently, release it. The
    final enabled state must hold the intended live connection and nothing
    else."""
    mgr = MCPManager()
    await mgr.start()
    first = mgr.connections["srv"]
    fleet.hold_names.add("srv")

    stop = asyncio.create_task(mgr.shutdown())
    await fleet.holding.wait()  # shutdown is inside the gather
    enable = asyncio.create_task(mgr.start())
    await _settle()

    assert mgr.connections.get("srv") is first, "the enable must not spawn into a dict being emptied"

    fleet.hold.set()
    await stop
    await enable
    await _settle()  # let the replacement's supervisor actually reach its first line

    second = mgr.connections.get("srv")
    assert mgr.started is True
    assert set(mgr.connections) == {"srv"}
    assert second is not None and second is not first, "the enable did not get its own connection"
    assert fleet.live == [second], f"orphaned supervisors left running: {[c.cfg.name for c in fleet.live]}"
    assert first._closed is True and first._task.done()


async def test_the_stale_tool_error_was_a_lie_about_a_live_child(fleet, one_server):
    """The shape the agent saw: started, nothing tracked, and a running stdio
    child the bridge described as "no longer configured"."""
    mgr = MCPManager()
    await mgr.start()
    fleet.hold_names.add("srv")

    stop = asyncio.create_task(mgr.shutdown())
    await fleet.holding.wait()
    enable = asyncio.create_task(mgr.start())
    await _settle()
    fleet.hold.set()
    await stop
    await enable
    await _settle()

    assert not (mgr.started and mgr.connections.get("srv") is None), (
        "started with nothing tracked is the state that made the bridge call a " "live server stale"
    )
    assert await mgr.remove_server("srv") is True, "a server that is running must be removable"
    await _settle()
    assert fleet.live == []


# --- the door that needs no concurrency at all ------------------------------


async def test_a_cancelled_shutdown_still_lets_go_of_its_connections(fleet, one_server):
    """api/app.py bounds shutdown with wait_for(..., timeout=8). That
    cancellation landed on the gather, so the untracking never ran and the
    manager was left stopped with half-closed connections still tracked."""
    mgr = MCPManager()
    await mgr.start()
    first = mgr.connections["srv"]
    fleet.hold_names.add("srv")

    stop = asyncio.create_task(mgr.shutdown())
    await fleet.holding.wait()
    stop.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stop
    await _settle()

    assert mgr.connections == {}, "a cancelled shutdown still owns what it could not close"
    assert mgr.started is False
    assert first._closed is True
    assert first._task.done(), "the supervisor must not outlive the manager that spawned it"


async def test_the_start_after_a_cancelled_shutdown_is_clean(fleet, one_server):
    mgr = MCPManager()
    await mgr.start()
    first = mgr.connections["srv"]
    fleet.hold_names.add("srv")

    stop = asyncio.create_task(mgr.shutdown())
    await fleet.holding.wait()
    stop.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stop
    await _settle()

    fleet.hold_names.clear()
    fleet.hold = asyncio.Event()
    await mgr.start()
    await _settle()

    second = mgr.connections["srv"]
    assert second is not first
    assert fleet.live == [second], "the next start() spawned straight over a connection nobody had closed"


# --- the same orphan, reached from reload -----------------------------------


async def test_a_reload_cannot_spawn_into_a_dict_a_shutdown_has_emptied(fleet, one_server):
    """reload_server holds only its own server's lock, so guarding start
    against shutdown leaves this open. The generation covers _spawn itself."""
    mgr = MCPManager()
    await mgr.start()
    fleet.hold_names.add("srv")

    reload = asyncio.create_task(mgr.reload_server("srv"))
    await fleet.holding.wait()  # parked in the old connection's close()
    stop = asyncio.create_task(mgr.shutdown())
    await _settle()
    fleet.hold.set()
    await stop
    with pytest.raises(MCPUnavailable):
        await reload
    await _settle()

    assert mgr.connections == {}
    assert fleet.live == [], "the reload spawned a replacement nothing owns"


async def test_a_shutdown_mid_remove_does_not_erase_the_server_file(fleet, one_server):
    """A CRUD call that resumes after a shutdown finds the dict empty. Left to
    persist that, a concurrent disable would rewrite mcp_servers.json with no
    servers in it at all."""
    saved = one_server
    mgr = MCPManager()
    await mgr.start()
    fleet.hold_names.add("srv")

    removal = asyncio.create_task(mgr.remove_server("srv"))
    await fleet.holding.wait()
    stop = asyncio.create_task(mgr.shutdown())
    await _settle()
    fleet.hold.set()
    await stop
    with pytest.raises(MCPUnavailable):
        await removal

    assert saved == [], "the user's configured servers were persisted away"


# --- ownership, idempotency, partial failure --------------------------------


async def test_a_second_start_does_not_spawn_over_the_first(fleet, one_server):
    """No concurrency required: start() re-read the file and spawned
    unconditionally, so the first generation's supervisors were simply
    replaced in the dict and left running."""
    mgr = MCPManager()
    await mgr.start()
    await _settle()
    first = mgr.connections["srv"]

    await mgr.start()
    await _settle()

    assert mgr.connections["srv"] is first
    assert len(fleet.started) == 1, "a duplicate start() spawned a second supervisor"


def test_a_spawn_from_a_retired_generation_is_refused(fleet, one_server):
    """The ownership rule on its own: a connection may only be tracked under
    the generation it was spawned for."""
    mgr = MCPManager()
    gen = mgr._generation
    mgr._generation += 1  # a global transition ran while the caller awaited

    with pytest.raises(MCPUnavailable):
        mgr._spawn(_cfg("srv"), generation=gen)
    assert mgr.connections == {}, "nothing may be tracked, and nothing started, under a retired generation"


async def test_one_unspawnable_server_does_not_take_the_others_with_it(fleet, one_server, monkeypatch):
    """Partial startup failure: the rest still come up, and the failure leaves
    no half-tracked entry with no supervisor behind it."""
    monkeypatch.setattr(
        "core.extensions.mcp.manager.load_server_configs",
        lambda: {"good": _cfg("good"), "bad": _cfg("bad")},
    )
    real_start = MCPConnection.start

    def _start(self):
        if self.cfg.name == "bad":
            raise RuntimeError("no such command")
        return real_start(self)

    monkeypatch.setattr(MCPConnection, "start", _start)

    mgr = MCPManager()
    await mgr.start()

    assert set(mgr.connections) == {"good"}
    assert mgr.started is True
    await mgr.shutdown()


async def test_abandon_is_the_teardown_a_caller_could_not_await(fleet, one_server):
    mgr = MCPManager()
    await mgr.start()
    conn = mgr.connections["srv"]

    conn.abandon()
    await _settle()
    assert conn._closed is True and conn.status == "stopped"
    assert conn._task.done()

    conn.abandon()  # idempotent: a closed connection is not re-torn-down
    await mgr.shutdown()
    assert mgr.connections == {}


# --- the lock order ---------------------------------------------------------


def test_the_global_transitions_never_wait_on_a_per_server_lock():
    """The whole deadlock argument, pinned. A global transition may hold
    _lifecycle across awaits; it must never need a lock a slow CRUD call is
    holding, and no CRUD call may reach for _lifecycle from underneath one."""
    for name in ("start", "shutdown"):
        src = inspect.getsource(getattr(MCPManager, name))
        assert "self._lifecycle" in src, f"{name} does not serialize against the other global transition"
        assert "_lock_for(" not in src, f"{name} would wait on a per-server lock while holding _lifecycle"
    for name in ("add_server", "remove_server", "toggle_server", "reload_server"):
        src = inspect.getsource(getattr(MCPManager, name))
        assert "self._lifecycle" not in src, f"{name} would invert the lock order"


def test_shutdown_releases_ownership_only_after_teardown_is_guaranteed():
    src = inspect.getsource(MCPManager.shutdown)
    assert "finally:" in src, "a cancelled gather must still untrack what it owned"
    assert "abandon()" in src, "anything the gather did not finish has to be force-closed"
    assert "self.connections.clear()" not in src, "clear() would drop connections this call does not own"
