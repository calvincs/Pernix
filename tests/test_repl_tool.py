"""Pernix — repl tool + prompt-as-variable binding (adaptation plan 2c)."""

import asyncio
import sys

import pytest

import core.kernel as kernel_mod
from core.kernel import SessionKernel, get_kernel_registry
from core.tools.builtin.repl_tool import register as register_repl
from core.tools.builtin.repl_tool import repl
from core.tools.executor import execute_tool_round
from core.tools.registry import ToolRegistry


@pytest.fixture(autouse=True)
def _kernel_env(tmp_path, monkeypatch):
    monkeypatch.setattr(kernel_mod, "KERNEL_STATE_ROOT", tmp_path / "kernels")
    monkeypatch.setattr(SessionKernel, "_interpreter", lambda self: sys.executable)
    monkeypatch.setattr("config.settings.session_kernel_enabled", True)
    yield
    # Kernels are keyed by session id in a process-global registry; shut down
    # anything this test created so state never leaks across tests.
    reg = get_kernel_registry()
    for sid in list(reg._kernels):
        reg.shutdown_session(sid, snapshot=False)


# ---------------------------------------------------------------------------
# The repl tool function
# ---------------------------------------------------------------------------


def test_repl_persists_namespace_across_calls():
    ctx = {"session_id": "repl-t1"}
    out = repl("x = 41", _context=ctx)
    assert "(no output)" in out  # assignment-only cells are success, not error
    assert "x:int" in out
    out = repl("print(x + 1)", _context=ctx)
    assert "42" in out


def test_repl_traceback_is_not_a_tool_error():
    ctx = {"session_id": "repl-t2"}
    out = repl("1/0", _context=ctx)
    assert "ZeroDivisionError" in out
    assert not out.startswith("Error:")  # iterative debugging, not failure
    # Kernel survives the traceback.
    out = repl("print('alive')", _context=ctx)
    assert "alive" in out


def test_repl_disabled_and_missing_context(monkeypatch):
    monkeypatch.setattr("config.settings.session_kernel_enabled", False)
    assert repl("x = 1", _context={"session_id": "s"}).startswith("Error:")
    monkeypatch.setattr("config.settings.session_kernel_enabled", True)
    assert repl("x = 1", _context={}).startswith("Error:")


def test_repl_registration_gated(monkeypatch):
    reg = ToolRegistry()
    monkeypatch.setattr("config.settings.session_kernel_enabled", False)
    register_repl(reg)
    assert reg.get("repl") is None

    monkeypatch.setattr("config.settings.session_kernel_enabled", True)
    register_repl(reg)
    tool = reg.get("repl")
    assert tool is not None
    assert tool.idempotent is False  # repeated identical cells must re-execute
    assert tool.max_timeout == 1800  # schema exposes timeout -> ceiling required
    assert tool.safety_level == "caution"


def test_tooldef_idempotent_defaults_true():
    reg = ToolRegistry()
    reg.register(
        name="t",
        func=lambda: "ok",
        description="d",
        parameters={"type": "object", "properties": {}},
    )
    assert reg.get("t").idempotent is True


# ---------------------------------------------------------------------------
# Prompt-as-variable binding post-pass
# ---------------------------------------------------------------------------


def _round(registry, calls, ctx):
    return asyncio.run(execute_tool_round(calls, ctx, registry))


@pytest.fixture
def bind_registry():
    reg = ToolRegistry()
    payload = "DATA-" + ("z" * 500) + "-MIDDLE-" + ("q" * 500) + "-END"
    reg.register(
        name="file_read",
        func=lambda path="": payload,
        description="fake read",
        parameters={"type": "object", "properties": {}},
    )
    reg.register(
        name="bash",
        func=lambda command="": payload,
        description="fake bash",
        parameters={"type": "object", "properties": {}},
    )
    reg.register(
        name="rlm_process",
        func=lambda query="": payload,
        description="fake rlm",
        parameters={"type": "object", "properties": {}},
    )
    return reg, payload


