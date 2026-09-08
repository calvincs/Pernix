"""Regression — 2026-09-08, harness audit 3.2.1 H17.

http_get carries a 60-second whole-exchange deadline and registered a
15-second tool timeout. The executor adds a 5-second dispatch grace, so
dispatch gave up at 20s on a fetch its own code had authorised to run for 60.
Measured against a local server dripping for 25 seconds: the executor returned
at 20.0s with "Error: Tool 'http_get' timed out after 20s", and the pool thread
finished the fetch successfully at 25.1s — a complete body, acquired, and
thrown away because nobody was listening for it any more.

Every other tool mirrors its inner bound outward. bash writes its clamp into
`max_timeout`; grep registers 30 against a 30-second subprocess timeout;
browse_web registers 60 against an inner 40. http_get was the only inverted
pair in the registry, and inverted is the direction that loses work.

The second half is worse than the arithmetic. `_kill_tool_subprocess` is what
the executor reaches for on timeout, and a pure-Python fetch has no
subprocess — so cancelling the dispatch cancelled nothing at all. The thread
stayed inside httpx, holding a tool-executor slot and an open socket, while the
model was told the call had ended. A retry then opened a SECOND fetch of the
same URL alongside the first.

Pinned here: registration is derived from the deadline rather than typed
beside it, every operation is bounded by what remains of the total allowance,
and a cancelled or timed-out dispatch sets a cooperative flag the fetch loop
actually checks — so the thread unwinds in bounded time and no second
acquisition starts while the first is still running.
"""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import core.extensions.web as web
from core.tools import executor as tool_executor
from core.tools.executor import _resolve_timeout, execute_tool_round
from core.tools.registry import ToolRegistry

MARKER = "SLOW_BODY_OK"


class _State:
    def __init__(self):
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.requests = 0
        self.aborted = 0


STATE = _State()
DRIP_SECONDS = 22.0
REDIRECT_HOPS = 3


class _Slow(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _enter(self):
        with STATE.lock:
            STATE.active += 1
            STATE.requests += 1
            STATE.max_active = max(STATE.max_active, STATE.active)

    def _leave(self, aborted: bool):
        with STATE.lock:
            STATE.active -= 1
            if aborted:
                STATE.aborted += 1

    def do_GET(self):
        self._enter()
        aborted = True
        try:
            if self.path.startswith("/hop"):
                # A slow redirect chain: each hop costs real time.
                n = int(self.path[4:] or 0)
                time.sleep(0.4)
                target = f"/hop{n + 1}" if n + 1 < REDIRECT_HOPS else "/final"
                self.send_response(302)
                self.send_header("Location", target)
                self.send_header("Content-Length", "0")
                self.end_headers()
                aborted = False
                return
            if self.path == "/final":
                body = MARKER.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                aborted = False
                return
            # /drip — continuous small chunks for DRIP_SECONDS, then the marker.
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            deadline = time.monotonic() + DRIP_SECONDS
            while time.monotonic() < deadline:
                self.wfile.write(b"1\r\n.\r\n")
                self.wfile.flush()
                time.sleep(0.1)
            tail = MARKER.encode()
            self.wfile.write(b"%x\r\n" % len(tail) + tail + b"\r\n0\r\n\r\n")
            self.wfile.flush()
            aborted = False
        except OSError:
            pass  # client hung up — that is the datum
        finally:
            self._leave(aborted)

    def log_message(self, *a):
        pass


@pytest.fixture
def server(monkeypatch):
    global STATE
    STATE = _State()
    monkeypatch.setattr("config.settings.network_enabled", False, raising=False)
    monkeypatch.setattr("config.settings.candor_enabled", False, raising=False)
    monkeypatch.setattr("config.settings.fetch_routing_enabled", False, raising=False)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Slow)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def reg():
    r = ToolRegistry()
    web.register(r)
    return r


def _instrument(reg):
    """Watch the pool thread from the outside: when did the tool really stop?"""
    tool = reg.get("http_get")
    inner = {}
    real = tool.function

    def wrapped(url, force=False, _context=None):
        inner["start"] = time.monotonic()
        try:
            return real(url, force=force, _context=_context)
        finally:
            inner["end"] = time.monotonic()

    tool.function = wrapped
    return inner


# ---------------------------------------------------------------------------
# The arithmetic
# ---------------------------------------------------------------------------


def test_the_dispatch_budget_covers_the_fetchs_own_deadline(reg):
    """15 registered against 60 internal. The number the executor enforces has
    to be at least the number the tool is allowed to spend."""
    tool = reg.get("http_get")
    assert (
        tool.timeout >= web._HTTP_GET_DEADLINE_S
    ), f"registration says {tool.timeout}s, the fetch's own deadline is {web._HTTP_GET_DEADLINE_S}s"
    assert (
        _resolve_timeout(tool, {}) > web._HTTP_GET_DEADLINE_S
    ), "the tool's own deadline must fire first so the model gets its diagnostic"


def test_every_operation_is_bounded_by_what_is_left_of_the_total(reg):
    """A per-read timeout larger than the remaining total allowance lets one
    slow read outlive the deadline it is supposed to be inside."""
    assert web._http_op_timeout(remaining=100.0) <= web._HTTP_OP_TIMEOUT_S
    assert web._http_op_timeout(remaining=2.0) == pytest.approx(2.0)
    assert web._http_op_timeout(remaining=0.0) is None
    assert web._http_op_timeout(remaining=-5.0) is None


