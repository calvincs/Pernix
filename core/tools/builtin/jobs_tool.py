"""Pernix — Background job manager: job_start / job_status / job_tail / job_kill.

Born from the ARC-3 campaign post-mortems: heavy searches (1-2M state BFS,
CP-SAT solves, big builds) need minutes of wall clock, but a blocking bash
call burns a tool round per wait and dies at its timeout with the work lost.
Agents hand-rolled nohup+pkill cycles and their own reflects condemned it
("burned tool rounds without converging", "ran unbounded and produced no
output"). These tools make that shape first-class:

- job_start launches the command DETACHED (setsid; survives turn end and
  cancellation) with output streaming to a log file the whole toolchain can
  read, a hard wall-clock cap enforced by coreutils `timeout`, and the same
  RLIMIT_AS/RLIMIT_FSIZE caps as bash.
- job_status / job_tail are cheap polls: state, elapsed, CPU, RSS, and paged
  output. Their results carry a timestamp line so identical-looking polls
  never collapse into the cross-round dedup cache.
- job_kill terminates the whole process group.

Durability: rows live in the sessions DB. The exit code is written by the
wrapper shell to a sidecar file, so completion is detectable even after a
server restart (when the Popen handle is long gone). A job whose pid vanished
without an exit file reads as 'lost'.
"""

from __future__ import annotations

import logging
import os
import shlex
import signal
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from config import settings

logger = logging.getLogger("pernix.tools.jobs")

_LOG_READ_CAP = 50_000  # chars per job_tail call, mirroring bash output cap
_STATUS_TAIL_LINES = 5
_KILL_GRACE_S = 2.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jobs_root() -> Path:
    from core.tools.paths import workspace

    root = workspace() / ".jobs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _rlimits():
    """(RLIMIT_AS, RLIMIT_FSIZE) from settings — one knob governs bash,
    kernel children, and jobs alike."""
    as_limit = int(getattr(settings, "shell_address_space_limit_bytes", 0) or 0)
    fsize = int(getattr(settings, "shell_fsize_limit_bytes", 0) or 0)
    return as_limit, fsize


