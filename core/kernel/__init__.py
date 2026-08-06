"""Pernix — Session kernel: a persistent per-session Python workspace.

Adaptation plan 2b. Wraps the RLM extension's ChildREPL (scaffold="plain":
no sub-LLM stubs, no answer dict) as long-lived per-session state:

- Variables, imports, and helper functions survive across tool rounds AND
  turns — and across compaction by construction (I1: compaction is a message
  view transform; the kernel is a process, untouched).
- Across restarts, per-variable dill snapshots revive the namespace; one
  unpicklable object is skipped-and-reported, never fatal.
- The kernel lives in its own slot — NEVER session._active_process, whose
  consumers kill unconditionally on any tool dispatch timeout in the
  session. Cell aborts are soft (SIGINT preserving the namespace), with
  kill only as the unresponsive-child escalation.
- cwd = the shared workspace (or the session's 1g override at spawn time),
  so repl and bash see the same files; sockets, logs, and snapshots stay in
  data/kernels/<session_id>/.

Same security posture as RLM (I7): scrubbed env, rlimits, setsid —
defense-in-depth, not a boundary.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from config import settings
from core.extensions.rlm.child_env import ChildREPL
from core.extensions.rlm.types import CellResult

logger = logging.getLogger("pernix.kernel")

KERNEL_STATE_ROOT = Path("data/kernels")


class KernelError(Exception):
    """The kernel could not run the cell (died, unresponsive, unstartable)."""


class SessionKernel:
    """One persistent plain-scaffold child REPL for one session."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.state_dir = (KERNEL_STATE_ROOT / session_id).resolve()
        self._repl: ChildREPL | None = None
        self._lock = threading.RLock()
        self.last_used = time.monotonic()

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------

    @property
    def snapshot_path(self) -> Path:
        return self.state_dir / "kernel-state.dill"

    @property
    def manifest_path(self) -> Path:
        return self.state_dir / "manifest.json"

    @property
    def payloads_dir(self) -> Path:
        """Durable sidecar for bound tool-result payloads (plan 2c) — keeps
        the transcript reconstructible after binding truncates in-context."""
        return self.state_dir / "payloads"

    @property
    def alive(self) -> bool:
        repl = self._repl
        return bool(repl is not None and repl.popen is not None and repl.popen.poll() is None)

    # ------------------------------------------------------------------
    # Interpreter resolution (workspace venv, dill ensured)
    # ------------------------------------------------------------------

    def _interpreter(self) -> str:
        """The workspace venv python (created if missing, mirroring bash),
        with dill ensured for snapshots. Falls back to sys.executable."""
        try:
            venv_py = Path(settings.workspace_venv_python)
            if not venv_py.exists():
                ws = Path(settings.workspace_dir).resolve()
                ws.mkdir(parents=True, exist_ok=True)
                subprocess.run(
                    [sys.executable, "-m", "venv", str(ws / ".venv")],
                    capture_output=True,
                    timeout=120,
                )
            if not venv_py.exists():
                return sys.executable
            probe = subprocess.run([str(venv_py), "-c", "import dill"], capture_output=True, timeout=30)
            if probe.returncode != 0:
                logger.info("Installing dill into workspace venv for kernel snapshots")
                subprocess.run(
                    [str(venv_py), "-m", "pip", "install", "--quiet", "dill"],
                    capture_output=True,
                    timeout=120,
                )
            return str(venv_py)
        except Exception as e:
            logger.warning("Workspace venv unavailable for kernel (%s); using server interpreter", e)
            return sys.executable

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def ensure_started(self) -> str | None:
        """Start (or revive) the kernel. Returns a model-facing note when
        something noteworthy happened (fresh start, revival, failed
        restore), else None for the already-running case."""
        with self._lock:
            if self.alive:
                return None
            self.state_dir.mkdir(parents=True, exist_ok=True)
            from core.tools.paths import workspace

            repl = ChildREPL(
                self.state_dir,
                python_exe=self._interpreter(),
                scaffold="plain",
                cwd=workspace(),  # honors the session's 1g override at spawn
            )
            repl.start()
            self._repl = repl
            self.last_used = time.monotonic()

            if not self.snapshot_path.exists():
                return "[kernel started: fresh namespace]"
            try:
                r = repl.restore(self.snapshot_path)
            except Exception as e:
                return f"[kernel started; snapshot restore failed: {e}]"
            if not r.get("ok"):
                return f"[kernel started; snapshot not restorable: {r.get('error')}]"
            restored = r.get("restored", [])
            failed = r.get("failed", {})
            note = f"[kernel revived: {len(restored)} name(s) restored"
            if restored:
                note += f" ({', '.join(restored[:8])}{', …' if len(restored) > 8 else ''})"
            if failed:
                reasons = "; ".join(f"{k}: {v.split(':')[0]}" for k, v in list(failed.items())[:4])
                note += f", {len(failed)} failed ({reasons})"
            return note + "]"

    def execute(self, code: str, timeout: float, cancel_check=None) -> tuple[CellResult, str | None]:
        """Run one cell. Soft aborts on cancel/deadline (SIGINT, namespace
        preserved); a dead child surfaces as KernelError and the next call
        respawns (reviving from the last snapshot, if any)."""
        note = self.ensure_started()
        self.last_used = time.monotonic()
        repl = self._repl
        try:
            result = repl.execute_cell(
                code,
                deadline=time.monotonic() + timeout,
                cancel_check=cancel_check,
                soft_abort=True,
            )
        except Exception as e:
            # RLMChildDied / RLMTimeout after failed interrupt / connection
            # loss — the process is gone or unusable. Drop it; next call
            # starts fresh (+ revival from the last snapshot).
            try:
                repl.cleanup()
            except Exception:
                pass
            self._repl = None
            raise KernelError(f"kernel cell failed: {e}") from e
        self.last_used = time.monotonic()
        return result, note

    def bind_variable(self, name: str, text: str) -> None:
        """Load a text payload into the kernel namespace (plan 2c binding).
        Uses the load_context frame via a spooled file so size is unbounded
        by the frame cap."""
        with self._lock:
            self.ensure_started()
            self.payloads_dir.mkdir(parents=True, exist_ok=True)
            spool = self.payloads_dir / f".bind_{name}.txt"
            spool.write_text(text, encoding="utf-8")
            from core.extensions.rlm.child_env import StagedContext

            try:
                self._repl.load_context(StagedContext(items=[{"var": name, "path": str(spool), "format": "text"}]))
            finally:
                spool.unlink(missing_ok=True)

    def snapshot_now(self) -> dict | None:
        """Snapshot the live namespace to disk + write the manifest."""
        with self._lock:
            if not self.alive:
                return None
            self.state_dir.mkdir(parents=True, exist_ok=True)
            reply = self._repl.snapshot(
                self.snapshot_path,
                max_bytes=int(settings.kernel_snapshot_max_bytes),
            )
            manifest = {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "session_id": self.session_id,
                "ok": bool(reply.get("ok")),
                "stored": reply.get("stored", []),
                "skipped": reply.get("skipped", {}),
                "bytes": reply.get("bytes", 0),
                "error": reply.get("error"),
            }
            tmp = self.manifest_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            tmp.replace(self.manifest_path)
            return reply

    def cancel_cell(self) -> None:
        """SIGINT the running cell (namespace preserved). Lock-free — this
        is the escape hatch while a cell holds the round-trip lock."""
        repl = self._repl
        if repl is not None:
            repl.interrupt()

    def shutdown(self, snapshot: bool = True) -> None:
        with self._lock:
            repl = self._repl
            if repl is None:
                return
            if snapshot and self.alive:
                try:
                    self.snapshot_now()
                except Exception as e:
                    logger.warning("Kernel %s: shutdown snapshot failed: %s", self.session_id[:12], e)
            try:
                repl.cleanup()
            except Exception:
                pass
            self._repl = None
            logger.info("Kernel %s: shut down%s", self.session_id[:12], " (snapshotted)" if snapshot else "")

    def purge_state(self) -> None:
        """Delete snapshots + payload sidecars (session deletion)."""
        shutil.rmtree(self.state_dir, ignore_errors=True)


