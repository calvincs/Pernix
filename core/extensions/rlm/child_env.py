"""Parent-side management of the RLM child REPL process.

Spawn/kill discipline mirrors the bash tool (core/tools/builtin/core_tools.py):
setsid + RLIMIT_AS + RLIMIT_FSIZE via preexec_fn, process-group kill. The env
is built from scratch — never inherited — so no API keys reach model code.

Process lifecycle adapted from the Recursive Language Models reference
implementation (https://github.com/alexzhang13/rlm, MIT License,
Copyright (c) 2025 Alex Zhang), with docker replaced by a plain subprocess
and dill state replaced by a persistent child holding globals in RAM.
"""

import hashlib
import json
import logging
import os
import resource
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from core.extensions.rlm.protocol import FrameError, recv_frame, send_frame
from core.extensions.rlm.types import CellResult, RLMCancelled, RLMChildDied, RLMTimeout

logger = logging.getLogger(__name__)

# Hard gate on total staged context (checked by the tool before a run starts).
MAX_SOURCE_BYTES = 128 * 1024 * 1024
SOURCE_WARN_BYTES = 32 * 1024 * 1024

# A cell is "hung" only after this much time with no result AND no in-flight
# sub-LLM calls — legitimate llm_query_batched waits can far exceed this.
CELL_QUIET_TIMEOUT = 300.0
SIGINT_GRACE = 10.0
SPAWN_TIMEOUT = 15.0
LOAD_TIMEOUT = 120.0
SNAPSHOT_TIMEOUT = 300.0  # per-variable dill of up to 256MiB + fsync

_DEFAULT_AS_LIMIT = 8 * 1024 * 1024 * 1024
_DEFAULT_FSIZE_LIMIT = 2 * 1024 * 1024 * 1024

_CHILD_RUNNER = str(Path(__file__).resolve().parent / "child_runner.py")


@dataclass
class StagedContext:
    """Context material staged into <run_dir>/context/ for the child to read."""

    items: list[dict] = field(default_factory=list)  # load_context frame items
    extra_vars: dict = field(default_factory=dict)
    context_type: str = "str"
    total_chars: int = 0
    file_names: list[str] = field(default_factory=list)
    # sha256 of the staged content in staging order. continue_from compares it
    # against the prior run's manifest so a continuation never silently builds
    # on findings derived from different source material.
    sha256: str = ""


def _content_hash(contents: list[str]) -> str:
    h = hashlib.sha256()
    for c in contents:
        h.update(c.encode("utf-8", "ignore"))
        h.update(b"\x00")  # unambiguous boundary between staged items
    return h.hexdigest()


def stage_context(run_dir: Path, *, text: str | None = None, files: list[Path] | None = None) -> StagedContext:
    """Copy source material into the run dir; the child reads it from disk
    (never via argv/env). One file or inline text -> `context: str`; several
    files -> `context: list[str]` plus a `context_files` name list."""
    ctx_dir = run_dir / "context"
    ctx_dir.mkdir(parents=True, exist_ok=True)
    staged = StagedContext()

    if text is not None:
        path = ctx_dir / "context_0.txt"
        path.write_text(text, encoding="utf-8")
        staged.items = [{"var": "context_0", "path": "context/context_0.txt", "format": "text"}]
        staged.total_chars = len(text)
        staged.sha256 = _content_hash([text])
        return staged

    files = files or []
    if len(files) == 1:
        content = files[0].read_text(encoding="utf-8", errors="replace")
        path = ctx_dir / "context_0.txt"
        path.write_text(content, encoding="utf-8")
        staged.items = [{"var": "context_0", "path": "context/context_0.txt", "format": "text"}]
        staged.total_chars = len(content)
        staged.file_names = [files[0].name]
        staged.sha256 = _content_hash([content])
        return staged

    contents = [f.read_text(encoding="utf-8", errors="replace") for f in files]
    path = ctx_dir / "context_0.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(contents, fh, ensure_ascii=False)
    staged.items = [{"var": "context_0", "path": "context/context_0.json", "format": "json"}]
    staged.context_type = "list[str]"
    staged.total_chars = sum(len(c) for c in contents)
    staged.file_names = [f.name for f in files]
    staged.extra_vars = {"context_files": staged.file_names}
    staged.sha256 = _content_hash(contents)
    return staged


