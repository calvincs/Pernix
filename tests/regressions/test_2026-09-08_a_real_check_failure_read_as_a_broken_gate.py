"""A real check failure read as a gate that never ran (harness review 3.2.1, H06).

`_looks_unrunnable` classified any short non-zero output containing one of
seven generic phrases as a gate that could not start — and one of them was the
bare substring "not found". Measured at this tip, all of these were classified
BROKEN and vanished from `failing()`, which drives both the retry fallback and
reflect's verdict clamp: an app smoke check printing "expected output file
dist/report.json not found", an HTTP check printing "404 Not Found", a pytest
run whose tail contained "config key not found", a short FileNotFoundError
traceback, and "DB write check: permission denied". Exit 126/127 was broken on
the exit code alone, whatever the command had printed and however much of it —
so an application that exits 127 on purpose was a broken gate, and 12KB of
real test output was too.

The reuse path then dropped the classification it copied everything else
through, so a genuinely broken gate flipped back to FAILING on attempt 3 —
exactly the retry storm the classification exists to prevent.

Broken now means launch evidence: a spawn that failed, a shell diagnostic
naming the gate's own command word, a cd error from the launcher. Everything
else is the work failing, which is what a gate is for.
"""

from __future__ import annotations

import os

import pytest

from core import gates


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def _row(command: str, *, name: str = "smoke", cwd: str = "") -> dict:
    return {"name": name, "command": command, "cwd": cwd, "scope": "session", "watch_paths": []}


# ── every marker, inside a short REAL failure ────────────────────────────────


@pytest.mark.parametrize(
    "tail",
    [
        "ERROR: expected output file dist/report.json not found",
        "404 Not Found",
        "AssertionError: expected output file not found",
        "assert 'api_key' in {}, config key not found",
        "FileNotFoundError: [Errno 2] No such file or directory: 'data.csv'",
        "DB write check: permission denied",
        "check failed: /var/log/app is a directory, expected a file",
        "deploy smoke: cannot cd to the staging release, aborting",
        "gate helper: command not found in the plugin registry",
        "5 failed, 15 passed in 0.66s",
    ],
)
def test_a_short_real_failure_is_a_failure(tail):
    """Every phrase the old detector keyed on, in output an application wrote
    about its own work. The gate ran; the check failed."""
    assert gates._looks_unrunnable(1, tail, "pytest -q") is False


def test_a_short_traceback_is_a_real_failure():
    """182 characters, exit 1: the script ran and could not find its input."""
    tail = (
        "Traceback (most recent call last):\n"
        '  File "/app/data/workspace/load.py", line 1, in <module>\n'
        "    open('data.csv')\n"
        "FileNotFoundError: [Errno 2] No such file or directory: 'data.csv'"
    )
    assert gates._looks_unrunnable(1, tail, "python3 load.py") is False


def test_a_bare_nonzero_exit_is_still_a_real_failure():
    assert gates._looks_unrunnable(2, "", "pytest -q") is False


def test_long_output_is_a_real_failure_whatever_it_exits_with():
    """The exit code alone used to decide for 126/127, at any output size."""
    tail = "FAILED tests/test_load.py - FileNotFoundError: No such file or directory: 'data.csv'\n" * 150
    assert len(tail) > 12000
    assert gates._looks_unrunnable(127, tail, "make check") is False
    assert gates._looks_unrunnable(1, tail, "make check") is False


def test_an_application_may_exit_126_or_127_on_purpose():
    assert gates._looks_unrunnable(127, "suite: 127 checks failed", "bash run_suite.sh") is False
    assert gates._looks_unrunnable(126, "permission model check failed", "bash run_suite.sh") is False


def test_a_missing_command_that_is_not_the_gates_own_command_is_a_failure():
    """`make test` ran. Its recipe could not find python3 — that is the
    project's environment failing a check, not the gate failing to start."""
    assert gates._looks_unrunnable(127, "Makefile:4: python3: command not found", "make test") is False


# ── launch evidence still reads as broken ───────────────────────────────────


def test_the_field_case_is_still_classified_broken():
    """The Agent Mesh completion gate, registered before its directory existed."""
    tail = "/bin/sh: 1: cd: can't cd to /app/data/workspace/spaces/agent-mesh/impl"
    assert gates._looks_unrunnable(2, tail, "cd impl && pytest -q") is True


def test_the_bash_wording_of_the_same_cd_failure():
    tail = "/bin/bash: line 1: cd: /app/data/workspace/impl: No such file or directory"
    assert gates._looks_unrunnable(1, tail, "cd impl && pytest -q") is True


def test_a_missing_executable_is_broken(workspace):
    r = gates._run_one(_row("definitely_not_a_binary_xyz --version"), workspace, workspace, "")
    assert r.exit_code == 127
    assert r.broken is True
    assert r.state == "unavailable"


