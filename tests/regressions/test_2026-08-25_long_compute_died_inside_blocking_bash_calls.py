"""Heavy compute had no home: blocking bash calls burned 15+ timeouts across
the ARC-3 campaign (300s/600s/900s/1800s), agents hand-rolled nohup+pkill
cycles, and their own reflects condemned it ("burned tool rounds without
converging", "ran unbounded and produced no output").

job_start/job_status/job_tail/job_kill make detached compute first-class:
output captured to a log, completion durable via an exit-code sidecar (so a
server restart cannot orphan the answer), wall-clock capped by coreutils
timeout, whole-group kill, and per-session concurrency caps. job_status and
job_tail register idempotent=False so their time-varying answers are never
served from the cross-round dedup cache.
"""

import time

from core.tools.builtin import jobs_tool
from db import models as db


def _ctx():
    sid = db.create_session(title="jobs test")
    return {"session_id": sid}


def _wait_state(job_id, want, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = jobs_tool._refresh(db.get_job(job_id))
        if job["state"] == want:
            return job
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} never reached {want}: {db.get_job(job_id)}")


def _extract_id(start_result):
    assert start_result.startswith("Job started: "), start_result
    return start_result.split()[2]


def test_job_runs_detached_and_completes_with_output():
    ctx = _ctx()
    r = jobs_tool.job_start("echo begin; echo progress 1; echo done", _context=ctx)
    job_id = _extract_id(r)
    job = _wait_state(job_id, "done")
    assert job["exit_code"] == 0
    tail = jobs_tool.job_tail(job_id, _context=ctx)
    assert "begin" in tail and "done" in tail
    status = jobs_tool.job_status(job_id, _context=ctx)
    assert "state=done" in status
    assert "[as of " in status  # timestamp defeats dedup caching


def test_job_kill_terminates_the_group():
    ctx = _ctx()
    job_id = _extract_id(jobs_tool.job_start("sleep 60", _context=ctx))
    out = jobs_tool.job_kill(job_id, _context=ctx)
    assert "killed" in out
    assert db.get_job(job_id)["state"] == "killed"


def test_wall_clock_cap_reads_as_timeout():
    ctx = _ctx()
    job_id = _extract_id(jobs_tool.job_start("sleep 30", wall_seconds=1, _context=ctx))
    job = _wait_state(job_id, "timeout", timeout=20.0)
    assert job["exit_code"] == 124


def test_concurrency_cap_blocks_a_fourth_job(monkeypatch):
    monkeypatch.setattr("config.settings.jobs_max_concurrent", 2)
    ctx = _ctx()
    ids = [_extract_id(jobs_tool.job_start("sleep 30", _context=ctx)) for _ in range(2)]
    refused = jobs_tool.job_start("echo never", _context=ctx)
    assert refused.startswith("Error:") and "already running" in refused
    for jid in ids:
        jobs_tool.job_kill(jid, _context=ctx)


def test_vanished_pid_without_exit_file_reads_as_lost():
    ctx = _ctx()
    job_id = _extract_id(jobs_tool.job_start("sleep 60", _context=ctx))
    job = db.get_job(job_id)
    # Simulate a server restart racing the wrapper: kill the group directly
    # (no exit sidecar gets written by us) and blank the sidecar if any.
    import os
    import signal as _signal

    try:
        os.killpg(job["pid"], _signal.SIGKILL)
    except ProcessLookupError:
        pass
    deadline = time.time() + 5
    while time.time() < deadline and jobs_tool._pid_alive(job["pid"]):
        time.sleep(0.05)
    from pathlib import Path

    exit_file = Path(job["log_path"]).parent / "exit_code"
    if exit_file.exists():
        exit_file.unlink()
    job = jobs_tool._refresh(db.get_job(job_id))
    assert job["state"] == "lost"


def test_status_and_tail_register_non_idempotent():
    from core.tools.registry import ToolRegistry

    reg = ToolRegistry()
    jobs_tool.register(reg)
    assert reg.get("job_status").idempotent is False
    assert reg.get("job_tail").idempotent is False
    assert reg.get("job_start") is not None and reg.get("job_kill") is not None


def test_bash_timeout_error_points_at_job_start(monkeypatch):
    """Field case (cn04 retest, 2026-08-25): two solver timeouts — 600s with
    partial output and a full 1800s — and the agent never considered
    job_start. The scout-time LONG COMPUTE rule doesn't reach the moment of
    need; the timeout error itself now carries the pointer."""
    monkeypatch.setattr("config.settings.jobs_enabled", True)
    from core.tools.builtin.core_tools import bash

    out = bash("sleep 30", timeout=1, _context={"session_id": "timeout-hint-test"})
    assert "timed out" in out
    assert "job_start" in out

    monkeypatch.setattr("config.settings.jobs_enabled", False)
    out = bash("sleep 30", timeout=1, _context={"session_id": "timeout-hint-test"})
    assert "timed out" in out
    assert "job_start" not in out