def _preexec():
    """Child setup: rlimits + nice. setsid comes from start_new_session."""
    import resource

    as_limit, fsize = _rlimits()
    try:
        if as_limit > 0:
            resource.setrlimit(resource.RLIMIT_AS, (as_limit, as_limit))
    except (ValueError, OSError):
        pass  # macOS rejects RLIMIT_AS in a forked child
    try:
        if fsize > 0:
            resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))
    except (ValueError, OSError):
        pass
    try:
        os.nice(10)
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    """True for a live, non-zombie pid. A job killed externally becomes a
    zombie child of THIS server process (we spawned it and nothing waits on
    it), and os.kill(pid, 0) succeeds on zombies — so without the state
    check a dead job would read 'running' forever. Zombies are reaped
    opportunistically here."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        state = open(f"/proc/{pid}/stat").read().split()[2]
    except (OSError, IndexError):
        return True  # alive but unreadable — assume running
    if state == "Z":
        try:
            os.waitpid(pid, os.WNOHANG)  # reap if it is our child
        except ChildProcessError:
            pass
        return False
    return True


def _proc_stats(pid: int) -> tuple[str, str]:
    """(rss_human, cpu_human) for a live pid; empty strings when unreadable."""
    rss = cpu = ""
    try:
        for line in open(f"/proc/{pid}/status"):
            if line.startswith("VmRSS:"):
                kb = int(line.split()[1])
                rss = f"{kb / 1024:.0f}MB" if kb < 1024 * 1024 else f"{kb / 1024 / 1024:.1f}GB"
                break
    except OSError:
        pass
    try:
        parts = open(f"/proc/{pid}/stat").read().split()
        ticks = int(parts[13]) + int(parts[14])
        cpu = f"{ticks / os.sysconf('SC_CLK_TCK'):.0f}s cpu"
    except (OSError, ValueError, IndexError):
        pass
    return rss, cpu


def _refresh(job: dict) -> dict:
    """Reconcile a DB row against reality (exit file, pid). Returns the
    up-to-date row, persisting any state change."""
    from db import models as db

    if job["state"] != "running":
        return job
    exit_file = Path(job["log_path"]).parent / "exit_code"
    if exit_file.exists():
        try:
            code = int(exit_file.read_text().strip() or "1")
        except ValueError:
            code = 1
        state = "timeout" if code == 124 else ("done" if code == 0 else "failed")
        db.update_job(job["id"], state=state, exit_code=code, finished_at=_now_iso())
        job.update(state=state, exit_code=code)
        return job
    if not _pid_alive(job["pid"]):
        # Died without writing an exit code — killed externally or lost to a
        # server restart racing the wrapper's last write.
        db.update_job(job["id"], state="lost", finished_at=_now_iso())
        job.update(state="lost")
    return job


def _tail_lines(log_path: str, n: int) -> list[str]:
    try:
        with open(log_path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 20_000))
            chunk = fh.read().decode("utf-8", errors="replace")
        lines = chunk.splitlines()
        return lines[-n:] if lines else []
    except OSError:
        return []


def _elapsed(job: dict) -> str:
    try:
        start = datetime.fromisoformat(job["created_at"])
        end = datetime.fromisoformat(job["finished_at"]) if job.get("finished_at") else datetime.now(timezone.utc)
        s = int((end - start).total_seconds())
        return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"
    except (ValueError, TypeError, KeyError):
        return "?"


def _format_job(job: dict, verbose: bool = False) -> str:
    rss, cpu = ("", "")
    if job["state"] == "running":
        rss, cpu = _proc_stats(job["pid"])
    bits = [
        f"[{job['id']}] {job.get('name') or job['command'][:40]}",
        f"state={job['state']}",
        f"elapsed={_elapsed(job)}",
    ]
    if job.get("exit_code") is not None:
        bits.append(f"exit={job['exit_code']}")
    if rss:
        bits.append(rss)
    if cpu:
        bits.append(cpu)
    line = " | ".join(bits)
    if not verbose:
        return line
    tail = _tail_lines(job["log_path"], _STATUS_TAIL_LINES)
    if tail:
        line += "\n  last output:\n" + "\n".join(f"    {t}" for t in tail)
    else:
        line += "\n  (no output yet)"
    line += f"\n  full log: job_tail(job_id='{job['id']}')  file: {job['log_path']}"
    return line


def job_start(
    command: str,
    name: str = "",
    wall_seconds: int | None = None,
    _context: dict | None = None,
) -> str:
    """Start a detached background job."""
    from db import models as db

    if not settings.jobs_enabled:
        return "Error: background jobs are disabled (settings.jobs_enabled)."
    session_id = (_context or {}).get("session_id", "") or "unknown"

    running = [j for j in db.list_jobs(session_id=session_id, limit=50) if _refresh(j)["state"] == "running"]
    cap = int(settings.jobs_max_concurrent)
    if len(running) >= cap:
        listing = "\n".join(_format_job(j) for j in running)
        return f"Error: {cap} job(s) already running for this session — finish or job_kill one first:\n{listing}"

    try:
        requested = int(wall_seconds or 0)
    except (TypeError, ValueError):
        requested = 0
    timeout_s = (
        min(requested, int(settings.jobs_max_timeout_s)) if requested > 0 else int(settings.jobs_default_timeout_s)
    )

    job_id = uuid.uuid4().hex[:12]
    job_dir = _jobs_root() / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    log_path = job_dir / "output.log"
    exit_file = job_dir / "exit_code"

    # coreutils `timeout` gives a thread-free hard cap (exit 124); the
    # wrapper's final echo makes completion durable across server restarts.
    wrapped = f"timeout -k 10 {timeout_s} bash -c {shlex.quote(command)}; " f"echo $? > {shlex.quote(str(exit_file))}"

    from core.tools.paths import build_shell_env, workspace

    # Same environment bash gets — venv on PATH, VIRTUAL_ENV set, env-mode
    # filter applied. A bare os.environ.copy() left jobs on the system
    # python, so `python3 script.py` in a job failed to import packages the
    # agent had just installed in bash (session 3dc5a307d751: sympy).
    # HOME tracks the job's cwd below, which is the workspace root.
    job_cwd = workspace()
    env = build_shell_env(job_cwd, job_cwd)

    try:
        with open(log_path, "ab") as log_fh:
            proc = subprocess.Popen(
                ["/bin/bash", "-c", wrapped],
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=str(job_cwd),
                env=env,
                start_new_session=True,  # own group; survives our cleanup paths
                preexec_fn=_preexec,
            )
    except OSError as e:
        return f"Error: failed to start job: {e}"

    db.create_job(
        job_id=job_id,
        session_id=session_id,
        name=name or "",
        command=command[:2000],
        pid=proc.pid,
        log_path=str(log_path),
        deadline_s=timeout_s,
    )
    logger.info("job %s started (session %s, pid %d, cap %ds)", job_id, session_id[:12], proc.pid, timeout_s)
    return (
        f"Job started: {job_id} (pid {proc.pid}, wall cap {timeout_s}s).\n"
        f"It runs detached — keep working and poll with job_status('{job_id}') "
        f"or job_tail('{job_id}'). Output streams to {log_path}."
    )


def job_status(job_id: str = "", _context: dict | None = None) -> str:
    """Status of one job, or all of this session's recent jobs."""
    from db import models as db

    stamp = f"[as of {_now_iso()[11:19]}Z]"
    if job_id:
        job = db.get_job(job_id)
        if job is None:
            return f"Error: no job '{job_id}'"
        return f"{stamp}\n{_format_job(_refresh(job), verbose=True)}"
    session_id = (_context or {}).get("session_id", "") or "unknown"
    jobs = db.list_jobs(session_id=session_id, limit=10)
    if not jobs:
        return f"{stamp}\nNo jobs for this session. Start one with job_start(command)."
    return stamp + "\n" + "\n".join(_format_job(_refresh(j)) for j in jobs)


