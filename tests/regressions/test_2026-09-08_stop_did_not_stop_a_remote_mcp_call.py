"""Stop did not stop a remote MCP call.

The MCP bridge runs on an executor thread. It hands `conn.call_tool()` to the
event loop with run_coroutine_threadsafe and blocks on the returned
concurrent.futures.Future — and nothing the dispatcher holds could reach that
future. `gate.cancel()` settles the queue-to-running boundary, `fut.cancel()`
cancels the pool wrapper, `_kill_tool_subprocess` kills children an MCP call
never has. The coroutine itself ran on regardless, and the bridge cancelled it
only on its own timeout.

Two consequences, both reproduced before the fix. A stop that landed while the
call was inside `ensure_ready()` — waking an idle stdio child or a degraded
server, which is exactly when a call is slow enough for someone to press stop —
transmitted the remote request the moment readiness arrived: `transmitted so
far = []` at the cancel, `[('delete_everything', {'target': 'prod'})]` once
readiness was released. And the pool thread stayed parked for
`mcp_call_timeout + 10` — 70s at the shipped default — before anyone even
looked at cancelling the coroutine.

Then the swallow: the bridge caught the future's CancelledError and returned
`"Error: MCP call ... was cancelled."`, a perfectly ordinary tool result. The
round carried on with a cancellation rendered as a string the model could read
past.

The dispatch now owns an AsyncOpScope. The bridge registers its future there,
so cancelling the dispatch cancels the coroutine and wakes the thread at once;
`conn.call_tool()` re-reads the flag on the loop either side of
`ensure_ready()`, which is the only place "nothing was sent" can be stated as a
fact; and a cancelled call raises DispatchCancelled instead of returning a
sentence about itself. A call cancelled after dispatch does not pretend to have
prevented anything — transport cancellation says nothing about what the remote
already did — and says so in the log and in the error it raises.
"""

from __future__ import annotations

import asyncio
import concurrent.futures as _futures
import logging
import threading

import pytest
from mcp import types as mcp_types

from core.extensions.mcp.bridge import call_mcp_tool_sync, make_tool_fn
from core.extensions.mcp.config import MCPServerConfig
from core.extensions.mcp.manager import MCPCallCancelled, MCPConnection
from core.tools import executor as tool_executor
from core.tools.executor import AsyncOpScope, _execute_single, execute_tool_round
from core.tools.registry import ToolRegistry

# The remote action the user is trying to stop. Named for what is at stake:
# every other tool in Pernix has its side effects on this machine.
REMOTE_TOOL = "delete_everything"
REMOTE_ARGS = {"target": "prod"}


class FakeSession:
    """The live ClientSession an MCPConnection holds, with the wire replaced.

    `transmitted` is the whole point of this file: an entry here means a
    request left Pernix for someone else's machine.
    """

    def __init__(self) -> None:
        self.transmitted: list[tuple[str, dict]] = []
        self.dispatched = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled_in_flight = False

    async def call_tool(self, name, arguments, **kwargs):
        self.transmitted.append((name, dict(arguments)))
        self.dispatched.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled_in_flight = True
            raise
        return mcp_types.CallToolResult(content=[mcp_types.TextContent(type="text", text="done")])


class Server:
    """One connection whose readiness and remote call are both scriptable."""

    def __init__(self, *, name: str = "srv") -> None:
        self.cfg = MCPServerConfig(name=name, transport="http", url="http://stub.invalid/mcp")
        self.conn = MCPConnection(self.cfg, manager=self)  # type: ignore[arg-type]
        self.session = FakeSession()
        self.connections = {name: self.conn}
        self.started = True
        self._loop: asyncio.AbstractEventLoop | None = None
        # Readiness barrier. ensure_ready() is where a call waits out an idle
        # stdio respawn or a degraded server's backoff.
        self.at_readiness = asyncio.Event()
        self.readiness_released = asyncio.Event()
        self.conn.ensure_ready = self._ensure_ready  # type: ignore[method-assign]

    async def _ensure_ready(self):
        self.at_readiness.set()
        await self.readiness_released.wait()
        self.conn.status = "ready"
        self.conn._session = self.session
        return self.session

    def ready_now(self) -> None:
        """Skip the readiness barrier: the server is already up."""
        self.readiness_released.set()

    def registry(self) -> ToolRegistry:
        reg = ToolRegistry()
        reg.register(
            name=f"mcp_{self.cfg.name}_{REMOTE_TOOL}",
            func=make_tool_fn(self, self.cfg.name, REMOTE_TOOL),
            description="stub",
            parameters={"type": "object", "properties": {}},
            category=f"mcp:{self.cfg.name}",
            source="mcp",
            parallel_safe=True,
            # The real registration: the executor's wait is the last resort,
            # not a race with the bridge's own.
            timeout=self.conn.call_timeout + 15,
        )
        return reg

    @property
    def tool_name(self) -> str:
        return f"mcp_{self.cfg.name}_{REMOTE_TOOL}"


