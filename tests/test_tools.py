"""Tests for tool registry, discovery, and execution."""

from core.tools.registry import ToolIndex, ToolRegistry


def test_registry_register():
    reg = ToolRegistry()
    reg.register(
        name="test_tool",
        func=lambda: "ok",
        description="A test tool",
        parameters={"type": "object", "properties": {}},
        tags=["test"],
        category="core",
    )
    assert reg.exists("test_tool")
    assert not reg.exists("nonexistent")
    assert len(reg.all_tools()) == 1


def test_registry_disable_enable():
    reg = ToolRegistry()
    reg.register(name="t1", func=lambda: "", description="t1", parameters={"type": "object", "properties": {}})
    assert not reg.is_disabled("t1")
    reg.disable("t1")
    assert reg.is_disabled("t1")
    assert len(reg.enabled_tools()) == 0
    reg.enable("t1")
    assert len(reg.enabled_tools()) == 1


def test_registry_schemas():
    reg = ToolRegistry()
    reg.register(
        name="t1",
        func=lambda: "",
        description="desc1",
        parameters={"type": "object", "properties": {"x": {"type": "string"}}},
    )
    schemas = reg.get_schemas(["t1"])
    assert len(schemas) == 1
    assert schemas[0]["function"]["name"] == "t1"


def test_discovery_basic():
    reg = ToolRegistry()
    reg.register(
        name="search_web",
        func=lambda: "",
        description="Search the web",
        parameters={},
        tags=["search", "web", "internet"],
    )
    reg.register(name="file_read", func=lambda: "", description="Read a file", parameters={}, tags=["read", "file"])
    reg.rebuild_index()

    results = reg.discover("search the internet")
    assert results[0].name == "search_web"

    results2 = reg.discover("read a file")
    assert results2[0].name == "file_read"


def test_discovery_synonyms():
    reg = ToolRegistry()
    reg.register(
        name="bash",
        func=lambda: "",
        description="Run a shell command",
        parameters={},
        tags=["shell", "execute", "run", "command", "terminal"],
    )
    reg.rebuild_index()

    # "run" should match via synonym expansion
    results = reg.discover("execute a command")
    assert any(r.name == "bash" for r in results)


def test_discovery_cooccurrence():
    from core.tools.registry import TOOL_COOCCURRENCE

    reg = ToolRegistry()
    reg.register(
        name="spawn_worker",
        func=lambda: "",
        description="Spawn a worker",
        parameters={},
        tags=["parallel", "worker", "spawn"],
    )
    reg.register(
        name="check_workers",
        func=lambda: "",
        description="Check workers",
        parameters={},
        tags=["parallel", "worker", "check"],
    )
    reg.register(
        name="await_workers",
        func=lambda: "",
        description="Await workers",
        parameters={},
        tags=["parallel", "worker", "wait"],
    )
    reg.rebuild_index()

    results = reg.discover("spawn a parallel worker")
    names = [r.name for r in results]
    # Co-occurrence should pull in check_workers and await_workers
    assert "spawn_worker" in names
    assert "check_workers" in names


def test_execute_sync():
    reg = ToolRegistry()
    reg.register(
        name="add",
        func=lambda a, b: str(int(a) + int(b)),
        description="Add two numbers",
        parameters={"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}}},
    )

    result = reg.execute_sync("add", {"a": "3", "b": "4"})
    assert result == "7"

    result2 = reg.execute_sync("nonexistent", {})
    assert "Unknown tool" in result2


def test_health_metrics():
    reg = ToolRegistry()
    reg.register(name="t1", func=lambda: "ok", description="", parameters={"type": "object", "properties": {}})

    reg.metrics["t1"].record_success(100)
    reg.metrics["t1"].record_success(200)
    reg.metrics["t1"].record_failure("err", 50)

    assert reg.metrics["t1"].total_calls == 3
    assert reg.metrics["t1"].success_rate == 2 / 3
    assert reg.metrics["t1"].avg_latency_ms == 350 / 3


def test_discovery_excludes_disabled(monkeypatch, tmp_path):
    """Disabled tools must not surface from discover() — same fix that closes
    the gap for scout's baseline + search_tools wrapper."""
    # Point the tools-config persistence at tmp so we don't write to data/.
    monkeypatch.setattr("core.tools.registry.TOOLS_CONFIG_PATH", tmp_path / "tools.json")
    reg = ToolRegistry()
    reg.register(name="search_web", func=lambda: "", description="search", parameters={}, tags=["search", "web"])
    reg.register(name="file_read", func=lambda: "", description="read a file", parameters={}, tags=["read", "file"])
    reg.rebuild_index()

    # Both visible by default
    names = {r.name for r in reg.discover("search the web")}
    assert "search_web" in names

    reg.disable("search_web")
    names = {r.name for r in reg.discover("search the web")}
    assert "search_web" not in names
    # And still visible via all_tools (UI introspection)
    assert "search_web" in {t.name for t in reg.all_tools()}
