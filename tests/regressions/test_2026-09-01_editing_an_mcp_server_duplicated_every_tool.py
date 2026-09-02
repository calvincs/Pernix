"""Regression: re-adding an MCP server duplicated every one of its tools.

Shipped defect (2026-09-01 stability audit): add_server closed the old
connection but never unregistered its tools, and register_server_tools
computed the "taken" name set against the NEW connection's (empty)
registered map — so every tool of the replacement connection collided
with its own predecessor and was minted under a hash suffix
(mcp_stub_echo_1a2b3c next to a now-orphaned mcp_stub_echo). The UI's
"Save changes" and the agent's mcp_add_server both hit this; shutdown()
also left self.connections populated, so an mcp_enabled off→on cycle did
the same to every server at once.

Fix: add_server unregisters the closed connection's tools; shutdown()
drops its connections so start() spawns fresh ones; and the name diff in
register_server_tools is by server (category mcp:<name>) rather than by
connection object, so a successor reclaims its predecessor's names and
prunes whatever it does not re-register.
"""

from __future__ import annotations

from tests.test_mcp_manager import _stub_cfg, mcp_env  # noqa: F401  (fixture re-export)


def _stub_tools(reg) -> set[str]:
    return {t.name for t in reg.all_tools() if t.category == "mcp:stub"}


async def test_re_adding_a_server_keeps_one_copy_of_each_tool(mcp_env):  # noqa: F811
    from core.extensions.mcp.manager import MCPManager

    reg = mcp_env
    mgr = MCPManager()
    await mgr.start()
    try:
        first = await mgr.add_server(_stub_cfg())
        names = set(first.registered)
        assert "mcp_stub_echo" in names

        # "Save changes" in the UI → POST /api/mcp/servers → add_server again.
        second = await mgr.add_server(_stub_cfg())
        assert second is not first
        assert set(second.registered) == names
        assert _stub_tools(reg) == names, "orphans or hash-suffixed duplicates left behind"

        # Removal takes the whole set with it — nothing stale survives.
        assert await mgr.remove_server("stub") is True
        assert _stub_tools(reg) == set()
    finally:
        await mgr.shutdown()


async def test_mcp_enabled_off_then_on_replaces_tools_in_place(mcp_env):  # noqa: F811
    from core.extensions.mcp.manager import MCPManager

    reg = mcp_env
    mgr = MCPManager()
    await mgr.start()
    conn = await mgr.add_server(_stub_cfg())
    names = set(conn.registered)

    # Settings → mcp_enabled off: connections close, tools stay registered.
    await mgr.shutdown()
    assert mgr.connections == {}
    assert _stub_tools(reg) == names

    # ... and back on: start() re-reads the file and reconnects.
    await mgr.start()
    try:
        assert set(mgr.connections) == {"stub"}
        fresh = mgr.connections["stub"]
        assert fresh is not conn
        await fresh.ensure_ready()
        assert set(fresh.registered) == names
        assert _stub_tools(reg) == names
    finally:
        await mgr.shutdown()