def test_large_eligible_result_is_bound(monkeypatch, bind_registry):
    reg, payload = bind_registry
    monkeypatch.setattr("config.settings.large_result_bind_threshold", 100)
    ctx = {"session_id": "bind-1"}

    results = _round(reg, [{"name": "file_read", "arguments": {"path": "big.txt"}}], ctx)
    r = results[0]
    assert "bound as `tool_result_1`" in r.content
    assert r.content.startswith("DATA-")  # head preserved
    assert "-END" in r.content  # tail preserved
    assert r.metadata["bound_var"] == "tool_result_1"
    assert r.metadata["orig_chars"] == len(payload)

    # Durable sidecar exists and the variable is live in the kernel.
    from pathlib import Path

    assert Path(r.metadata["payload_path"]).read_text() == payload
    out = repl("print(len(tool_result_1), tool_result_1[:5])", _context=ctx)
    assert str(len(payload)) in out and "DATA-" in out


def test_binding_covers_any_tool_not_excluded(monkeypatch, bind_registry):
    """Binding is an exclusion list, not a 4-tool allowlist: a big bash dump
    is data the model wants to slice exactly like a big file_read."""
    reg, payload = bind_registry
    monkeypatch.setattr("config.settings.large_result_bind_threshold", 100)
    ctx = {"session_id": "bind-bash"}

    r = _round(reg, [{"name": "bash", "arguments": {"command": "x"}}], ctx)[0]
    assert r.metadata["bound_var"] == "tool_result_1"
    assert "bound as `tool_result_1`" in r.content
    assert r.metadata["orig_chars"] == len(payload)
    out = repl("print(len(tool_result_1))", _context=ctx)
    assert str(len(payload)) in out


def test_binding_skips_excluded_small_and_disabled(monkeypatch, bind_registry):
    reg, payload = bind_registry
    monkeypatch.setattr("config.settings.large_result_bind_threshold", 100)
    ctx = {"session_id": "bind-2"}

    # Excluded tool (its answer is synthesized, not source data): untouched.
    results = _round(reg, [{"name": "rlm_process", "arguments": {"query": "x"}}], ctx)
    assert results[0].content == payload

    # Below threshold: untouched.
    monkeypatch.setattr("config.settings.large_result_bind_threshold", 10_000_000)
    results = _round(reg, [{"name": "file_read", "arguments": {}}], ctx)
    assert results[0].content == payload

    # Kernel disabled: untouched.
    monkeypatch.setattr("config.settings.large_result_bind_threshold", 100)
    monkeypatch.setattr("config.settings.session_kernel_enabled", False)
    results = _round(reg, [{"name": "file_read", "arguments": {}}], ctx)
    assert results[0].content == payload


def test_bind_ordinals_increment_and_survive_restart(monkeypatch, bind_registry):
    reg, payload = bind_registry
    monkeypatch.setattr("config.settings.large_result_bind_threshold", 100)
    ctx = {"session_id": "bind-3"}

    r1 = _round(reg, [{"name": "file_read", "arguments": {}}], ctx)[0]
    r2 = _round(reg, [{"name": "file_read", "arguments": {}}], ctx)[0]
    assert r1.metadata["bound_var"] == "tool_result_1"
    assert r2.metadata["bound_var"] == "tool_result_2"

    # A fresh kernel object for the same session seeds its counter from the
    # sidecar files, so restarts never reuse a cited ordinal.
    fresh = SessionKernel("bind-3")
    assert fresh.next_bind_ordinal() == 3


def test_bound_payload_path_is_actually_readable(monkeypatch, bind_registry):
    """The stub advertises the sidecar path to the model. data/kernels/ is
    outside the workspace, so without it as a read root every one of those
    pointers is dead on arrival — file_read refuses the path it was told to
    use."""
    from core.tools.builtin.core_tools import file_read
    from core.tools.paths import safe_read_path

    reg, payload = bind_registry
    monkeypatch.setattr("config.settings.large_result_bind_threshold", 100)
    ctx = {"session_id": "bind-path"}

    r = _round(reg, [{"name": "file_read", "arguments": {}}], ctx)[0]
    advertised = r.metadata["payload_path"]
    assert advertised in r.content  # the model is shown this exact path

    assert safe_read_path(advertised).read_text() == payload
    out = file_read(advertised)
    assert "DATA-" in out and not out.startswith("Error:")


def test_kernel_payload_root_absent_when_kernel_disabled(monkeypatch):
    from core.tools.paths import allowed_read_roots, kernel_state_root

    monkeypatch.setattr("config.settings.session_kernel_enabled", False)
    assert kernel_state_root() is None
    monkeypatch.setattr("config.settings.session_kernel_enabled", True)
    root = kernel_state_root()
    assert root is not None and root in allowed_read_roots()