# ---------------------------------------------------------------------------
# Through the executor, against a real socket
# ---------------------------------------------------------------------------


async def test_a_slow_redirect_chain_completes_through_the_executor(server, reg):
    calls = [{"name": "http_get", "arguments": {"url": f"{server}/hop0", "force": True}}]
    results = await execute_tool_round(calls, None, reg)
    assert not results[0].was_error, results[0].content
    assert MARKER in results[0].content
    assert STATE.requests == REDIRECT_HOPS + 1, "every hop must still be validated separately"


@pytest.mark.slow
async def test_a_dripping_body_outlives_the_old_dispatch_budget(server, reg):
    """The audit's measurement, verbatim: a legitimate fetch that takes longer
    than 20s and less than the fetch's own deadline."""
    inner = _instrument(reg)
    calls = [{"name": "http_get", "arguments": {"url": f"{server}/drip", "force": True}}]
    t0 = time.monotonic()
    results = await execute_tool_round(calls, None, reg)
    elapsed = time.monotonic() - t0

    assert elapsed > 20.0, "precondition: this fetch outlives the old 20s dispatch budget"
    assert not results[0].was_error, results[0].content
    assert MARKER in results[0].content, "the acquired body must reach the caller, not the bin"
    assert inner["end"] - t0 < elapsed + 1.0
    assert STATE.max_active == 1


async def test_a_cancelled_dispatch_stops_the_fetch_that_is_running(server, reg):
    """`_kill_tool_subprocess` is a no-op for a pure-Python fetch, so the
    thread ran on past the reported timeout with the socket still open."""
    inner = _instrument(reg)
    calls = [{"name": "http_get", "arguments": {"url": f"{server}/drip", "force": True}}]
    task = asyncio.ensure_future(execute_tool_round(calls, None, reg))
    await asyncio.sleep(1.5)
    assert "start" in inner, "precondition: the fetch is running"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    deadline = time.monotonic() + 8.0
    while "end" not in inner and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    assert "end" in inner, f"the pool thread never unwound (drip is {DRIP_SECONDS}s)"
    assert inner["end"] - inner["start"] < 8.0, "cleanup latency must be bounded"

    # The socket really closed: the server saw the client go, it did not just
    # finish serving a body nobody read.
    drain = time.monotonic() + 5.0
    while STATE.active and time.monotonic() < drain:
        await asyncio.sleep(0.05)
    assert STATE.active == 0, "the server is still serving a fetch nobody is awaiting"
    assert STATE.aborted >= 1, "the drip should have been abandoned mid-body, not completed"

    # And with nothing still in flight, a retry is one acquisition, not two.
    with STATE.lock:
        STATE.max_active = 0
    results = await execute_tool_round(
        [{"name": "http_get", "arguments": {"url": f"{server}/hop0", "force": True}}], None, reg
    )
    assert not results[0].was_error
    assert STATE.max_active == 1, "a retry must not run alongside the acquisition it replaced"


async def test_deadline_expiry_is_reported_as_the_fetchs_own_deadline(server, monkeypatch):
    """Not as a generic executor timeout: the two have different causes and
    different fixes, and only one of them is worth retrying."""
    monkeypatch.setattr(web, "_HTTP_GET_DEADLINE_S", 3.0)
    reg = ToolRegistry()
    web.register(reg)  # registration derives from the deadline
    inner = _instrument(reg)

    t0 = time.monotonic()
    results = await execute_tool_round(
        [{"name": "http_get", "arguments": {"url": f"{server}/drip", "force": True}}], None, reg
    )
    elapsed = time.monotonic() - t0
    assert elapsed < 8.0, f"the deadline is 3s; the call took {elapsed:.1f}s"
    body = results[0].content
    assert "deadline" in body.lower()
    assert "timed out after" not in body, "an executor timeout is a different diagnosis"
    assert (results[0].metadata or {}).get("fetch_status") == "deadline"
    assert (results[0].metadata or {}).get("source_complete") is False
    assert inner["end"] - inner["start"] < 8.0


async def test_a_dispatch_that_never_got_a_thread_never_fetches(server, reg, monkeypatch):
    """Queue waiting and started execution are different states, and a call
    the dispatcher gave up on must not run later with nobody listening."""
    monkeypatch.setattr(tool_executor, "_QUEUE_WAIT_CEILING_S", 0.4)
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pernix-tool")
    monkeypatch.setattr(tool_executor, "_get_tool_executor", lambda: pool)
    try:
        hog = asyncio.ensure_future(
            execute_tool_round([{"name": "http_get", "arguments": {"url": f"{server}/drip", "force": True}}], None, reg)
        )
        await asyncio.sleep(0.3)
        results = await execute_tool_round(
            [{"name": "http_get", "arguments": {"url": f"{server}/final", "force": True}}], None, reg
        )
        assert results[0].was_error
        assert "never started" in results[0].content and "saturated" in results[0].content
        assert "timed out after" not in results[0].content

        hog.cancel()
        with pytest.raises(asyncio.CancelledError):
            await hog
        await asyncio.sleep(1.0)
        assert STATE.requests == 1, "the queued call must never reach the network"
    finally:
        pool.shutdown(wait=False)


async def test_a_successful_fetch_carries_a_structured_status(server, reg):
    results = await execute_tool_round(
        [{"name": "http_get", "arguments": {"url": f"{server}/final", "force": True}}], None, reg
    )
    meta = results[0].metadata or {}
    assert meta.get("fetch_status") == "ok"
    assert meta.get("source_complete") is True
    assert meta.get("url", "").endswith("/final")