@pytest.fixture
def mcp_on(monkeypatch):
    monkeypatch.setattr("config.settings.mcp_enabled", True)


@pytest.fixture
def one_thread_pool(monkeypatch):
    """A single tool thread, so "the thread came back" is observable."""
    pool = _futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="pernix-tool")
    monkeypatch.setattr(tool_executor, "_get_tool_executor", lambda: pool)
    yield pool
    pool.shutdown(wait=False)


async def _cancel(task) -> BaseException:
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    return caught.value


async def _settle() -> None:
    """Let every callback the cancel scheduled actually run."""
    for _ in range(20):
        await asyncio.sleep(0)


class WarningWatch(logging.Handler):
    """Wait for one warning by content.

    The bridge logs from the tool thread, so "did it warn?" is not settled by
    the time the dispatcher's cancel returns — an assertion on captured records
    is a race under a loaded suite. This one waits for the record itself.
    """

    def __init__(self, needle: str) -> None:
        super().__init__(level=logging.WARNING)
        self.needle = needle
        self.seen = threading.Event()

    def emit(self, record: logging.LogRecord) -> None:
        if self.needle in record.getMessage():
            self.seen.set()

    def __enter__(self) -> "WarningWatch":
        logging.getLogger("pernix.ext.mcp").addHandler(self)
        return self

    def __exit__(self, *exc) -> None:
        logging.getLogger("pernix.ext.mcp").removeHandler(self)


# --- the transmission that outlived the stop -------------------------------


async def test_a_cancel_during_readiness_never_reaches_the_server(mcp_on, one_thread_pool):
    """The filed repro. Stop lands while the call waits for the server to wake;
    releasing readiness afterwards must not pay the request out."""
    srv = Server()
    reg = srv.registry()

    task = asyncio.create_task(_execute_single(srv.tool_name, REMOTE_ARGS, {}, reg))
    await srv.at_readiness.wait()
    assert srv.session.transmitted == [], "nothing should be sent before the server is even ready"

    await _cancel(task)
    assert srv.session.transmitted == []

    srv.readiness_released.set()
    await _settle()
    assert srv.session.transmitted == [], (
        "the remote call was transmitted after the user pressed stop — the cancel "
        "reached the executor's wrapper future, never the coroutine behind it"
    )


async def test_the_loop_side_check_is_what_makes_that_a_guarantee(mcp_on):
    """Under the bridge: call_tool re-reads the stop flag on the loop, either
    side of ensure_ready(), and refuses before writing anything."""
    srv = Server()
    stopped = False

    async def _call():
        return await srv.conn.call_tool(REMOTE_TOOL, REMOTE_ARGS, cancel_check=lambda: stopped)

    task = asyncio.create_task(_call())
    await srv.at_readiness.wait()
    stopped = True  # the stop lands while readiness is still pending
    srv.readiness_released.set()

    with pytest.raises(MCPCallCancelled) as caught:
        # Bounded: a call that dispatched anyway would block here forever.
        await asyncio.wait_for(task, timeout=5)
    assert "nothing was sent" in str(caught.value)
    assert srv.session.transmitted == []
    assert srv.conn._inflight == 0, "a call that never dispatched must not be counted in flight"


async def test_a_stop_that_predates_the_bridge_sends_nothing_either(mcp_on):
    """The other window: the pool thread claimed the gate, then the stop landed
    before it reached the bridge. Nothing has been handed to the loop yet, so
    "no request was sent" is a fact here too — and it is not reported as a tool
    result the round can read past."""
    srv = Server()
    srv.ready_now()
    scope = AsyncOpScope()
    scope.cancel()
    ctx = {"_loop": asyncio.get_running_loop(), "_async_ops": scope, "_cancel_event": scope.event}

    def _call():
        return call_mcp_tool_sync(srv, srv.cfg.name, REMOTE_TOOL, REMOTE_ARGS, ctx)

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(asyncio.to_thread(_call), timeout=5)
    await _settle()
    assert srv.session.transmitted == []


