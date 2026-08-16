"""Tests for core/tools/executor.py: single tool execution and rounds."""

import asyncio

import pytest

from core.tools.executor import (
    ToolExecutionResult,
    _execute_single,
    _resolve_timeout,
    execute_tool_round,
)
from core.tools.registry import ToolDef, ToolRegistry


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
# _resolve_timeout
# ---------------------------------------------------------------------------


def _tool(timeout: int, max_timeout: int = 0) -> ToolDef:
    return ToolDef(
        name="t",
        description="d",
        parameters={"type": "object", "properties": {}},
        function=lambda: "ok",
        timeout=timeout,
        max_timeout=max_timeout,
    )


def test_resolve_timeout_without_ceiling_ignores_caller_override():
    """A tool that never declared max_timeout is not overridable — but still
    gets the dispatch grace, so the tool's own timeout fires first."""
    assert _resolve_timeout(_tool(30), {"timeout": 1800}) == 35
    assert _resolve_timeout(_tool(30), None) == 35


def test_resolve_timeout_honors_override_up_to_ceiling():
    """bash's documented 1800s override must actually reach the dispatcher."""
    t = _tool(30, max_timeout=1800)
    # Grace is added so the tool's own internal timeout fires first.
    assert _resolve_timeout(t, {"timeout": 600}) > 600
    assert _resolve_timeout(t, {"timeout": 600}) == 605


def test_resolve_timeout_clamps_to_ceiling():
    t = _tool(30, max_timeout=1800)
    assert _resolve_timeout(t, {"timeout": 99999}) == 1805


def test_resolve_timeout_ignores_junk_and_below_default_values():
    """Junk falls back to the tool default — with grace, like every other
    path: the dispatcher must never win the race against the tool's own
    timeout, and the default path is the one that runs most often."""
    t = _tool(30, max_timeout=1800)
    assert _resolve_timeout(t, {"timeout": 0}) == 35
    assert _resolve_timeout(t, {"timeout": -5}) == 35
    assert _resolve_timeout(t, {"timeout": "nonsense"}) == 35
    assert _resolve_timeout(t, {}) == 35
    # Below the tool default: never shrink under it, but still grant grace.
    assert _resolve_timeout(t, {"timeout": 5}) == 35


def test_bash_registers_a_timeout_ceiling():
    """Regression: bash advertises `timeout` in its schema, so it must declare
    max_timeout or the executor caps every call at shell_timeout."""
    from core.tools.builtin.core_tools import BASH_MAX_TIMEOUT, register

    reg = ToolRegistry()
    register(reg)
    bash_def = reg.get("bash")
    assert "timeout" in bash_def.parameters["properties"]
    assert bash_def.max_timeout == BASH_MAX_TIMEOUT


def test_every_tool_exposing_timeout_declares_a_ceiling():
    """Guard the whole builtin+extension surface against the same trap."""
    from core.extensions import load_extensions
    from core.tools.builtin import load_builtin_tools

    reg = ToolRegistry()
    load_builtin_tools(reg)
    try:
        load_extensions(reg)
    except Exception:
        pass  # extensions are optional here; builtins are the contract
    offenders = [
        t.name
        for t in reg.all_tools()
        if "timeout" in (t.parameters or {}).get("properties", {}) and t.max_timeout <= 0
    ]
    assert not offenders, f"tools expose a `timeout` arg but declare no max_timeout: {offenders}"


def test_batch_timeout_covers_the_slowest_tool_in_the_batch():
    """The gather backstop must never fire before a per-call timeout.

    A parallel_safe tool registered above settings.tool_timeout would
    otherwise blow up the whole round instead of failing one call.
    """
    from config import settings
    from core.tools.executor import _batch_timeout

    reg = ToolRegistry()
    reg.register(
        "slow_par",
        func=lambda: "ok",
        description="s",
        parameters={"type": "object", "properties": {}},
        parallel_safe=True,
        timeout=settings.tool_timeout + 600,
    )
    reg.register(
        "fast_par",
        func=lambda: "ok",
        description="f",
        parameters={"type": "object", "properties": {}},
        parallel_safe=True,
        timeout=5,
    )

    calls = [{"name": "fast_par", "arguments": {}}, {"name": "slow_par", "arguments": {}}]
    assert _batch_timeout([0, 1], calls, reg) > settings.tool_timeout + 600
    # An all-fast batch still gets at least the configured floor.
    assert _batch_timeout([0], calls, reg) > settings.tool_timeout


