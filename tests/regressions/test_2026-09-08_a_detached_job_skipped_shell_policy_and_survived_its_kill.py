"""job_start was a second shell launcher with none of bash's guarantees.

Two halves, both measured at f16007f.

Policy: core_tools.py:794 was the whole of bash's command admission, and
job_start had no equivalent — no denylist scan, no allowlist check, no
reference to shell_security_mode. `crontab -l` was refused by bash and ran as
a job; so did a redirect into /etc (only the OS refused the write); in strict
mode `perl -e ...` was refused by bash and exited 0 as a job. A job runs the
same shell in the same environment with a two-hour leash instead of a
ten-minute one, so this made the policy advisory.

Kill: the wrapper is `timeout -k 10 N bash -c CMD`, and GNU timeout calls
setpgid on itself, so the workload lives in timeout's process group, not the
leader's. job_kill sent SIGTERM to the leader's group and escalated on
leader liveness alone, so after a "killed" report the leader was gone and the
work was still running — no TERM resistance required, an ordinary sleep loop
survived. Four consequences: the job ran on to its wall cap, the exit sidecar
was never written, the concurrency cap freed a slot to a live predecessor,
and _refresh labelled the survivor 'lost'.

The old test passed on a timing accident — it killed microseconds after
launch, before the leader had forked timeout. Every kill here waits first.
"""

import json
import os
import signal
import time
from pathlib import Path

import pytest

from core.tools.builtin import jobs_tool
from core.tools.builtin.core_tools import bash
from db import models as db

# ---------------------------------------------------------------------------
# /proc walkers, deliberately independent of the implementation's own scan
# ---------------------------------------------------------------------------


def _stat(pid: int) -> dict | None:
    try:
        raw = open(f"/proc/{pid}/stat").read()
        f = raw.rsplit(")", 1)[1].split()
        return {"state": f[0], "ppid": int(f[1]), "pgid": int(f[2]), "sid": int(f[3]), "start": int(f[19])}
    except (OSError, IndexError, ValueError):
        return None


def _live(pid: int) -> bool:
    st = _stat(pid)
    return bool(st) and st["state"] != "Z"


