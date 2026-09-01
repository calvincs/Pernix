"""Registry changes for MCP: unregister + late-registrant safety overrides."""

import json

from core.tools.registry import ToolRegistry


def _reg_tool(reg, name, source="mcp"):
    reg.register(
        name=name,
        func=lambda: "x",
        description=f"{name} tool",
        parameters={"type": "object", "properties": {}},
        source=source,
    )


def test_unregister_removes_tool_and_reports_existence():
    reg = ToolRegistry()
    _reg_tool(reg, "mcp_s_a")
    reg.rebuild_index()
    assert reg.unregister("mcp_s_a") is True
    assert reg.unregister("mcp_s_a") is False
    assert not reg.exists("mcp_s_a")
    assert reg.get_schemas(["mcp_s_a"]) == []
    reg.rebuild_index()
    assert all(s.name != "mcp_s_a" for s in reg.discover("tool", limit=20))


def test_safety_override_survives_unregister_and_reregister(tmp_path, monkeypatch):
    monkeypatch.setattr("core.tools.registry.TOOLS_CONFIG_PATH", tmp_path / "tools.json")
    reg = ToolRegistry()
    _reg_tool(reg, "mcp_s_a")
    reg.set_safety_level("mcp_s_a", "dangerous")
    # An MCP refresh unregisters + re-registers; the user's override must hold.
    reg.unregister("mcp_s_a")
    _reg_tool(reg, "mcp_s_a")
    assert reg.get("mcp_s_a").safety_level == "dangerous"


def test_load_config_keeps_overrides_for_late_registrants(tmp_path, monkeypatch):
    """Boot order is load_config() → MCP servers connect. An override saved
    for an MCP tool must survive that gap AND survive the next save."""
    cfg_path = tmp_path / "tools.json"
    cfg_path.write_text(json.dumps({"disabled": [], "safety_levels": {"mcp_s_late": "safe"}}))
    monkeypatch.setattr("core.tools.registry.TOOLS_CONFIG_PATH", cfg_path)
    reg = ToolRegistry()
    reg.load_config()  # tool not registered yet
    _reg_tool(reg, "mcp_s_late")
    assert reg.get("mcp_s_late").safety_level == "safe"
    # A later save (any disable) must not drop the stored override.
    reg.disable("other_tool")
    saved = json.loads(cfg_path.read_text())
    assert saved["safety_levels"]["mcp_s_late"] == "safe"
