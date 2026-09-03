"""MCP manager lifecycle against the stdio stub server (tests/fixtures/mcp_stub.py).

These spawn a real subprocess speaking real MCP over stdio — the closest
thing to the box without a network. Kept to a few broad tests so the suite
doesn't pay one interpreter start per assertion.
"""

import asyncio
import sys

import pytest

from core.extensions.mcp.config import MCPServerConfig
from core.extensions.mcp.manager import MCPManager, MCPUnavailable


@pytest.fixture
def mcp_env(monkeypatch, tmp_path):
    """Fresh registry singleton + tmp config/log paths."""
    from core.tools import registry as regmod

    fresh = regmod.ToolRegistry()
    monkeypatch.setattr(regmod, "_registry", fresh)
    monkeypatch.setattr("core.tools.registry.TOOLS_CONFIG_PATH", tmp_path / "tools.json")
    monkeypatch.setattr("core.extensions.mcp.config.MCP_SERVERS_PATH", tmp_path / "mcp_servers.json")
    monkeypatch.setattr("core.extensions.mcp.manager.LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr("config.settings.mcp_enabled", True)
    monkeypatch.setattr("config.settings.mcp_stdio_enabled", True)
    monkeypatch.setattr("config.settings.mcp_connect_timeout", 25)
    return fresh


def _stub_cfg(name="stub", **overrides) -> MCPServerConfig:
    base = dict(
        name=name,
        transport="stdio",
        command=sys.executable,
        args=["-m", "tests.fixtures.mcp_stub"],
    )
    base.update(overrides)
    return MCPServerConfig(**base)


async def _call(reg, tool, args):
    loop = asyncio.get_running_loop()
    return await asyncio.to_thread(reg.execute_sync, tool, args, {"_loop": loop, "session_id": "t"})


async def test_lifecycle_end_to_end(mcp_env):
    reg = mcp_env
    mgr = MCPManager()
    await mgr.start()
    conn = await mgr.add_server(_stub_cfg())
    try:
        assert conn.status == "ready"
        names = set(conn.registered)
        assert "mcp_stub_echo" in names and "mcp_stub_wipe" in names

        # Safety: default caution; destructive annotation escalates.
        assert reg.get("mcp_stub_echo").safety_level == "caution"
        assert reg.get("mcp_stub_wipe").safety_level == "dangerous"
        # Canary sessions must never reach external services.
        assert "canary" in reg.get("mcp_stub_echo").denied_session_types
        assert reg.get("mcp_stub_echo").source == "mcp"

        # Round trips through the sync bridge.
        out = await _call(reg, "mcp_stub_echo", {"text": "hi"})
        assert out[0] == "echo: hi" and out[1]["mcp_server"] == "stub"
        err = await _call(reg, "mcp_stub_boom", {})
        assert err[0].startswith("Error:")

        # Suspend (idle reap) then transparent resume on the next call.
        conn.suspend()
        await asyncio.sleep(0.5)
        assert conn.status == "idle"
        out = await _call(reg, "mcp_stub_add", {"a": 1, "b": 2})
        assert out[0] == "3" and conn.status == "ready"

        # Toggle off unregisters; back on re-registers.
        await mgr.toggle_server("stub", False)
        assert not reg.exists("mcp_stub_echo")
        conn = await mgr.toggle_server("stub", True)
        await conn.ensure_ready()
        assert reg.exists("mcp_stub_echo")

        # Removal unregisters and drops config.
        assert await mgr.remove_server("stub") is True
        assert not reg.exists("mcp_stub_echo")
    finally:
        await mgr.shutdown()


async def test_allowlist_and_tool_cap(mcp_env, monkeypatch):
    reg = mcp_env
    mgr = MCPManager()
    await mgr.start()
    try:
        conn = await mgr.add_server(_stub_cfg(name="allow", tool_allowlist=["echo", "add"]))
        assert set(conn.registered.values()) == {"echo", "add"}

        monkeypatch.setattr("config.settings.mcp_max_tools_per_server", 2)
        conn2 = await mgr.add_server(_stub_cfg(name="capped"))
        assert len(conn2.registered) == 2  # first two by name, rest skipped
        assert reg.get("mcp_capped_add") is not None
    finally:
        await mgr.shutdown()


async def test_stdio_gate_and_disabled_entries(mcp_env, monkeypatch):
    reg = mcp_env
    mgr = MCPManager()
    await mgr.start()
    try:
        # enabled=False: configured but never spawned, no tools.
        conn = await mgr.add_server(_stub_cfg(name="off", enabled=False))
        assert conn.status == "disabled" and conn.registered == {}
        assert not reg.exists("mcp_off_echo")

        # Remote-only mode: stdio transport refuses to connect.
        monkeypatch.setattr("config.settings.mcp_stdio_enabled", False)
        with pytest.raises(MCPUnavailable):
            await mgr.add_server(_stub_cfg(name="blocked"))
        assert mgr.connections["blocked"].status == "degraded"
    finally:
        await mgr.shutdown()


async def test_bridge_refuses_when_mcp_disabled(mcp_env, monkeypatch):
    reg = mcp_env
    mgr = MCPManager()
    await mgr.start()
    try:
        await mgr.add_server(_stub_cfg())
        monkeypatch.setattr("config.settings.mcp_enabled", False)
        out = await _call(reg, "mcp_stub_echo", {"text": "x"})
        body = out[0] if isinstance(out, tuple) else out
        assert body.startswith("Error:") and "disabled" in body
    finally:
        await mgr.shutdown()