# --- the registration-versus-cancellation race -----------------------------


async def test_a_stop_landing_between_the_check_and_the_submit_still_stops(mcp_on, one_thread_pool, monkeypatch):
    """The narrowest window there is: the bridge checked, found the dispatch
    live, submitted the coroutine — and the stop landed before its future was
    registered. Registration must lose that race, not the user."""
    srv = Server()
    reg = srv.registry()
    real_register = AsyncOpScope.register

    def _stop_first(self, fut):
        # Exactly the interleaving the window allows, made deterministic.
        self.cancel()
        return real_register(self, fut)

    monkeypatch.setattr(AsyncOpScope, "register", _stop_first)

    with pytest.raises(asyncio.CancelledError):
        await _execute_single(srv.tool_name, REMOTE_ARGS, {}, reg)

    # The coroutine was parked in ensure_ready when its owner went away.
    # Releasing readiness afterwards must not resurrect the call.
    srv.readiness_released.set()
    await _settle()
    assert srv.session.transmitted == [], "an operation registered after its owner was cancelled ran anyway"


def test_a_scope_that_is_already_cancelled_cancels_on_registration():
    scope = AsyncOpScope()
    scope.cancel()
    fut: _futures.Future = _futures.Future()

    assert scope.register(fut) is False
    assert fut.cancelled(), "the caller is on a pool thread — the scope has to settle it here"


def test_a_finished_operation_is_not_disturbed_by_a_later_cancel():
    """The completion/cancellation race from the other side: the result arrived
    first, and cancelling the scope must not rewrite it."""
    scope = AsyncOpScope()
    fut: _futures.Future = _futures.Future()
    assert scope.register(fut) is True
    fut.set_running_or_notify_cancel()
    fut.set_result("already back")

    scope.cancel()
    assert fut.result() == "already back"


def test_unregistering_keeps_a_completed_call_out_of_the_next_cancel():
    scope = AsyncOpScope()
    fut: _futures.Future = _futures.Future()
    scope.register(fut)
    scope.unregister(fut)

    scope.cancel()
    assert not fut.cancelled(), "a call that already returned is not the cancel's business"
    assert scope.cancelled is True


# --- releasing the thread, and telling the truth about what was sent -------


async def test_the_pool_thread_comes_back_immediately_instead_of_in_70_seconds(mcp_on, one_thread_pool):
    """The bridge blocks for mcp_call_timeout + 10. Before the fix a cancelled
    call held its tool-pool thread for all of it; the scope cancels the future,
    which wakes the waiter at once."""
    srv = Server()
    reg = srv.registry()
    assert srv.conn.call_timeout >= 60, "the default is what made this expensive"
    returned = threading.Event()
    inner = reg.get(srv.tool_name).function

    def _watched(_context=None, **arguments):
        # execute_sync injects _context by name, so the wrapper has to declare it.
        try:
            return inner(_context=_context, **arguments)
        finally:
            returned.set()

    reg.get(srv.tool_name).function = _watched

    task = asyncio.create_task(_execute_single(srv.tool_name, REMOTE_ARGS, {}, reg))
    await srv.at_readiness.wait()
    await _cancel(task)

    assert await asyncio.to_thread(returned.wait, 5), (
        "the tool thread was still parked on the bridge's own timeout — a stop has "
        "to reach the future the thread is blocked on"
    )


async def test_a_cancel_after_dispatch_admits_it_may_already_have_been_sent(mcp_on, one_thread_pool):
    """The honest case. The request is on the wire; cancelling the transport
    cannot roll back what the server already did, so nothing may claim it did."""
    srv = Server()
    srv.ready_now()
    reg = srv.registry()

    with WarningWatch("cannot be ruled out") as watch:
        task = asyncio.create_task(_execute_single(srv.tool_name, REMOTE_ARGS, {}, reg))
        await srv.session.dispatched.wait()  # transmitted, no answer yet
        await _cancel(task)
        await _settle()
        warned = await asyncio.to_thread(watch.seen.wait, 5)

    assert srv.session.transmitted == [(REMOTE_TOOL, REMOTE_ARGS)]
    assert srv.session.cancelled_in_flight, "the in-flight coroutine must actually be cancelled"
    assert warned, (
        "an already-transmitted call that was cancelled is an uncertain outcome and " "the operator needs to be told so"
    )


