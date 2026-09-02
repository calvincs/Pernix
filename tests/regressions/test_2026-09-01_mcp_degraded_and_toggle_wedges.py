"""Three ways an MCP server stopped answering promptly.

1. toggle_server(enabled=True) on an already-running server overwrote its
   status with "connecting". start() is a no-op while the task is alive, so
   nothing ever set it back: every call then waited the full connect
   timeout and failed, and nothing self-healed it until a restart.
2. ensure_ready kicked the supervisor awake on every call, defeating the
   exponential backoff. Against a host that blackholes packets each agent
   call paid 30-40s.
3. The call-failure breaker was only reset by a successful call, so after
   one trip every later error forced a full reconnect.
"""

import time

import pytest

from core.extensions.mcp.manager import MCPConnection, MCPManager, MCPUnavailable
from tests.test_mcp_manager import _stub_cfg, mcp_env  # noqa: F401  (fixture re-export)


def _conn(name="srv"):
    return MCPConnection(_stub_cfg(name), MCPManager())


async def test_a_degraded_server_fails_fast_inside_its_backoff_window():
    conn = _conn()
    conn.status = "degraded"
    conn.error = "All connection attempts failed"
    conn._degraded_until = time.time() + 30

    with pytest.raises(MCPUnavailable) as exc:
        await conn.ensure_ready()
    assert "degraded" in str(exc.value)
    assert "All connection attempts failed" in str(exc.value), "the cached reason is worth reporting"


async def test_past_the_window_it_tries_again():
    conn = _conn()
    conn.status = "degraded"
    conn._degraded_until = time.time() - 1  # window elapsed
    # No supervisor is running, so this parks on the ready-waiter and times
    # out — the point is that it ATTEMPTS rather than refusing outright.
    conn._resume_evt.clear()
    import asyncio

    with pytest.raises((MCPUnavailable, asyncio.TimeoutError)):
        await asyncio.wait_for(conn.ensure_ready(), timeout=0.3)
    assert conn._resume_evt.is_set(), "the supervisor must be woken once the backoff has elapsed"


def test_a_fresh_connection_starts_with_no_cooldown():
    assert _conn()._degraded_until == 0.0


async def test_toggling_on_a_running_server_leaves_its_status_alone(mcp_env):  # noqa: F811
    mgr = MCPManager()
    conn = _conn("live")
    mgr.connections["live"] = conn
    conn.status = "ready"

    class _AliveTask:
        def done(self):
            return False

    conn._task = _AliveTask()
    out = await mgr.toggle_server("live", True)
    assert out.status == "ready", "a live server must not be pushed into 'connecting' and stranded"
