"""The cross-round dedup cache answered calls that had to actually run.

Field case, session 3dc5a307d751 (2026-09-03), two shapes of the same bug:

1. Polls. `check_workers`, `await_workers` and `goal_status` registered as
   idempotent, so the second identical poll got "(already executed in round N
   with identical arguments)" — a frozen snapshot of a world that had moved on.
   job_status/job_tail already registered `idempotent=False`; these did not.

2. bash re-runs. The cache is only invalidated by file_write/file_edit/
   multiedit, but the agent edited its script with `sed -i` through bash. The
   next identical `python3 solve.py` was answered from the pre-edit cache, and
   StuckDetector signal 2 — which compares against the mutation epoch — read
   the pair as a tool cycle and fired a false "repeating tool calls" nudge.

Now a successful bash call retires every OTHER cached bash result and bumps
the mutation epoch, so only an exact back-to-back repeat with nothing in
between still short-circuits.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from core.agent import StuckDetector, _MUTATING_TOOLS, _ToolCallGate
from core.tools.registry import ToolRegistry


# --- 1. polls are never served from the cache -------------------------------


def test_worker_polls_register_non_idempotent():
    from core.extensions.orchestration import register as register_orchestration

    reg = ToolRegistry()
    register_orchestration(reg)
    assert reg.get("check_workers").idempotent is False
    assert reg.get("await_workers").idempotent is False
    # A one-shot fetch stays cacheable — this is not a blanket flip.
    assert reg.get("get_worker_result").idempotent is True


def test_goal_status_registers_non_idempotent(monkeypatch):
    from core.tools.builtin.goal_tools import register as register_goals

    monkeypatch.setattr("config.settings.goals_enabled", True)
    reg = ToolRegistry()
    register_goals(reg)
    assert reg.get("goal_status").idempotent is False


# --- 2. bash edits invalidate the bash cache --------------------------------


def _bash(command: str) -> dict:
    return {"id": f"call-{abs(hash(command)) % 10000}", "name": "bash", "arguments": json.dumps({"command": command})}


def _make_gate(stuck: StuckDetector, saved: list) -> _ToolCallGate:
    async def _save(role, content, tool_call_id=""):
        saved.append((role, content, tool_call_id))

    registry = SimpleNamespace(get=lambda name: SimpleNamespace(idempotent=True))
    session = SimpleNamespace(emit_event=lambda payload: None)
    return _ToolCallGate(
        registry=registry,
        session=session,
        save_turn_msg=_save,
        stuck=stuck,
        tool_failures={},
    )


async def test_a_bash_edit_between_two_identical_runs_lets_the_rerun_execute():
    stuck = StuckDetector()
    saved: list = []
    gate = _make_gate(stuck, saved)

    run = _bash("python3 solve.py")
    edit = _bash("sed -i 's/n=10/n=200/' solve.py")

    assert await gate._dedup([run]) == [run]
    gate.remember_success("bash", run["arguments"], 1, "IndexError")

    assert await gate._dedup([edit]) == [edit]
    gate.remember_success("bash", edit["arguments"], 2, "")

    # The rerun must reach the executor — the edit changed what it does.
    assert await gate._dedup([run]) == [run]
    assert not [c for _, c, _ in saved if "already executed" in c]


async def test_an_exact_back_to_back_repeat_still_short_circuits():
    """The purge is scoped to OTHER bash entries; the no-op repeat case that
    dedup exists for keeps working."""
    stuck = StuckDetector()
    saved: list = []
    gate = _make_gate(stuck, saved)

    call = _bash("ls -la")
    assert await gate._dedup([call]) == [call]
    gate.remember_success("bash", call["arguments"], 1, "total 0")

    assert await gate._dedup([call]) == []
    assert any("already executed in round 1" in c for _, c, _ in saved)


async def test_a_file_edit_still_clears_cached_bash_results():
    stuck = StuckDetector()
    saved: list = []
    gate = _make_gate(stuck, saved)

    run = _bash("python3 solve.py")
    assert await gate._dedup([run]) == [run]
    gate.remember_success("bash", run["arguments"], 1, "IndexError")
    gate.remember_success("file_edit", '{"path": "solve.py"}', 2, "edited")

    assert await gate._dedup([run]) == [run]


def test_bash_bumps_the_mutation_epoch_so_edit_rerun_is_not_a_cycle():
    """Signal 2 must not fire on run → sed edit → same run."""
    assert "bash" in _MUTATING_TOOLS
    stuck = StuckDetector()
    registry = SimpleNamespace(exists=lambda name: True)

    run = [{"name": "bash", "arguments": json.dumps({"command": "python3 solve.py"})}]
    edit = [{"name": "bash", "arguments": json.dumps({"command": "sed -i 's/a/b/' solve.py"})}]

    stuck.evaluate("", run, {}, registry)
    stuck.mark_success(tool_name="bash", args={"command": "python3 solve.py"})
    stuck.evaluate("", edit, {}, registry)
    stuck.mark_success(tool_name="bash", args={"command": "sed -i 's/a/b/' solve.py"})
    score, _ = stuck.evaluate("", run, {}, registry)

    assert "tool_cycle" not in stuck.behavioral_flags
    assert score == 0.0


def test_a_true_bash_loop_still_trips_signal_two():
    """No mutation in between (the tool call never succeeded) — still a cycle."""
    stuck = StuckDetector()
    registry = SimpleNamespace(exists=lambda name: True)
    run = [{"name": "bash", "arguments": json.dumps({"command": "python3 solve.py"})}]

    stuck.evaluate("", run, {}, registry)
    score, _ = stuck.evaluate("", run, {}, registry)

    assert "tool_cycle" in stuck.behavioral_flags
    assert score >= 0.4
