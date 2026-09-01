"""MCP tools in the no-scout fallback surface.

source="mcp" is deliberately outside the builtin force-add, so scout curates
MCP tools per turn. The no-scout fallback used to include every enabled tool;
for MCP that would hand a couple of connected servers the whole schema. The
rule: excluded, unless this session already used the tool successfully.
"""

from core.agent import _resolve_tool_surface
from core.tools.registry import ToolRegistry
from sessions.state import AgentSession


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    for name, source in (
        ("bash", "builtin"),
        ("recall", "builtin"),
        ("mcp_gh_issues", "mcp"),
        ("mcp_gh_search", "mcp"),
    ):
        reg.register(
            name=name,
            func=lambda: "x",
            description=f"{name} tool",
            parameters={"type": "object", "properties": {}},
            source=source,
        )
    return reg


def test_fallback_excludes_mcp_tools(monkeypatch):
    monkeypatch.setattr("core.agent._prior_turn_tool_names", lambda sid: set())
    session = AgentSession(session_id="s1")
    session.last_scout_report = None
    _, names = _resolve_tool_surface(session, "s1", _registry())
    assert set(names) == {"bash", "recall"}


def test_fallback_keeps_mcp_tools_this_session_already_used(monkeypatch):
    monkeypatch.setattr("core.agent._prior_turn_tool_names", lambda sid: {"mcp_gh_issues", "bash"})
    session = AgentSession(session_id="s1")
    session.last_scout_report = None
    _, names = _resolve_tool_surface(session, "s1", _registry())
    assert "mcp_gh_issues" in names
    assert "mcp_gh_search" not in names