async def test_the_uncertain_case_says_so_in_what_it_raises(mcp_on):
    """The same honesty in the exception, not only the log. Read at the bridge
    itself, where the outer dispatch's own CancelledError cannot mask it."""
    srv = Server()
    srv.ready_now()
    scope = AsyncOpScope()
    ctx = {"_loop": asyncio.get_running_loop(), "_async_ops": scope, "_cancel_event": scope.event}

    async def _stop_once_it_is_on_the_wire():
        await srv.session.dispatched.wait()
        # Wait for the bridge thread to hand its future to the scope, so the
        # stop lands where it lands in production: on a thread already blocked
        # on an answer that is never coming. Bounded, so a fix that never
        # registers fails the assertion instead of wedging the suite.
        for _ in range(10_000):
            if scope._futures:
                break
            await asyncio.sleep(0)
        scope.cancel()

    asyncio.get_running_loop().create_task(_stop_once_it_is_on_the_wire())
    with pytest.raises(asyncio.CancelledError) as caught:
        await asyncio.wait_for(
            asyncio.to_thread(call_mcp_tool_sync, srv, srv.cfg.name, REMOTE_TOOL, REMOTE_ARGS, ctx), timeout=5
        )

    assert "may already have been transmitted" in str(caught.value), (
        "cancelling a transport says nothing about what the remote already did — "
        "the uncertain case must not be dressed up as a prevented one"
    )


async def test_an_interrupted_call_still_lets_the_idle_reaper_do_its_job(mcp_on, one_thread_pool):
    """_inflight gates suspend(). An orphaned post-stop call left it at 1, so an
    idle stdio child sat there past its reap window with nobody waiting on it."""
    srv = Server()
    srv.ready_now()
    srv.cfg.transport = "stdio"
    reg = srv.registry()

    task = asyncio.create_task(_execute_single(srv.tool_name, REMOTE_ARGS, {}, reg))
    await srv.session.dispatched.wait()
    assert srv.conn._inflight == 1
    await _cancel(task)
    await _settle()

    assert srv.conn._inflight == 0
    srv.conn.status = "ready"
    srv.conn.suspend()
    assert srv.conn._suspend_requested is True, "the child can be reaped again"


# --- the swallow ------------------------------------------------------------


async def test_a_cancelled_call_unwinds_the_round_instead_of_describing_itself(mcp_on, one_thread_pool):
    """The bridge used to return "Error: MCP call ... was cancelled." — a normal
    tool result. The round read past it and dispatched the next call."""
    srv = Server()
    srv.ready_now()
    reg = srv.registry()

    calls = [{"name": srv.tool_name, "arguments": REMOTE_ARGS}]
    task = asyncio.create_task(execute_tool_round(calls, {}, reg))
    await srv.session.dispatched.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_another_dispatch_is_untouched_by_the_cancel(mcp_on):
    """Cancellation is per dispatch. One session stopping must not disturb a
    concurrent call over the same shared connection — and must never take the
    connection itself down to stop one call."""
    srv = Server()
    srv.ready_now()
    reg = srv.registry()

    doomed = asyncio.create_task(_execute_single(srv.tool_name, {"target": "doomed"}, {}, reg))
    await srv.session.dispatched.wait()
    srv.session.dispatched.clear()
    survivor = asyncio.create_task(_execute_single(srv.tool_name, {"target": "survivor"}, {}, reg))
    await srv.session.dispatched.wait()

    await _cancel(doomed)
    srv.session.release.set()
    result = await survivor

    assert result.content == "done" and result.was_error is False
    assert srv.conn.status != "stopped", "one call's cancel must not close the shared connection"
    assert srv.conn._session is srv.session


# --- the pattern, for the five other bridges that share it -----------------


def test_the_scope_travels_with_every_dispatch(mcp_on):
    """AsyncOpScope is reachable from any tool's _context, not just MCP's:
    memory_tools, rlm, evaluation, model_mgmt and dream/probe all block on a
    run_coroutine_threadsafe future the same way and can adopt it unchanged."""
    import inspect

    src = inspect.getsource(tool_executor._execute_single)
    assert 'ctx["_async_ops"] = scope' in src
    assert 'ctx["_cancel_event"] = cancel_event' in src, "the older cooperative flag stays where tools expect it"

    scope = AsyncOpScope()
    ctx = {"_async_ops": scope, "_cancel_event": scope.event}
    assert tool_executor.dispatch_cancelled(ctx) is False
    scope.cancel()
    assert tool_executor.dispatch_cancelled(ctx) is True
    assert scope.event.is_set(), "one object answers for both kinds of tool"
    assert tool_executor.dispatch_cancelled(None) is False
