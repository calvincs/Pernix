"""A shell command that failed was booked as a success (harness review 3.2.1, H05).

bash printed `Exit code: N` only when the command said nothing at all, and
then prefixed `[cwd: …]` to everything it returned — so `startswith("Error:")`,
the executor's only failure test for a bare-string tool, could never fire for
a shell command. Every measured shape came back `was_error=False`: a silent
`exit 1`, a test run that printed "3 failed" and exited 1, a missing binary
exiting 127, even output that literally began with "Error:".

The harness then booked the call a success: per-tool health recorded a
success, the stuck detector's unresolved-failure state was cleared, and the
result was cached for cross-round dedup — so an identical rerun could be
answered from the pre-fix failure instead of running. The whole round's batch
is deduplicated before any of it executes, so even `file_write(fix)` +
`bash(rerun)` in ONE assistant response served the stale result.

A nonzero exit is a fact, not a verdict: grep exits 1 on no match and a
reproduction test is meant to fail. It is now reported honestly — an error the
agent sees, with the code in the text and in structured metadata — without
being charged to bash's health or to the stuck detector, both of which watch
for a broken tool rather than a failing command.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core.agent import StuckDetector, _record_round_results, _ToolCallGate, record_tool_outcome
from core.tools.builtin.core_tools import bash
from core.tools.builtin.core_tools import register as register_core
from core.tools.executor import ToolExecutionResult, execute_tool_round, is_command_failure
from core.tools.registry import ToolRegistry


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setattr("config.settings.shell_security_mode", "permissive")
    reg = ToolRegistry()
    register_core(reg)
    return reg


async def _run(registry, command: str, **args):
    calls = [{"name": "bash", "arguments": {"command": command, **args}}]
    return (await execute_tool_round(calls, {"session_id": ""}, registry))[0]


# ── the exit code decides, not the prose ─────────────────────────────────────


async def test_a_silent_exit_1_is_an_error(registry):
    """The reported case: nothing on stdout, exit 1, booked as a success."""
    r = await _run(registry, "exit 1")
    assert r.was_error is True
    assert r.metadata["exit_code"] == 1
    assert is_command_failure(r)


async def test_a_failing_command_that_printed_something_is_an_error(registry):
    r = await _run(registry, "echo '3 failed, 0 passed'; exit 1")
    assert r.was_error is True
    assert r.metadata["exit_code"] == 1
    assert "3 failed" in r.content


async def test_stderr_with_a_nonzero_exit_is_an_error(registry):
    r = await _run(registry, "echo boom >&2; exit 2")
    assert r.was_error is True
    assert r.metadata["exit_code"] == 2
    assert "boom" in r.content


async def test_output_beginning_with_error_is_judged_by_the_exit_code(registry):
    """Both directions: the text never decides. A command that prints
    "Error:" and exits 0 succeeded; one that prints it and exits 1 failed."""
    ok = await _run(registry, "echo 'Error: cosmetic'; exit 0")
    assert ok.was_error is False
    assert ok.metadata["exit_code"] == 0

    bad = await _run(registry, "echo 'Error: build failed'; exit 1")
    assert bad.was_error is True
    assert bad.metadata["exit_code"] == 1


async def test_a_missing_binary_reports_127(registry):
    r = await _run(registry, "no_such_binary_xyz")
    assert r.was_error is True
    assert r.metadata["exit_code"] == 127


async def test_a_successful_command_stays_a_success(registry):
    r = await _run(registry, "echo ok")
    assert r.was_error is False
    assert is_command_failure(r) is False
    assert r.metadata["exit_code"] == 0
    assert r.metadata["timed_out"] is False
    assert "ok" in r.content


async def test_the_exit_status_is_always_displayed(registry):
    """It used to appear only when the command produced no output at all —
    the one case where the agent could not have missed the failure anyway."""
    for command, code in (("echo ok", 0), ("echo '3 failed'; exit 1", 1), ("exit 3", 3)):
        r = await _run(registry, command)
        assert f"exit: {code}" in r.content.splitlines()[0], r.content


# ── infrastructure failures keep their old classification ────────────────────


async def test_a_timeout_is_still_an_error_and_is_not_a_command_failure(registry, monkeypatch):
    monkeypatch.setattr("config.settings.shell_timeout", 1)
    r = await _run(registry, "sleep 5", timeout=1)
    assert r.was_error is True
    assert r.content.startswith("Error:")
    assert "timed out" in r.content
    assert r.metadata["timed_out"] is True
    assert r.metadata["exit_code"] is None
    # The tool broke, not the command — the two must stay distinguishable.
    assert is_command_failure(r) is False


async def test_a_policy_block_is_still_an_error(registry):
    r = await _run(registry, "sudo rm -rf /")
    assert r.was_error is True
    assert r.content.startswith("Error:")
    assert is_command_failure(r) is False


async def test_an_empty_command_is_still_an_error(registry):
    r = await _run(registry, "   ")
    assert r.was_error is True
    assert r.content.startswith("Error:")


def test_the_structured_channel_carries_cwd_and_truncation(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.shell_security_mode", "permissive")
    out, meta = bash("echo hi")
    assert "hi" in out
    assert meta["cwd"]
    assert meta["truncated"] is False
    assert meta["total_chars"] >= 3


# ── a failing command is not a failing tool ──────────────────────────────────


async def test_tool_health_records_a_working_tool(registry):
    """bash ran the command it was given. Booking that as a bash failure is
    how a `grep` with no match turns into a "bash degraded" routing hint."""
    await _run(registry, "exit 1")
    m = registry.metrics["bash"]
    assert m.total_calls == 1
    assert m.failure_count == 0
    assert m.success_count == 1


def test_the_turn_summary_separates_a_failed_command_from_a_failed_tool():
    turn = SimpleNamespace(tool_summary={}, tool_summary_attempts=[], reflect_count=0)
    failed_command = ToolExecutionResult(
        tool_name="bash",
        content="[cwd: .] [exit: 1]\n1 failed",
        was_error=True,
        latency_ms=5,
        metadata={"exit_code": 1, "command_failed": True},
    )
    record_tool_outcome(turn, failed_command)
    entry = turn.tool_summary["bash"]
    assert entry["calls"] == 1
    assert entry["failures"] == 0, "candor/telos read `failures` as an unreliable tool"
    assert entry["command_failures"] == 1

    broken_tool = ToolExecutionResult(
        tool_name="bash",
        content="Error: Command timed out after 1s",
        was_error=True,
        latency_ms=5,
        metadata={"timed_out": True},
    )
    record_tool_outcome(turn, broken_tool)
    assert turn.tool_summary["bash"]["failures"] == 1


# ── the stuck detector must not read it as a broken tool ─────────────────────


async def _save_noop(role, content, **kw):
    return 1


async def _record(result, *, stuck, tool_failures, registry):
    """Drive the real round-accounting pass over one tool result."""
    session = SimpleNamespace(
        emit_event=lambda payload: None,
        turn=SimpleNamespace(tool_summary={}, tool_summary_attempts=[], reflect_count=0),
    )
    gate = _ToolCallGate(
        registry=registry,
        session=session,
        save_turn_msg=_save_noop,
        stuck=stuck,
        tool_failures=tool_failures,
    )
    tc = {"id": "c1", "name": result.tool_name, "arguments": json.dumps({"command": "pytest -q"})}
    await _record_round_results(
        [{"tc": tc, "parsed_args": {"command": "pytest -q"}}],
        [result],
        session=session,
        notes_by_id={},
        save_turn_msg=_save_noop,
        nudges_fired=set(),
        tool_failures=tool_failures,
        stuck=stuck,
        gate=gate,
        tool_round=1,
        active_tools=["bash"],
    )
    return session


async def test_a_reproduction_that_fails_does_not_arm_the_stuck_detector(registry):
    """A test written to fail, or a grep that finds nothing, exits nonzero on
    purpose. Charging that to the error-retry signals made the very next
    identical rerun — the whole point of the fix-and-rerun loop — score as an
    error loop."""
    stuck, tool_failures = StuckDetector(), {}
    result = ToolExecutionResult(
        tool_name="bash",
        content="[cwd: .] [exit: 1]\n1 failed",
        was_error=True,
        latency_ms=5,
        metadata={"exit_code": 1, "command_failed": True},
    )
    await _record(result, stuck=stuck, tool_failures=tool_failures, registry=registry)
    assert tool_failures == {}, "signal 3 (error-retry without change) must not arm"
    assert stuck.has_unresolved_failure is False
    assert stuck.tool_failure_counts == {}


async def test_a_broken_tool_still_arms_the_stuck_detector(registry):
    stuck, tool_failures = StuckDetector(), {}
    result = ToolExecutionResult(
        tool_name="bash",
        content="Error: Command timed out after 1s",
        was_error=True,
        latency_ms=5,
        metadata={"timed_out": True},
    )
    await _record(result, stuck=stuck, tool_failures=tool_failures, registry=registry)
    assert tool_failures.get("bash")
    assert stuck.has_unresolved_failure is True
    assert stuck.tool_failure_counts["bash"] == 1


# ── the dedup cache never answers for a shell call ───────────────────────────


def test_bash_registers_non_idempotent(registry):
    """Command text equality does not prove the environment is unchanged."""
    assert registry.get("bash").idempotent is False


def _call(name: str, args: dict) -> dict:
    return {"id": f"c{abs(hash(json.dumps(args, sort_keys=True))) % 9999}", "name": name, "arguments": json.dumps(args)}


async def test_a_fix_and_its_rerun_in_the_same_round_both_execute(registry):
    """The round's whole batch is deduplicated before any of it runs, so the
    mitigation that clears the cache after a successful file_write cannot help
    a fix and a rerun that arrive in one assistant response."""
    stuck = StuckDetector()
    session = SimpleNamespace(emit_event=lambda payload: None)
    gate = _ToolCallGate(
        registry=registry,
        session=session,
        save_turn_msg=_save_noop,
        stuck=stuck,
        tool_failures={},
    )
    rerun_args = {"command": "python3 app.py"}
    gate.remember_success("bash", json.dumps(rerun_args), 1, "AssertionError")

    calls = [_call("file_write", {"path": "app.py", "content": "print('fixed')\n"}), _call("bash", rerun_args)]
    admitted, _notes = await gate.admit(calls, ["file_write", "bash"])
    assert [a["tc"]["name"] for a in admitted] == ["file_write", "bash"]


async def test_an_identical_rerun_with_nothing_in_between_still_executes(registry):
    """External state moves without a tool call: a background job finishes, a
    file is edited by a script bash itself ran."""
    stuck = StuckDetector()
    session = SimpleNamespace(emit_event=lambda payload: None)
    gate = _ToolCallGate(
        registry=registry,
        session=session,
        save_turn_msg=_save_noop,
        stuck=stuck,
        tool_failures={},
    )
    args = {"command": "python3 solve.py"}
    gate.remember_success("bash", json.dumps(args), 1, "IndexError")
    admitted, _notes = await gate.admit([_call("bash", args)], ["bash"])
    assert [a["tc"]["name"] for a in admitted] == ["bash"]
