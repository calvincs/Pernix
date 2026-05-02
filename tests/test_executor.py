"""Tests for core/tools/executor.py: single tool execution and rounds."""

import asyncio

import pytest

from core.tools.executor import ToolExecutionResult, _execute_single, execute_tool_round
from core.tools.registry import ToolRegistry


def _make_registry(tools: dict | None = None) -> ToolRegistry:
    """Build a test registry with simple sync tool functions."""
    reg = ToolRegistry()
    if tools:
        for name, func in tools.items():
            reg.register(
                name=name,
                func=func,
                description=f"Test tool {name}",
                parameters={"type": "object", "properties": {}},
                parallel_safe=True,
                timeout=5,
            )
    return reg


# ---------------------------------------------------------------------------
# _execute_single
# ---------------------------------------------------------------------------


async def test_execute_single_success():
    reg = _make_registry({"echo": lambda: "hello"})
    result = await _execute_single("echo", {}, None, reg)
    assert result.tool_name == "echo"
    assert result.content == "hello"
    assert not result.was_error
    assert result.latency_ms >= 0


async def test_execute_single_unknown_tool():
    reg = _make_registry()
    result = await _execute_single("nonexistent", {}, None, reg)
    assert result.was_error
    assert "Unknown tool" in result.content


async def test_execute_single_disabled_tool():
    reg = _make_registry({"myecho": lambda: "ok"})
    reg.disable("myecho")
    result = await _execute_single("myecho", {}, None, reg)
    assert result.was_error
    assert "disabled" in result.content


async def test_execute_single_error_result():
    def bad_tool():
        return "Error: something went wrong"

    reg = _make_registry({"bad": bad_tool})
    result = await _execute_single("bad", {}, None, reg)
    assert result.was_error
    assert "something went wrong" in result.content


async def test_execute_single_exception():
    def exploding_tool():
        raise RuntimeError("boom")

    reg = _make_registry({"explode": exploding_tool})
    result = await _execute_single("explode", {}, None, reg)
    assert result.was_error
    assert "boom" in result.content


async def test_execute_single_timeout():
    import time

    def slow_tool():
        time.sleep(10)
        return "done"

    reg = ToolRegistry()
    reg.register(
        name="slow",
        func=slow_tool,
        description="Slow tool",
        parameters={"type": "object", "properties": {}},
        timeout=0.1,  # very short timeout
    )
    result = await _execute_single("slow", {}, None, reg)
    assert result.was_error
    assert "timed out" in result.content


async def test_execute_single_dangerous_blocked(monkeypatch):
    """Dangerous tools are blocked when auto_approve_dangerous=False."""

    def dangerous_fn():
        return "executed"

    reg = ToolRegistry()
    reg.register(
        name="risky",
        func=dangerous_fn,
        description="Dangerous",
        parameters={"type": "object", "properties": {}},
        safety_level="dangerous",
    )
    monkeypatch.setattr("config.settings.auto_approve_dangerous", False)
    result = await _execute_single("risky", {}, None, reg)
    assert result.was_error
    assert "dangerous" in result.content.lower()


async def test_execute_single_dangerous_allowed(monkeypatch):
    """Dangerous tools execute when auto_approve_dangerous=True."""

    def dangerous_fn():
        return "executed"

    reg = ToolRegistry()
    reg.register(
        name="risky2",
        func=dangerous_fn,
        description="Dangerous",
        parameters={"type": "object", "properties": {}},
        safety_level="dangerous",
        timeout=5,
    )
    monkeypatch.setattr("config.settings.auto_approve_dangerous", True)
    result = await _execute_single("risky2", {}, None, reg)
    assert not result.was_error
    assert result.content == "executed"


# ---------------------------------------------------------------------------
# execute_tool_round
# ---------------------------------------------------------------------------


async def test_execute_tool_round_sequential():
    """Sequential tools run in order and all complete."""
    order = []

    def tool_a():
        order.append("a")
        return "a done"

    def tool_b():
        order.append("b")
        return "b done"

    reg = ToolRegistry()
    for name, fn in [("tool_a", tool_a), ("tool_b", tool_b)]:
        reg.register(
            name=name,
            func=fn,
            description=name,
            parameters={"type": "object", "properties": {}},
            parallel_safe=False,
            timeout=5,
        )

    calls = [{"name": "tool_a", "arguments": {}}, {"name": "tool_b", "arguments": {}}]
    results = await execute_tool_round(calls, None, reg)
    assert len(results) == 2
    assert results[0].content == "a done"
    assert results[1].content == "b done"
    assert order == ["a", "b"]


async def test_execute_tool_round_parallel():
    """Parallel-safe tools are dispatched concurrently."""
    reg = ToolRegistry()
    for name in ["pa", "pb", "pc"]:
        reg.register(
            name=name,
            func=lambda: "ok",
            description=name,
            parameters={"type": "object", "properties": {}},
            parallel_safe=True,
            timeout=5,
        )
    calls = [{"name": n, "arguments": {}} for n in ["pa", "pb", "pc"]]
    results = await execute_tool_round(calls, None, reg)
    assert len(results) == 3
    assert all(r.content == "ok" for r in results)


async def test_execute_tool_round_mixed():
    """Mixed parallel + sequential calls both complete."""
    reg = ToolRegistry()
    reg.register(
        "par_tool",
        func=lambda: "par",
        description="p",
        parameters={"type": "object", "properties": {}},
        parallel_safe=True,
        timeout=5,
    )
    reg.register(
        "seq_tool",
        func=lambda: "seq",
        description="s",
        parameters={"type": "object", "properties": {}},
        parallel_safe=False,
        timeout=5,
    )

    calls = [
        {"name": "par_tool", "arguments": {}},
        {"name": "seq_tool", "arguments": {}},
    ]
    results = await execute_tool_round(calls, None, reg)
    assert len(results) == 2
    contents = {r.tool_name: r.content for r in results}
    assert contents["par_tool"] == "par"
    assert contents["seq_tool"] == "seq"


async def test_execute_tool_round_empty():
    reg = _make_registry()
    results = await execute_tool_round([], None, reg)
    assert results == []


async def test_execute_tool_round_health_metrics():
    """Tool health metrics are updated after execution."""

    def counting_fn():
        return "ok"

    reg = _make_registry({"counter": counting_fn})
    calls = [{"name": "counter", "arguments": {}}]
    await execute_tool_round(calls, None, reg)
    metrics = reg.metrics.get("counter")
    assert metrics is not None
    assert metrics.success_count >= 1
