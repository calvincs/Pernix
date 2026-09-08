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
- job_kill terminates the job's containment unit.

A job is admitted by the same policy as a foreground bash call
(`check_shell_command`): same shell, same environment, a much longer leash,
so the same command rules — otherwise job_start is the way around bash.

Containment: the leader is launched with start_new_session, so it opens a
SESSION of its own and everything the job spawns inherits it. That session is
the job's containment unit, and it is what job_kill targets. The leader's own
process GROUP is not: `timeout` calls setpgid on itself, so the workload runs
in timeout's group, not the leader's, and killing the leader's group killed
the bookkeeping while the work ran on. Only a descendant that calls setsid
itself leaves the unit, and that case is reported as unresolved rather than
claimed as killed.

Durability: rows live in the sessions DB. The exit code is written by the
wrapper shell to a sidecar file, so completion is detectable even after a
server restart (when the Popen handle is long gone), and the containment
identity is written beside it so a kill after a restart still knows what it
may signal. A job is 'lost' only when its containment unit is empty and no
exit code was recorded.
"""

from __future__ import annotations

import json
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
_KILL_HARD_GRACE_S = 1.0
_CONTAINMENT_FILE = "containment"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jobs_root(root: Path | None = None) -> Path:
    """Where job bookkeeping lives: under the CONTAINMENT root, never under the
    job's cwd. Logs and exit sidecars are harness state shared by every session
    in the workspace — only the working directory follows a space."""
    if root is None:
        from core.tools.paths import workspace

        root = workspace()
    jobs = root / ".jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    return jobs


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


def _proc_ident(pid: int) -> dict | None:
    """Identity fields from /proc/<pid>/stat, or None when the pid is gone.

    `start` is field 22, the process's start time in clock ticks since boot.
    A pid on its own is not an identity — the kernel hands the number out
    again — but (pid, start) is, which is what lets a kill refuse to signal a
    stranger. comm can contain spaces and parens, so the split is on the last
    ')' rather than on whitespace.
    """
    if not pid:
        return None
    try:
        raw = open(f"/proc/{pid}/stat").read()
        fields = raw.rsplit(")", 1)[1].split()
        return {
            "state": fields[0],
            "ppid": int(fields[1]),
            "pgid": int(fields[2]),
            "sid": int(fields[3]),
            "start": int(fields[19]),
        }
    except (OSError, IndexError, ValueError):
        return None


def _record_containment(job_dir: Path, leader_pid: int) -> dict:
    """Write the job's containment identity beside its log, at launch.

    Durable on purpose: the kill can come from a different server process
    than the launch, and by then the Popen handle is long gone. The session
    id is knowable immediately (start_new_session makes the leader its own
    session leader, so sid == pid), while the process group that ends up
    holding the workload is not — `timeout` re-groups itself microseconds
    after Popen returns, and job_start promises to return instantly rather
    than wait for it. The group is therefore derived from this identity at
    kill time, when it can be read instead of guessed.
    """
    ident = _proc_ident(leader_pid)
    rec = {
        "leader_pid": leader_pid,
        "sid": leader_pid,
        "leader_start": ident["start"] if ident else None,
    }
    try:
        (job_dir / _CONTAINMENT_FILE).write_text(json.dumps(rec))
    except OSError:
        pass
    return rec


def _read_containment(job: dict) -> dict:
    """The job's recorded containment identity, or one rebuilt from the row.

    Jobs launched before the record existed still resolve: they were started
    with start_new_session too, so their leader pid is their session id. Only
    the recycle guard is weaker, because no start time was captured.
    """
    try:
        rec = json.loads((Path(job["log_path"]).parent / _CONTAINMENT_FILE).read_text())
        if isinstance(rec, dict) and rec.get("leader_pid"):
            rec.setdefault("sid", rec["leader_pid"])
            rec.setdefault("leader_start", None)
            return rec
    except (OSError, ValueError, KeyError, TypeError):
        pass
    pid = int(job.get("pid") or 0)
    return {"leader_pid": pid, "sid": pid, "leader_start": None}


def _identity_ok(rec: dict) -> bool:
    """False when the recorded pid now belongs to somebody else.

    A leader that is simply gone does not fail this: while any member of the
    session is alive the kernel keeps the number reserved as that session's
    id, so nobody else can be holding it. What fails is a live process on
    that pid with the wrong start time, or in a different session — that is
    the number having been handed on, and signalling it would hit a stranger.
    """
    if not rec.get("sid"):
        return False
    ident = _proc_ident(rec["leader_pid"])
    if ident is None:
        return True
    if ident["sid"] != rec["sid"]:
        return False
    return rec.get("leader_start") is None or ident["start"] == rec["leader_start"]


def _live_members(rec: dict | None) -> list[tuple[int, dict]]:
    """Live processes in the job's containment unit — its session.

    Everything a job spawns inherits that session: `timeout`'s own group, the
    inner shell, grandchildren, and orphans that outlived the leader and were
    reparented to init. Zombies are not live, and a process that started
    before the leader cannot be ours.
    """
    if not rec or not _identity_ok(rec):
        return []
    sid = rec["sid"]
    floor = rec.get("leader_start")
    try:
        entries = os.listdir("/proc")
    except OSError:
        # No procfs (non-Linux dev boxes): fall back to the leader's own
        # group, which is all a kill could reach before this record existed.
        if _pid_alive(rec["leader_pid"]):
            return [(rec["leader_pid"], {"pgid": rec["leader_pid"], "sid": sid, "state": "R", "start": 0})]
        return []
    members = []
    for entry in entries:
        if not entry.isdigit():
            continue
        ident = _proc_ident(int(entry))
        if ident is None or ident["sid"] != sid or ident["state"] == "Z":
            continue
        if floor is not None and ident["start"] < floor:
            continue
        members.append((int(entry), ident))
    return sorted(members, key=lambda m: m[0])


def _terminate_containment(rec: dict) -> tuple[int, list[int]]:
    """SIGTERM the group(s) holding the workload, escalate to SIGKILL on
    whatever is left, and report anything still standing.

    Escalation waits on the containment unit being empty, not on the leader
    being gone — the same rule foreground `_kill_process_tree` applies to its
    group, which is the check this file was missing. Returns
    (processes_signalled, leftover_pids).
    """
    members = _live_members(rec)
    if not members:
        return 0, []
    leader = rec["leader_pid"]
    groups = {ident["pgid"] for _, ident in members}
    # The wrapper shell sits alone in the leader's group; the workload sits in
    # the group `timeout` made for itself. Spare the leader on the first pass
    # so it outlives its child and still runs `echo $? > exit_code` — that is
    # the only way a killed job keeps an exit code. If the workload turned out
    # to share the leader's group there is nothing to spare.
    workload = groups - {leader} or groups
    for pgid in sorted(workload):
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.time() + _KILL_GRACE_S
    while time.time() < deadline and _live_members(rec):
        time.sleep(0.05)
    left = _live_members(rec)
    if left:
        for pgid in sorted({ident["pgid"] for _, ident in left}):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        deadline = time.time() + _KILL_HARD_GRACE_S
        while time.time() < deadline and _live_members(rec):
            time.sleep(0.05)
    _pid_alive(leader)  # reap the wrapper if it is our zombie
    return len(members), [pid for pid, _ in _live_members(rec)]


def _unresolved_note(leftovers: list[int]) -> str:
    pids = ", ".join(str(p) for p in leftovers[:8])
    more = "" if len(leftovers) <= 8 else f" (+{len(leftovers) - 8} more)"
    return (
        f"\nCLEANUP UNRESOLVED: {len(leftovers)} process(es) outlived SIGKILL: "
        f"pid(s) {pids}{more}. They are no longer tracked — check them with ps "
        f"before reusing a port, file or lock this job held."
    )


def _exit_sidecar(job: dict) -> int | None:
    """The exit code the wrapper recorded, or None if it never got that far."""
    try:
        return int((Path(job["log_path"]).parent / "exit_code").read_text().strip())
    except (OSError, ValueError, KeyError):
        return None


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
        # The wrapper is gone, which is not the same as the job being gone: a
        # command that backgrounds its work outlives its own shell, and those
        # descendants stay in the job's session. Only an empty containment
        # unit with no exit code is genuinely lost — killed externally, or
        # lost to a server restart racing the wrapper's last write.
        if _live_members(_read_containment(job)):
            return job
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


def _job_cwd(job: dict) -> str:
    """The root the job was launched against, from the sidecar written at
    start. Empty for a job that predates the sidecar."""
    try:
        return (Path(job["log_path"]).parent / "cwd").read_text().strip()
    except (OSError, KeyError):
        return ""


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
    cwd = _job_cwd(job)
    if cwd:
        line += f"\n  [cwd: {cwd}]"
    line += f"\n  full log: job_tail(job_id='{job['id']}')  file: {job['log_path']}"
    return line


def job_start(
    command: str,
    name: str = "",
    wall_seconds: int | None = None,
    _context: dict | None = None,
) -> str:
    """Start a detached background job."""
    from core.tools.builtin.core_tools import check_shell_command
    from db import models as db

    if not settings.jobs_enabled:
        return "Error: background jobs are disabled (settings.jobs_enabled)."

    # Same admission as foreground bash, in whichever mode is configured, and
    # before the concurrency cap so a refusal says what is actually wrong. A
    # job runs the same shell in the same environment for two hours instead of
    # ten minutes; if the policy did not follow it here, job_start would be
    # the documented way around bash.
    blocked = check_shell_command(command)
    if blocked:
        return blocked

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

    from core.tools.paths import build_shell_env, roots_for_context

    # The one relative-path root, taken from this call's context rather than
    # the paths ContextVars: the job outlives the thread that starts it, so the
    # root it runs against has to be decided and passed here, explicitly. Jobs
    # used to launch in the global workspace while bash ran in the space home,
    # which is how `job_start("cd impl && pytest")` reported a green suite from
    # a different copy of the project than the one under edit (2026-09-08).
    ws_root, job_cwd = roots_for_context(
        (_context or {}).get("workspace_override"),
        (_context or {}).get("workspace_home"),
    )
    job_cwd.mkdir(parents=True, exist_ok=True)

    job_id = uuid.uuid4().hex[:12]
    job_dir = _jobs_root(ws_root) / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    log_path = job_dir / "output.log"
    exit_file = job_dir / "exit_code"

    # coreutils `timeout` gives a thread-free hard cap (exit 124); the
    # wrapper's final echo makes completion durable across server restarts.
    wrapped = f"timeout -k 10 {timeout_s} bash -c {shlex.quote(command)}; " f"echo $? > {shlex.quote(str(exit_file))}"

    # Same environment bash gets — venv on PATH, VIRTUAL_ENV set, env-mode
    # filter applied. A bare os.environ.copy() left jobs on the system
    # python, so `python3 script.py` in a job failed to import packages the
    # agent had just installed in bash (session 3dc5a307d751: sympy). The venv
    # and PATH stay on the containment root because the toolchain is shared;
    # only HOME and the cwd follow the space, exactly as the bash tool does it.
    env = build_shell_env(ws_root, job_cwd)
    # Durable next to the log, so a poll after a server restart can still say
    # which project the job actually ran against.
    try:
        (job_dir / "cwd").write_text(str(job_cwd))
    except OSError:
        pass

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

    _record_containment(job_dir, proc.pid)
    db.create_job(
        job_id=job_id,
        session_id=session_id,
        name=name or "",
        command=command[:2000],
        pid=proc.pid,
        log_path=str(log_path),
        deadline_s=timeout_s,
    )
    logger.info(
        "job %s started (session %s, pid %d, cap %ds, cwd %s)", job_id, session_id[:12], proc.pid, timeout_s, job_cwd
    )
    return (
        f"Job started: {job_id} (pid {proc.pid}, wall cap {timeout_s}s).\n"
        f"[cwd: {job_cwd}]\n"
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
    """Terminate a job's containment unit (SIGTERM, then SIGKILL)."""
    from db import models as db

    job = db.get_job(job_id)
    if job is None:
        return f"Error: no job '{job_id}'"
    job = _refresh(job)
    rec = _read_containment(job)

    if not _identity_ok(rec):
        # The recorded pid belongs to somebody else now. There is nothing of
        # ours left to signal, and signalling anyway would hit a stranger.
        if job["state"] == "running":
            db.update_job(job_id, state="lost", finished_at=_now_iso())
        return f"Job {job_id} could not be terminated: its process identity was reused, so nothing was signalled."

    signalled, leftovers = _terminate_containment(rec)

    if job["state"] != "running":
        # A finished row can still have live descendants: a command that
        # backgrounds its work lets the wrapper exit and write exit 0 while
        # the work runs on. Sweep them rather than report nothing to do.
        if not signalled:
            return f"Job {job_id} is not running (state={job['state']})."
        note = (
            f"Job {job_id} is not running (state={job['state']}) — killed {signalled} process(es) it had left behind."
        )
        return note + (_unresolved_note(leftovers) if leftovers else "")

    if not signalled:
        db.update_job(job_id, state="lost", finished_at=_now_iso())
        return f"Job {job_id} was already gone."

    code = _exit_sidecar(job)
    db.update_job(job_id, state="killed", exit_code=code, finished_at=_now_iso())
    logger.info("job %s killed (%d process(es), %d leftover)", job_id, signalled, len(leftovers))
    msg = f"Job {job_id} killed ({signalled} process(es) terminated"
    msg += f", exit {code})." if code is not None else ", exit code unrecorded)."
    return msg + (_unresolved_note(leftovers) if leftovers else "")


def register(reg) -> None:
    if not settings.jobs_enabled:
        return
    reg.register(
        name="job_start",
        func=job_start,
        description=(
            "Start a long-running shell command as a DETACHED background job, "
            "in the same working directory bash uses. Use this instead of a blocking bash call for "
            "heavy compute that needs minutes: solvers, brute-force searches, "
            "builds, dataset crunching. The job survives the end of your turn; "
            "output streams to a log file. Returns a job_id — keep working and "
            "poll job_status(job_id) or job_tail(job_id). Wall-clock capped "
            "(default 2h, exit 124 on timeout); same memory cap and same "
            "command policy as bash. Print progress lines in your command so "
            "polls show advancement."
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
        description=(
            "Terminate a running background job and every process it spawned "
            "(SIGTERM, then SIGKILL). Reports anything it could not prove "
            "stopped rather than claiming a clean kill."
        ),
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