async def test_slow_parallel_tool_does_not_destroy_the_round():
    """Regression: one slow parallel call must not take its peers down.

    Pre-fix the gather was bounded by a fixed settings.tool_timeout, so a
    tool registered above it raised TimeoutError out of execute_tool_round
    and discarded every sibling result along with it.
    """
    import time as _time

    from config import settings

    reg = ToolRegistry()
    reg.register(
        "slow_par",
        func=lambda: (_time.sleep(0.2), "SLOW")[1],
        description="s",
        parameters={"type": "object", "properties": {}},
        parallel_safe=True,
        timeout=settings.tool_timeout + 600,
    )
    reg.register(
        "quick_par",
        func=lambda: "QUICK",
        description="q",
        parameters={"type": "object", "properties": {}},
        parallel_safe=True,
        timeout=5,
    )

    calls = [{"name": "quick_par", "arguments": {}}, {"name": "slow_par", "arguments": {}}]
    results = await execute_tool_round(calls, None, reg)
    assert [r.content for r in results] == ["QUICK", "SLOW"]


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


def _make_approved_session(monkeypatch, tool_name, scope, persistent=False):
    """Fake a session whose _approved_dangerous_tools holds one approval."""

    class _FakeSession:
        session_type = "normal"
        parent_session_id = None
        _approved_dangerous_tools = {tool_name: {"scope": scope, "persistent": persistent}}

    fake = _FakeSession()
    monkeypatch.setattr("sessions.manager.get_manager", lambda: type("M", (), {"get": lambda self, s: fake})())
    return fake


async def test_dangerous_approval_scope_mismatch_blocks(monkeypatch):
    """A single-use approval whose scope doesn't mention the call's argument
    values must not unlock the call (approve 'delete skill foo' must not
    allow delete_skill(name='bar'))."""

    def dangerous_fn(name=""):
        return f"deleted {name}"

    reg = ToolRegistry()
    reg.register(
        name="risky_scoped",
        func=dangerous_fn,
        description="Dangerous",
        parameters={"type": "object", "properties": {"name": {"type": "string"}}},
        safety_level="dangerous",
        timeout=5,
    )
    monkeypatch.setattr("config.settings.auto_approve_dangerous", False)
    fake = _make_approved_session(monkeypatch, "risky_scoped", "delete skill foofoo")

    result = await _execute_single("risky_scoped", {"name": "barbar"}, {"session_id": "s1"}, reg)
    assert result.was_error
    assert "scope" in result.content.lower()
    # The approval is left intact for the call the user actually confirmed.
    assert "risky_scoped" in fake._approved_dangerous_tools


async def test_dangerous_approval_scope_match_consumes(monkeypatch):
    """A single-use approval covering the call's argument values unlocks
    exactly one call and is consumed."""

    def dangerous_fn(name=""):
        return f"deleted {name}"

    reg = ToolRegistry()
    reg.register(
        name="risky_scoped2",
        func=dangerous_fn,
        description="Dangerous",
        parameters={"type": "object", "properties": {"name": {"type": "string"}}},
        safety_level="dangerous",
        timeout=5,
    )
    monkeypatch.setattr("config.settings.auto_approve_dangerous", False)
    fake = _make_approved_session(monkeypatch, "risky_scoped2", "delete skill foofoo")

    result = await _execute_single("risky_scoped2", {"name": "foofoo"}, {"session_id": "s1"}, reg)
    assert not result.was_error
    assert result.content == "deleted foofoo"
    assert "risky_scoped2" not in fake._approved_dangerous_tools


async def test_dangerous_approval_persistent_stays_broad(monkeypatch):
    """Persistent approvals keep their documented broad-scope behavior."""

    def dangerous_fn(url=""):
        return f"fetched {url}"

    reg = ToolRegistry()
    reg.register(
        name="risky_persist",
        func=dangerous_fn,
        description="Dangerous",
        parameters={"type": "object", "properties": {"url": {"type": "string"}}},
        safety_level="dangerous",
        timeout=5,
    )
    monkeypatch.setattr("config.settings.auto_approve_dangerous", False)
    fake = _make_approved_session(
        monkeypatch, "risky_persist", "browse several pages while researching", persistent=True
    )

    result = await _execute_single("risky_persist", {"url": "https://example.com/x"}, {"session_id": "s1"}, reg)
    assert not result.was_error
    assert "risky_persist" in fake._approved_dangerous_tools


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


