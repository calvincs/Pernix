"""Agent Mesh build, 2026-09-07/08: two gate defects, one turn apart.

The completion gate was registered before the directory it pointed at
existed, so it exited 2 with `/bin/sh: 1: cd: can't cd to ...` at the end of
that same turn. A gate that never ran was counted as a failing check: it
forced three retry turns in the middle of an active build and mechanically
blocked a pass verdict, so three turns of real progress were graded escalate.

Then the successor session had no gate tools at all. It probed for an HTTP
gate endpoint, found none, and wrote "no such gate API exists on this
deployment" into the project's own TASKS.md — a false claim, now in the docs.
"""

from __future__ import annotations

from core import gates


def _result(**kw):
    base = {"name": "impl-suite", "command": "cd impl && pytest -q", "passed": False}
    base.update(kw)
    return gates.GateResult(**base)


# ── a gate that could not run ────────────────────────────────────────────────


def test_the_field_case_is_classified_broken():
    """Verbatim shell output from the failing impl-suite gate."""
    assert gates._looks_unrunnable(2, "/bin/sh: 1: cd: can't cd to /app/data/workspace/spaces/agent-mesh/impl")


def test_command_not_found_is_broken():
    assert gates._looks_unrunnable(127, "gate: command not found")


def test_a_real_test_failure_is_not_broken():
    assert not gates._looks_unrunnable(1, "5 failed, 15 passed in 0.66s")


def test_a_long_output_mentioning_a_missing_file_is_a_real_failure():
    """A test run whose output happens to say 'no such file or directory' is
    failing work, not a broken gate — only a short output is scanned."""
    tail = "FAILED tests/test_load.py - FileNotFoundError: No such file or directory: 'data.csv'\n" * 20
    assert not gates._looks_unrunnable(1, tail)


def test_a_bare_nonzero_exit_is_a_real_failure():
    assert not gates._looks_unrunnable(2, "")


# ── what that classification changes ─────────────────────────────────────────


def test_a_broken_gate_does_not_count_as_failing():
    """failing() drives both the retry fallback and the reflect verdict clamp."""
    results = [_result(exit_code=2, broken=True)]
    assert gates.failing(results) == []
    assert len(gates.broken(results)) == 1


def test_a_genuinely_failing_gate_still_counts():
    results = [_result(exit_code=1, output_tail="1 failed, 33 passed")]
    assert len(gates.failing(results)) == 1
    assert gates.broken(results) == []


def test_a_refused_gate_is_broken_not_failing():
    """Policy refusal means the command never executed either."""
    row = {"name": "g", "command": "rm -rf /", "scope": "session"}
    refused = gates._refused(row, "", "Error: blocked by security policy")
    assert refused.broken is True
    assert gates.failing([refused]) == []


# ── the agent still has to hear about it ─────────────────────────────────────


def test_the_broken_notice_names_the_gate_and_how_to_fix_it():
    notice = gates.format_broken_notice([_result(exit_code=2, broken=True, output_tail="can't cd to /x")])
    assert "impl-suite" in notice
    assert "could NOT RUN" in notice
    assert "add_gate" in notice


def test_no_notice_when_every_gate_ran():
    assert gates.format_broken_notice([_result(passed=True, exit_code=0)]) == ""


def test_reflect_evidence_tells_the_grader_not_to_blame_the_turn():
    evidence = gates.format_evidence([_result(exit_code=2, broken=True)])
    assert "BROKEN" in evidence
    assert "NOT a failure of this turn's work" in evidence


# ── the mechanism is findable ────────────────────────────────────────────────


def test_the_empty_gate_list_points_at_add_gate_and_says_gates_are_per_session():
    """The successor session wrote a false claim into TASKS.md because nothing
    told it gates exist or that they do not carry across sessions."""
    from core.extensions.evaluation import list_gates

    text = list_gates(_context={"session_id": "no-such-session"})
    assert "add_gate" in text
    assert "per-session" in text


def test_list_gates_is_forced_into_the_tool_surface():
    import inspect

    from core import agent

    src = inspect.getsource(agent._resolve_tool_surface)
    assert 'active.add("list_gates")' in src
    assert "settings.gates_enabled" in src
