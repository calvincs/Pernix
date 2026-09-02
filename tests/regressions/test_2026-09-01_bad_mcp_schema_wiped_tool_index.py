"""One malformed remote tool schema emptied the whole discovery index.

ToolIndex.rebuild cleared _entries in place and then read
`tool.parameters["properties"].keys()`. `inputSchema` comes from the MCP
server, so `properties` can be null or a list: the raise left the index
holding only the tools iterated before the bad one — builtins included —
and the supervisor re-wiped it on every reconnect. Nothing else rebuilds
the index outside boot, so scout lost tool discovery until a restart.
"""

from core.extensions.mcp.manager import _normalize_input_schema
from core.tools.registry import ToolIndex, ToolRegistry


def _registry_with(bad_schema):
    reg = ToolRegistry()
    reg.register(
        "bash",
        func=lambda: "",
        description="run a command",
        parameters={"type": "object", "properties": {"command": {}}},
    )
    reg.register("broken", func=lambda: "", description="remote tool", parameters=bad_schema)
    reg.register(
        "file_read",
        func=lambda: "",
        description="read a file",
        parameters={"type": "object", "properties": {"path": {}}},
    )
    return reg


def test_a_null_properties_does_not_take_the_index_with_it():
    idx = ToolIndex()
    idx.rebuild({t.name: t for t in _registry_with({"type": "object", "properties": None}).all_tools()})
    names = {e.name for e in idx._entries.values()}
    assert "bash" in names and "file_read" in names, "builtins must survive one bad remote schema"


def test_a_list_where_an_object_belongs_is_survivable():
    idx = ToolIndex()
    idx.rebuild({t.name: t for t in _registry_with({"properties": ["nope"]}).all_tools()})
    assert {"bash", "file_read"} <= {e.name for e in idx._entries.values()}


def test_the_index_is_swapped_not_cleared_in_place():
    idx = ToolIndex()
    idx.rebuild({t.name: t for t in _registry_with({"type": "object", "properties": {}}).all_tools()})
    before = dict(idx._entries)
    assert len(before) == 3
    idx.rebuild({})
    assert idx._entries == {}


def test_schemas_are_normalized_before_they_reach_the_registry():
    assert _normalize_input_schema(None) == {"type": "object", "properties": {}}
    assert _normalize_input_schema({"properties": None})["properties"] == {}
    assert _normalize_input_schema({"properties": ["x"]})["properties"] == {}
    kept = _normalize_input_schema({"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]})
    assert kept["properties"] == {"a": {"type": "string"}}
    assert kept["required"] == ["a"]
    assert _normalize_input_schema({"properties": {}, "required": "a"}).get("required") is None