class KernelRegistry:
    """All live kernels, LRU-capped at settings.kernel_max_concurrent."""

    def __init__(self):
        self._kernels: dict[str, SessionKernel] = {}
        self._lock = threading.Lock()

    def get_or_create(self, session_id: str) -> SessionKernel:
        with self._lock:
            kernel = self._kernels.get(session_id)
            if kernel is None:
                kernel = SessionKernel(session_id)
                self._kernels[session_id] = kernel
            evict = self._pick_lru_beyond_cap(exclude=session_id)
        # Evict outside the registry lock — a shutdown snapshot is seconds
        # of blocking IO and must not stall other sessions' lookups.
        for old in evict:
            logger.info("Kernel cap reached; snapshotting + reaping LRU kernel %s", old.session_id[:12])
            old.shutdown(snapshot=True)
        return kernel

    def _pick_lru_beyond_cap(self, exclude: str) -> list[SessionKernel]:
        cap = max(1, int(settings.kernel_max_concurrent))
        live = [k for sid, k in self._kernels.items() if k.alive and sid != exclude]
        overflow = len(live) + 1 - cap  # +1 for the kernel about to start
        if overflow <= 0:
            return []
        live.sort(key=lambda k: k.last_used)
        return live[:overflow]

    def get(self, session_id: str) -> SessionKernel | None:
        with self._lock:
            return self._kernels.get(session_id)

    def reap_idle(self, max_idle: float | None = None) -> int:
        """Snapshot + shut down kernels idle past kernel_idle_seconds.
        Called from maintenance OFF the event loop (blocking dill IO)."""
        max_idle = max_idle if max_idle is not None else float(settings.kernel_idle_seconds)
        now = time.monotonic()
        with self._lock:
            candidates = [k for k in self._kernels.values() if k.alive and now - k.last_used > max_idle]
        for kernel in candidates:
            logger.info("Reaping idle kernel %s (idle %.0fs)", kernel.session_id[:12], now - kernel.last_used)
            kernel.shutdown(snapshot=True)
        with self._lock:
            dead = [sid for sid, k in self._kernels.items() if not k.alive]
            for sid in dead:
                del self._kernels[sid]
        return len(candidates)

    def any_reapable(self) -> bool:
        max_idle = float(settings.kernel_idle_seconds)
        now = time.monotonic()
        with self._lock:
            return any(k.alive and now - k.last_used > max_idle for k in self._kernels.values()) or any(
                not k.alive for k in self._kernels.values()
            )

    def shutdown_session(self, session_id: str, snapshot: bool = True, purge_state: bool = False) -> None:
        with self._lock:
            kernel = self._kernels.pop(session_id, None)
        if kernel is None:
            if purge_state:
                shutil.rmtree((KERNEL_STATE_ROOT / session_id).resolve(), ignore_errors=True)
            return
        kernel.shutdown(snapshot=snapshot and not purge_state)
        if purge_state:
            kernel.purge_state()

    def shutdown_session_detached(self, session_id: str, snapshot: bool = True, purge_state: bool = False) -> None:
        """Fire-and-forget shutdown for sync callers on the event loop
        (manager.remove / delete_session) — the snapshot must not block."""
        threading.Thread(
            target=self.shutdown_session,
            args=(session_id, snapshot, purge_state),
            daemon=True,
            name=f"kernel-shutdown-{session_id[:8]}",
        ).start()

    def stats(self) -> dict:
        with self._lock:
            return {
                "kernels": len(self._kernels),
                "alive": sum(1 for k in self._kernels.values() if k.alive),
            }


_registry: KernelRegistry | None = None
_registry_lock = threading.Lock()


def get_kernel_registry() -> KernelRegistry:
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = KernelRegistry()
    return _registry