def job_tail(job_id: str, offset: int = 0, _context: dict | None = None) -> str:
    """Read job output from a byte offset (paged, complete-lines)."""
    from db import models as db

    job = db.get_job(job_id)
    if job is None:
        return f"Error: no job '{job_id}'"
    job = _refresh(job)
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0
    try:
        with open(job["log_path"], "rb") as fh:
            fh.seek(offset)
            data = fh.read(_LOG_READ_CAP)
            end = fh.seek(0, os.SEEK_END)
    except OSError:
        return f"[{job_id}] state={job['state']} — no output yet"
    text = data.decode("utf-8", errors="replace")
    header = f"[{job_id}] state={job['state']} elapsed={_elapsed(job)} bytes {offset}-{offset + len(data)} of {end} [as of {_now_iso()[11:19]}Z]"
    if offset + len(data) < end:
        header += f"\n(more available: job_tail('{job_id}', offset={offset + len(data)}))"
    return header + "\n" + (text if text else "(no output yet)")


def job_kill(job_id: str, _context: dict | None = None) -> str:
    """Terminate a job's whole process group (SIGTERM, then SIGKILL)."""
    from db import models as db

    job = db.get_job(job_id)
    if job is None:
        return f"Error: no job '{job_id}'"
    job = _refresh(job)
    if job["state"] != "running":
        return f"Job {job_id} is not running (state={job['state']})."
    pid = job["pid"]
    try:
        os.killpg(pid, signal.SIGTERM)  # start_new_session → pgid == pid
    except ProcessLookupError:
        db.update_job(job_id, state="lost", finished_at=_now_iso())
        return f"Job {job_id} was already gone."
    except PermissionError as e:
        return f"Error: could not signal job {job_id}: {e}"
    deadline = time.time() + _KILL_GRACE_S
    while time.time() < deadline and _pid_alive(pid):
        time.sleep(0.1)
    if _pid_alive(pid):
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    db.update_job(job_id, state="killed", finished_at=_now_iso())
    logger.info("job %s killed (pid %d)", job_id, pid)
    return f"Job {job_id} killed."


def register(reg) -> None:
    if not settings.jobs_enabled:
        return
    reg.register(
        name="job_start",
        func=job_start,
        description=(
            "Start a long-running shell command as a DETACHED background job "
            "(cwd=data/workspace). Use this instead of a blocking bash call for "
            "heavy compute that needs minutes: solvers, brute-force searches, "
            "builds, dataset crunching. The job survives the end of your turn; "
            "output streams to a log file. Returns a job_id — keep working and "
            "poll job_status(job_id) or job_tail(job_id). Wall-clock capped "
            "(default 2h, exit 124 on timeout); same memory cap as bash. Print "
            "progress lines in your command so polls show advancement."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run detached"},
                "name": {"type": "string", "description": "Optional short label shown in listings"},
                "wall_seconds": {
                    "type": "integer",
                    "description": "Optional wall-clock cap for the JOB in seconds (default 7200, max 21600). Named wall_seconds, not timeout: the tool call itself returns instantly.",
                },
            },
            "required": ["command"],
        },
        category="core",
        tags=["job", "background", "detach", "long", "compute", "solver", "search", "async", "parallel"],
        timeout=30,
        parallel_safe=False,
        safety_level="caution",
    )
    reg.register(
        name="job_status",
        func=job_status,
        description=(
            "Check background jobs: state (running/done/failed/timeout/killed/"
            "lost), elapsed, CPU, memory, exit code, and the last output lines. "
            "No job_id lists this session's recent jobs. Cheap — poll between "
            "other work instead of blocking."
        ),
        parameters={
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "Job to inspect; omit to list this session's jobs"},
            },
        },
        category="core",
        tags=["job", "background", "status", "poll", "check", "progress"],
        timeout=15,
        parallel_safe=True,
        idempotent=False,  # time-varying answers must never dedup-cache
    )
    reg.register(
        name="job_tail",
        func=job_tail,
        description=(
            "Read a background job's captured output from a byte offset "
            "(50KB pages; the result names the next offset). Works while the "
            "job runs and after it finishes."
        ),
        parameters={
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "Job whose output to read"},
                "offset": {"type": "integer", "description": "Byte offset to start from (default 0)"},
            },
            "required": ["job_id"],
        },
        category="core",
        tags=["job", "background", "output", "log", "tail", "read"],
        timeout=15,
        parallel_safe=True,
        idempotent=False,
    )
    reg.register(
        name="job_kill",
        func=job_kill,
        description="Terminate a running background job (whole process group; SIGTERM then SIGKILL).",
        parameters={
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "Job to terminate"},
            },
            "required": ["job_id"],
        },
        category="core",
        tags=["job", "background", "kill", "stop", "terminate", "cancel"],
        timeout=15,
        parallel_safe=False,
        safety_level="caution",
    )