def _build_child_env(run_dir: Path, venv_bin: Path | None) -> dict[str, str]:
    """Scrubbed from scratch. Deliberately NOT the bash tool's env builder —
    its default passthrough mode would hand the child every API key."""
    path_parts = []
    if venv_bin is not None:
        path_parts.append(str(venv_bin))
    path_parts += ["/usr/local/bin", "/usr/bin", "/bin"]
    return {
        "PATH": ":".join(path_parts),
        "HOME": str(run_dir),
        "LANG": "C.UTF-8",
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


# Conservative across platforms: macOS/BSD sun_path holds ~104 bytes,
# Linux 108. Anything longer fails bind() with "AF_UNIX path too long".
_SUN_PATH_MAX = 100


def resolve_socket_dir(run_dir: Path) -> tuple[Path, bool]:
    """Directory for this run's unix sockets, and whether it's a private tmp.

    Uses the run dir itself when the socket paths fit within the sun_path
    limit; otherwise a short mkdtemp under /tmp (never TMPDIR — on macOS
    TMPDIR itself is a deep /var/folders path, which is the problem)."""
    run_dir = Path(run_dir).resolve()
    if len(str(run_dir / "exec.sock").encode()) <= _SUN_PATH_MAX:
        return run_dir, False
    import tempfile

    return Path(tempfile.mkdtemp(prefix="pnx-", dir="/tmp")), True


class ChildREPL:
    """One persistent child REPL process for one run."""

    def __init__(
        self,
        run_dir: Path,
        *,
        python_exe: str | None = None,
        address_space_limit: int = _DEFAULT_AS_LIMIT,
        fsize_limit: int = _DEFAULT_FSIZE_LIMIT,
        scaffold: str = "rlm",
    ):
        # Resolve to absolute: the child's cwd IS the run dir, so a relative
        # workspace_dir (the default) would make the child resolve the socket
        # paths against itself — <run_dir>/<run_dir>/exec.sock — and never
        # connect (found the hard way on box, run ba1e005a).
        self.run_dir = Path(run_dir).resolve()
        # Sockets may live elsewhere: AF_UNIX sun_path caps at ~104 bytes on
        # macOS/BSD, and a deep run dir (pytest tmpdirs, user-chosen
        # workspace roots) makes bind() fail with "path too long". Fall back
        # to a short private dir under /tmp; cleanup() removes it.
        sock_dir, self._sock_dir_is_temp = resolve_socket_dir(self.run_dir)
        self.exec_sock_path = sock_dir / "exec.sock"
        self.llm_sock_path = sock_dir / "llm.sock"
        self._python_exe = python_exe or sys.executable
        self._as_limit = address_space_limit
        self._fsize_limit = fsize_limit
        self.scaffold = scaffold  # "rlm" (sub-LLM stubs + answer) | "plain" (session kernel)
        self.popen: subprocess.Popen | None = None
        self._conn: socket.socket | None = None
        self._listener: socket.socket | None = None
        self._log_file = None
        self._exec_id = 0
        # One driver at a time (adaptation plan 2a): the connection carries
        # interleaved frames from exactly one exchange. RLM's broker is a
        # single driver by construction; session kernels add concurrent
        # callers (repl cells vs. maintenance snapshots), which without this
        # would interleave frames and hang both. interrupt()/kill() stay
        # lock-free — they are the escape hatch while a cell holds the lock.
        self._rt_lock = threading.Lock()

    def start(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.exec_sock_path.unlink(missing_ok=True)

        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(str(self.exec_sock_path))
        self._listener.listen(1)
        self._listener.settimeout(SPAWN_TIMEOUT)

        as_limit, fsize_limit = self._as_limit, self._fsize_limit

        def _child_setup():
            os.setsid()
            try:
                resource.setrlimit(resource.RLIMIT_AS, (as_limit, as_limit))
            except (ValueError, OSError):
                # macOS rejects RLIMIT_AS in a forked child (EINVAL) — the
                # address-space cap is defense-in-depth (I7), not a boundary,
                # so proceed without it rather than failing every spawn on
                # Darwin. Linux enforces it normally.
                pass
            try:
                resource.setrlimit(resource.RLIMIT_FSIZE, (fsize_limit, fsize_limit))
            except (ValueError, OSError):
                pass

        venv_bin = Path(self._python_exe).parent if "venv" in self._python_exe else None
        self._log_file = open(self.run_dir / "child.log", "ab")
        try:
            self.popen = subprocess.Popen(
                [
                    self._python_exe,
                    _CHILD_RUNNER,
                    "--exec-sock",
                    str(self.exec_sock_path),
                    "--llm-sock",
                    str(self.llm_sock_path),
                    "--scaffold",
                    self.scaffold,
                ],
                cwd=str(self.run_dir),
                env=_build_child_env(self.run_dir, venv_bin),
                stdout=self._log_file,
                stderr=self._log_file,
                stdin=subprocess.DEVNULL,
                preexec_fn=_child_setup,
            )
            self._conn, _ = self._listener.accept()
            self._conn.settimeout(SPAWN_TIMEOUT)
            hello = recv_frame(self._conn)
            if hello.get("type") != "hello":
                raise RLMChildDied(f"unexpected first frame from child: {hello.get('type')}")
        except (socket.timeout, EOFError, FrameError, OSError) as e:
            self.cleanup()
            raise RLMChildDied(f"child REPL failed to start: {e}") from e

    def load_context(self, staged: StagedContext) -> int:
        frame = {"type": "load_context", "items": staged.items, "extra_vars": staged.extra_vars}
        reply = self._roundtrip(frame, deadline=time.monotonic() + LOAD_TIMEOUT)
        if not reply.get("ok"):
            raise RLMChildDied(f"context load failed in child: {reply.get('error')}")
        return int(reply.get("chars", 0))

    def execute_cell(
        self,
        code: str,
        *,
        deadline: float,
        cancel_check=None,
        in_flight=None,
        last_activity=None,
    ) -> CellResult:
        """Run one cell; poll the watchdog while waiting for the result.

        `in_flight`/`last_activity` come from the broker: a cell with live (or
        recently active) sub-LLM calls is working, not hung.
        """
        self._rt_lock.acquire()
        try:
            return self._execute_cell_locked(
                code, deadline=deadline, cancel_check=cancel_check, in_flight=in_flight, last_activity=last_activity
            )
        finally:
            self._rt_lock.release()

    def _execute_cell_locked(
        self,
        code: str,
        *,
        deadline: float,
        cancel_check=None,
        in_flight=None,
        last_activity=None,
    ) -> CellResult:
        self._exec_id += 1
        exec_id = self._exec_id
        self._send({"type": "exec", "id": exec_id, "code": code})

        cell_start = time.monotonic()
        interrupted_at: float | None = None
        while True:
            now = time.monotonic()
            if self.popen is not None and self.popen.poll() is not None:
                raise RLMChildDied(f"child REPL exited with code {self.popen.returncode} mid-cell")
            if cancel_check is not None and cancel_check():
                self.kill()
                raise RLMCancelled("session cancelled during RLM cell execution")
            if now > deadline:
                self.kill()
                raise RLMTimeout("RLM run wall clock expired during cell execution")
            if interrupted_at is not None and now - interrupted_at > SIGINT_GRACE:
                self.kill()
                raise RLMChildDied("cell unresponsive after interrupt — child killed")
            if interrupted_at is None:
                busy = in_flight() > 0 if in_flight is not None else False
                last = last_activity() if last_activity is not None else cell_start
                quiet_since = max(cell_start, last)
                if not busy and now - quiet_since > CELL_QUIET_TIMEOUT:
                    logger.warning("RLM cell quiet for %.0fs — sending SIGINT", now - quiet_since)
                    self.interrupt()
                    interrupted_at = now

            try:
                self._conn.settimeout(1.0)
                reply = recv_frame(self._conn)
            except socket.timeout:
                continue
            except (EOFError, ConnectionError, OSError, FrameError) as e:
                raise RLMChildDied(f"child REPL connection lost mid-cell: {e}") from e

            if reply.get("type") == "exec_result" and reply.get("id") == exec_id:
                return CellResult(
                    stdout=reply.get("stdout", ""),
                    stderr=reply.get("stderr", ""),
                    duration=float(reply.get("duration", 0.0)),
                    final_answer=reply.get("final_answer"),
                    draft=reply.get("draft"),
                    var_names=list(reply.get("var_names", [])),
                )
            logger.warning("discarding unexpected frame from child: %s", reply.get("type"))

    def snapshot(self, path: Path, *, max_bytes: int = 256 * 1024 * 1024) -> dict:
        """Per-variable dill snapshot of the child namespace (plan 2a).

        Returns the child's report: {ok, stored: [names], skipped: {name:
        reason}, bytes}. The child writes the file itself (atomically), so
        payload size is never bounded by the frame cap. Serialized with
        cell execution via the round-trip lock."""
        reply = self._roundtrip(
            {"type": "snapshot", "path": str(path), "max_bytes": int(max_bytes)},
            deadline=time.monotonic() + SNAPSHOT_TIMEOUT,
        )
        if reply.get("type") != "snapshot_result":
            raise RLMChildDied(f"unexpected snapshot reply: {reply.get('type')}")
        return reply

    def restore(self, path: Path) -> dict:
        """Load a snapshot per-name; one stale blob fails alone. Returns
        {ok, restored: [names], failed: {name: reason}}."""
        reply = self._roundtrip(
            {"type": "restore", "path": str(path)},
            deadline=time.monotonic() + SNAPSHOT_TIMEOUT,
        )
        if reply.get("type") != "restore_result":
            raise RLMChildDied(f"unexpected restore reply: {reply.get('type')}")
        return reply

    def interrupt(self) -> None:
        if self.popen is not None and self.popen.poll() is None:
            try:
                os.kill(self.popen.pid, signal.SIGINT)
            except (OSError, ProcessLookupError):
                pass

    def kill(self) -> None:
        if self.popen is not None:
            from core.tools.builtin.core_tools import _kill_process_tree

            _kill_process_tree(self.popen)

    def cleanup(self) -> None:
        try:
            if self._conn is not None:
                try:
                    self._conn.settimeout(2.0)
                    send_frame(self._conn, {"type": "shutdown"})
                except (OSError, FrameError):
                    pass
        finally:
            self.kill()
            for sock in (self._conn, self._listener):
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
            self._conn = self._listener = None
            if self._log_file is not None:
                try:
                    self._log_file.close()
                except OSError:
                    pass
                self._log_file = None
            self.exec_sock_path.unlink(missing_ok=True)
            if getattr(self, "_sock_dir_is_temp", False):
                # Private short socket dir under /tmp (deep-run-dir fallback).
                # The broker's llm.sock may live here too — its stop() unlinks
                # with missing_ok, so removal order doesn't matter.
                import shutil

                shutil.rmtree(self.exec_sock_path.parent, ignore_errors=True)

    # ---- internals ----

    def _send(self, frame: dict) -> None:
        if self._conn is None:
            raise RLMChildDied("child REPL is not running")
        try:
            send_frame(self._conn, frame)
        except (OSError, FrameError) as e:
            raise RLMChildDied(f"failed to send to child REPL: {e}") from e

    def _roundtrip(self, frame: dict, *, deadline: float) -> dict:
        with self._rt_lock:
            return self._roundtrip_locked(frame, deadline=deadline)

    def _roundtrip_locked(self, frame: dict, *, deadline: float) -> dict:
        self._send(frame)
        while True:
            if time.monotonic() > deadline:
                self.kill()
                raise RLMChildDied("child REPL round-trip timed out")
            if self.popen is not None and self.popen.poll() is not None:
                raise RLMChildDied(f"child REPL exited with code {self.popen.returncode}")
            try:
                self._conn.settimeout(1.0)
                return recv_frame(self._conn)
            except socket.timeout:
                continue
            except (EOFError, ConnectionError, OSError, FrameError) as e:
                raise RLMChildDied(f"child REPL connection lost: {e}") from e