def test_a_gate_whose_cwd_does_not_exist_is_broken(workspace):
    """The spawn itself fails — structured launch evidence, no output at all."""
    r = gates._run_one(_row("pytest -q", cwd="impl/does/not/exist"), workspace, workspace, "")
    assert r.broken is True
    assert r.passed is False
    assert r.error


def test_a_cd_that_fails_inside_the_command_is_broken(workspace):
    r = gates._run_one(_row("cd no_such_dir && pytest -q"), workspace, workspace, "")
    assert r.exit_code != 0
    assert r.broken is True


def test_a_command_that_is_not_executable_is_broken(workspace):
    script = workspace / "noexec.sh"
    script.write_text("echo hi\n")
    os.chmod(script, 0o644)
    r = gates._run_one(_row("./noexec.sh"), workspace, workspace, "")
    assert r.exit_code == 126
    assert r.broken is True


def test_a_real_smoke_failure_runs_and_fails(workspace):
    """The counterpart the report filed: this must reach failing()."""
    (workspace / "verify.py").write_text(
        "import sys, pathlib\n"
        "if not pathlib.Path('dist/report.json').exists():\n"
        "    print('ERROR: expected output file dist/report.json not found'); sys.exit(1)\n"
    )
    r = gates._run_one(_row("python3 verify.py"), workspace, workspace, "")
    assert r.exit_code == 1
    assert r.broken is False
    assert r.state == "failed"
    assert gates.failing([r]) == [r]


# ── three states, carried everywhere ────────────────────────────────────────


def test_the_three_states_stay_apart():
    passed = gates.GateResult(name="g", command="c", passed=True, exit_code=0)
    failed = gates.GateResult(name="g", command="c", passed=False, exit_code=1)
    unavailable = gates.GateResult(name="g", command="c", passed=False, exit_code=127, broken=True)
    assert [r.state for r in (passed, failed, unavailable)] == ["passed", "failed", "unavailable"]
    assert gates.failing([passed, failed, unavailable]) == [failed]
    assert gates.broken([passed, failed, unavailable]) == [unavailable]
    assert unavailable.to_payload()["state"] == "unavailable"


def test_an_unavailable_check_is_never_read_as_success():
    unavailable = gates.GateResult(name="required-suite", command="pytest -q", passed=False, broken=True, exit_code=127)
    evidence = gates.format_evidence([unavailable])
    assert "BROKEN" in evidence
    assert "UNVERIFIED" in evidence
    assert "NOT a failure of this turn's work" in evidence
    assert unavailable.passed is False


# ── the reuse path keeps the whole state ────────────────────────────────────


def test_reuse_preserves_broken(monkeypatch, workspace):
    """attempt >= 3 reuses an unchanged failure. Copying exit_code and output
    but not `broken` flipped a broken gate back to FAILING — the retry storm
    the classification exists to stop, arriving two attempts later."""
    monkeypatch.setattr("config.settings.workspace_dir", str(workspace))
    (workspace / "impl").mkdir(exist_ok=True)
    (workspace / "impl" / "src.py").write_text("x = 1\n")

    from db import models as db

    sid = "gate-reuse-session"
    db.add_gate(sid, "cd-broken", "cd /does/not/exist && pytest -q", watch_paths=["impl"], cwd="")

    first = gates.run_gates(sid, {}, attempt=1)
    assert [g.broken for g in first] == [True]

    prior = {g.name: (g.fingerprint, g) for g in first if g.fingerprint}
    second = gates.run_gates(sid, prior, attempt=3)
    assert [g.reused for g in second] == [True]
    assert [g.broken for g in second] == [True]
    assert second[0].error == first[0].error
    assert gates.failing(second) == []
    assert [g.name for g in gates.broken(second)] == ["cd-broken"]


def test_reuse_still_carries_a_real_failure(monkeypatch, workspace):
    monkeypatch.setattr("config.settings.workspace_dir", str(workspace))
    (workspace / "impl").mkdir(exist_ok=True)
    (workspace / "impl" / "src.py").write_text("x = 1\n")

    from db import models as db

    sid = "gate-reuse-real-session"
    db.add_gate(sid, "real-fail", "echo '1 failed'; exit 1", watch_paths=["impl"], cwd="")

    first = gates.run_gates(sid, {}, attempt=1)
    assert [g.broken for g in first] == [False]
    prior = {g.name: (g.fingerprint, g) for g in first if g.fingerprint}
    second = gates.run_gates(sid, prior, attempt=3)
    assert [g.reused for g in second] == [True]
    assert [g.name for g in gates.failing(second)] == ["real-fail"]
    assert gates.broken(second) == []


def test_a_broken_gate_still_reaches_the_agent(workspace):
    r = gates._run_one(_row("definitely_not_a_binary_xyz", name="impl-suite"), workspace, workspace, "")
    notice = gates.format_broken_notice([r])
    assert "impl-suite" in notice
    assert "could NOT RUN" in notice
    assert "definitely_not_a_binary_xyz" in notice