def _descendants(root: int) -> list[tuple[int, int]]:
    """(pid, start) for every live descendant of root, by ppid links."""
    children: dict[int, list[int]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        st = _stat(int(entry))
        if st:
            children.setdefault(st["ppid"], []).append(int(entry))
    found, stack = [], [root]
    while stack:
        for kid in children.get(stack.pop(), []):
            st = _stat(kid)
            if st and st["state"] != "Z":
                found.append((kid, st["start"]))
            stack.append(kid)
    return sorted(found)


def _survivors(snapshot: list[tuple[int, int]]) -> list[int]:
    """Which of a snapshot are still the same live process (start time pins
    the identity, so a recycled pid never reads as a survivor)."""
    out = []
    for pid, start in snapshot:
        st = _stat(pid)
        if st and st["state"] != "Z" and st["start"] == start:
            out.append(pid)
    return out


def _session_pids(sid: int) -> list[int]:
    out = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        st = _stat(int(entry))
        if st and st["sid"] == sid and st["state"] != "Z":
            out.append(int(entry))
    return out


def _wait_state(job_id: str, want: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = jobs_tool._refresh(db.get_job(job_id))
        if job["state"] == want:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never reached {want}: {db.get_job(job_id)}")


def _wait_file(path: Path, timeout: float = 10.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            text = path.read_text().strip()
            if text:
                return text
        except OSError:
            pass
        time.sleep(0.05)
    raise AssertionError(f"{path} never appeared")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _stub_workspace_venv():
    """build_shell_env forks `python -m venv` when the workspace has none, and
    every job in this file would pay for it. A stub binary is enough."""
    from core.tools.paths import workspace

    venv_bin = workspace() / ".venv" / "bin"
    venv_bin.mkdir(parents=True, exist_ok=True)
    stub = venv_bin / "python"
    stub.write_text('#!/bin/sh\nexec /usr/bin/env python3 "$@"\n')
    stub.chmod(0o755)


class _Jobs:
    """Launches jobs and remembers what to sweep. Only ever signals sessions
    this test created."""

    def __init__(self):
        self.ctx = {"session_id": db.create_session(title="h18 jobs")}
        self.leaders: list[int] = []

    def raw(self, command: str, **kw) -> str:
        return jobs_tool.job_start(command, _context=self.ctx, **kw)

    def start(self, command: str, **kw) -> str:
        out = self.raw(command, **kw)
        assert out.startswith("Job started: "), out
        job_id = out.split()[2]
        self.leaders.append(int(db.get_job(job_id)["pid"]))
        return job_id

    def leader(self, job_id: str) -> int:
        return int(db.get_job(job_id)["pid"])

    def sweep(self) -> None:
        for leader in self.leaders:
            for pid in _session_pids(leader):  # sid == leader pid: our own session
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass


@pytest.fixture
def jobs():
    j = _Jobs()
    try:
        yield j
    finally:
        j.sweep()


# ---------------------------------------------------------------------------
# Half one: the same command policy through both launchers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode,command",
    [
        ("permissive", "crontab -l"),
        ("permissive", "echo probe > /etc/pernix_h18_regression"),
        ("strict", "perl -e 'print 1'"),
        ("strict", "crontab -l"),
    ],
)
def test_a_command_bash_refuses_is_refused_by_job_start(monkeypatch, jobs, mode, command):
    monkeypatch.setattr("config.settings.shell_security_mode", mode)
    from_bash = bash(command, timeout=5, _context=jobs.ctx)
    from_job = jobs.raw(command, wall_seconds=10)

    assert from_bash.startswith("Error:"), from_bash
    assert not from_job.startswith("Job started"), from_job
    # Same refusal, word for word: the agent's next move should not depend on
    # which launcher it happened to reach for.
    assert from_job == from_bash
    assert not Path("/etc/pernix_h18_regression").exists()


@pytest.mark.parametrize("mode", ["permissive", "strict"])
def test_a_command_bash_admits_still_runs_as_a_job(monkeypatch, jobs, mode):
    monkeypatch.setattr("config.settings.shell_security_mode", mode)
    bash_out = bash("echo ok-in-bash", timeout=10, _context=jobs.ctx)
    # bash returns (text, metadata) once a process launches (H05, 084c16a).
    assert "ok-in-bash" in (bash_out[0] if isinstance(bash_out, tuple) else bash_out)

    job_id = jobs.start("echo ok-in-job", wall_seconds=30)
    assert _wait_state(job_id, "done")["exit_code"] == 0
    assert "ok-in-job" in jobs_tool.job_tail(job_id, _context=jobs.ctx)


def test_an_empty_command_is_refused_the_same_way(jobs):
    assert bash("   ", _context=jobs.ctx) == "Error: Empty command"
    assert jobs.raw("   ") == "Error: Empty command"


# ---------------------------------------------------------------------------
# Half two: the kill reaches the work, not just the bookkeeping
# ---------------------------------------------------------------------------


def test_job_kill_reaches_the_child_and_the_grandchild(jobs, tmp_path):
    ticks = tmp_path / "ticks"
    child_pid_file = tmp_path / "child.pid"
    job_id = jobs.start(
        f"bash -c 'sleep 45 & echo $! > {child_pid_file}; " f"while :; do echo t >> {ticks}; sleep 0.2; done'",
        wall_seconds=60,
    )
    leader = jobs.leader(job_id)

    # The defect hid in the launch race: kill inside the first few
    # microseconds and the leader has not forked `timeout` yet. Wait.
    grandchild = int(_wait_file(child_pid_file))
    time.sleep(0.6)
    before = _descendants(leader)
    assert len(before) >= 3, f"expected timeout + inner shell + child, got {before}"
    assert grandchild in [pid for pid, _ in before]
    ticked = ticks.read_text().count("t")
    assert ticked > 0, "the job never did any work to interrupt"

    out = jobs_tool.job_kill(job_id, _context=jobs.ctx)

    assert "killed" in out, out
    assert _survivors(before) == [], f"{out}\nsurvivors: {_survivors(before)}"
    assert not _live(grandchild)
    frozen = ticks.read_text().count("t")
    time.sleep(0.6)
    assert ticks.read_text().count("t") == frozen, "the work carried on after 'killed'"
    assert db.get_job(job_id)["state"] == "killed"


def test_a_killed_job_still_records_an_exit_code(jobs):
    job_id = jobs.start("sleep 45", wall_seconds=60)
    time.sleep(0.6)

    out = jobs_tool.job_kill(job_id, _context=jobs.ctx)

    # The wrapper is spared on the first pass precisely so it outlives its
    # child and writes the sidecar; 143 is SIGTERM.
    assert "exit 143" in out, out
    assert db.get_job(job_id)["exit_code"] == 143
    assert db.get_job(job_id)["state"] == "killed"


def test_a_wrapper_that_exits_first_still_gets_its_orphans_killed(jobs, tmp_path):
    """The command backgrounds its work, so the inner shell, `timeout` and the
    wrapper all exit at once and the row reads done, exit 0 — while the work
    runs on, reparented to init and outside every ppid link the harness has."""
    child_pid_file = tmp_path / "orphan.pid"
    job_id = jobs.start(f"sleep 45 & echo $! > {child_pid_file}", wall_seconds=60)
    orphan = int(_wait_file(child_pid_file))

    finished = _wait_state(job_id, "done")
    assert finished["exit_code"] == 0
    assert not jobs_tool._pid_alive(jobs.leader(job_id))
    assert _live(orphan), "test needs a live orphan to sweep"

    out = jobs_tool.job_kill(job_id, _context=jobs.ctx)

    assert "left behind" in out, out
    assert not _live(orphan), out


def test_a_dead_wrapper_with_live_work_does_not_read_as_lost(jobs):
    job_id = jobs.start("bash -c 'sleep 45'", wall_seconds=60)
    leader = jobs.leader(job_id)
    time.sleep(0.6)
    before = _descendants(leader)
    assert before, "test needs descendants outside the leader's group"

    # Kill the wrapper's own group only — every pid in it is one we launched.
    # No exit sidecar gets written, which is what used to read as 'lost'.
    os.killpg(leader, signal.SIGKILL)
    deadline = time.time() + 5
    while time.time() < deadline and jobs_tool._pid_alive(leader):
        time.sleep(0.05)

    assert jobs_tool._refresh(db.get_job(job_id))["state"] == "running"

    out = jobs_tool.job_kill(job_id, _context=jobs.ctx)

    assert "killed" in out and "exit code unrecorded" in out, out
    assert _survivors(before) == [], out


def test_the_wall_cap_takes_the_descendants_with_it(jobs):
    job_id = jobs.start("bash -c 'sleep 45'", wall_seconds=1)
    leader = jobs.leader(job_id)
    time.sleep(0.6)
    before = _descendants(leader)
    assert before

    job = _wait_state(job_id, "timeout")

    assert job["exit_code"] == 124
    assert _survivors(before) == []


def test_a_killed_job_frees_its_slot_only_because_the_work_is_over(monkeypatch, jobs):
    """The cap counts rows, so a kill that killed nothing let a replacement
    start against a live predecessor — same port, same files."""
    monkeypatch.setattr("config.settings.jobs_max_concurrent", 1)
    job_id = jobs.start("bash -c 'sleep 45'", wall_seconds=60)
    time.sleep(0.6)
    before = _descendants(jobs.leader(job_id))

    refused = jobs.raw("echo second", wall_seconds=30)
    assert refused.startswith("Error:") and "already running" in refused

    jobs_tool.job_kill(job_id, _context=jobs.ctx)
    assert _survivors(before) == [], "slot freed while the predecessor was still running"

    replacement = jobs.start("echo replacement", wall_seconds=30)
    assert _wait_state(replacement, "done")["exit_code"] == 0


def test_a_reused_pid_is_never_signalled(jobs):
    """Identity, not liveness: a pid that has been handed to somebody else
    must not be signalled just because it answers."""
    job_id = jobs.start("bash -c 'sleep 45'", wall_seconds=60)
    time.sleep(0.6)
    before = _descendants(jobs.leader(job_id))
    assert before

    record = Path(db.get_job(job_id)["log_path"]).parent / "containment"
    stored = json.loads(record.read_text())
    assert stored["sid"] == jobs.leader(job_id)
    stored["leader_start"] += 100_000  # as if the pid had been recycled since
    record.write_text(json.dumps(stored))

    out = jobs_tool.job_kill(job_id, _context=jobs.ctx)

    assert "identity was reused" in out, out
    assert sorted(_survivors(before)) == sorted(pid for pid, _ in before), out
    assert db.get_job(job_id)["state"] == "lost"