async def test_execute_tool_round_mixed_preserves_call_order():
    """results[i] must be the result of tool_calls[i], whatever the mix.

    core/agent.py zips parsed_calls against these results to attach each
    result to its originating call's tool_call_id. Bucketing parallel-safe
    calls ahead of sequential ones used to reorder the returned list, so a
    round like [sequential, parallel] handed every result to the wrong call.
    Sequential-first ordering is the case that regressed.
    """
    reg = ToolRegistry()
    reg.register(
        "par_tool",
        func=lambda: "PAR",
        description="p",
        parameters={"type": "object", "properties": {}},
        parallel_safe=True,
        timeout=5,
    )
    reg.register(
        "seq_tool",
        func=lambda: "SEQ",
        description="s",
        parameters={"type": "object", "properties": {}},
        parallel_safe=False,
        timeout=5,
    )

    # Sequential first — the ordering that used to come back reversed.
    calls = [
        {"name": "seq_tool", "arguments": {}},
        {"name": "par_tool", "arguments": {}},
    ]
    results = await execute_tool_round(calls, None, reg)
    assert [r.tool_name for r in results] == ["seq_tool", "par_tool"]
    assert [r.content for r in results] == ["SEQ", "PAR"]

    # Interleaved, with repeats, to catch index-mapping slips.
    calls = [
        {"name": "seq_tool", "arguments": {}},
        {"name": "par_tool", "arguments": {}},
        {"name": "seq_tool", "arguments": {}},
        {"name": "par_tool", "arguments": {}},
    ]
    results = await execute_tool_round(calls, None, reg)
    assert [r.tool_name for r in results] == [c["name"] for c in calls]


async def test_execute_tool_round_order_holds_when_a_parallel_call_raises():
    """A raising parallel call keeps its own slot; peers are not shifted."""

    def boom():
        raise RuntimeError("kaboom")

    reg = ToolRegistry()
    reg.register(
        "seq_tool",
        func=lambda: "SEQ",
        description="s",
        parameters={"type": "object", "properties": {}},
        parallel_safe=False,
        timeout=5,
    )
    reg.register(
        "bad_par",
        func=boom,
        description="b",
        parameters={"type": "object", "properties": {}},
        parallel_safe=True,
        timeout=5,
    )
    reg.register(
        "good_par",
        func=lambda: "GOOD",
        description="g",
        parameters={"type": "object", "properties": {}},
        parallel_safe=True,
        timeout=5,
    )

    calls = [
        {"name": "seq_tool", "arguments": {}},
        {"name": "bad_par", "arguments": {}},
        {"name": "good_par", "arguments": {}},
    ]
    results = await execute_tool_round(calls, None, reg)
    assert [r.tool_name for r in results] == ["seq_tool", "bad_par", "good_par"]
    assert results[0].content == "SEQ"
    assert results[1].was_error
    assert results[2].content == "GOOD"


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


# ---------------------------------------------------------------------------
# _is_failure_verdict — memory-write verdicts in tool health
# ---------------------------------------------------------------------------


def test_write_failure_verdicts_count_as_errors():
    from core.tools.executor import _is_failure_verdict

    assert _is_failure_verdict("NOT SAVED — Memory system unavailable")
    assert _is_failure_verdict("NOT UPDATED — no entry with epoch=123 in 'demo.notes'")
    assert _is_failure_verdict(
        "NOT DELETED — VERIFY=STILL-PRESENT: entry epoch=123 is still in demo.notes on read-back"
    )


def test_dedup_refusal_is_not_a_tool_failure():
    from core.tools.executor import _is_failure_verdict

    assert not _is_failure_verdict(
        'NOT SAVED — duplicate of demo.notes@123: "already stored". If your version is newer'
    )


def test_success_verdicts_and_plain_results_are_not_failures():
    from core.tools.executor import _is_failure_verdict

    assert not _is_failure_verdict("SAVED file=demo.notes epoch=123 VERIFY=OK")
    assert not _is_failure_verdict("UPDATED file=demo.notes epoch=123 VERIFY=OK")
    assert not _is_failure_verdict("some ordinary tool output")


@pytest.mark.asyncio
async def test_execute_single_records_write_failure_verdict_as_error():
    reg = _make_registry({"remember": lambda: "NOT SAVED — Memory system unavailable"})
    result = await _execute_single("remember", {}, None, reg)
    assert result.was_error is True
    assert reg.metrics["remember"].failure_count == 1
