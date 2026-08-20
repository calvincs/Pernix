"""Pernix — SessionKernel lifecycle (adaptation plan 2b).

Persistent per-session namespace; soft aborts preserve it; snapshots revive
it across kernel restarts; the registry LRU-caps live kernels and reaps
idle ones with a snapshot first.
"""

import sys
import time

import pytest

import core.kernel as kernel_mod
from core.kernel import KernelRegistry, SessionKernel


@pytest.fixture(autouse=True)
def _kernel_env(tmp_path, monkeypatch):
    # State under tmp; project interpreter (has dill) instead of paying a
    # venv+pip build per test.
    monkeypatch.setattr(kernel_mod, "KERNEL_STATE_ROOT", tmp_path / "kernels")
    monkeypatch.setattr(SessionKernel, "_interpreter", lambda self: sys.executable)


def _mk(session_id="sess-k") -> SessionKernel:
    return SessionKernel(session_id)


def test_namespace_persists_across_calls():
    k = _mk()
    try:
        result, note = k.execute("x = 41", timeout=30)
        assert note and "fresh namespace" in note
        result, note = k.execute("x += 1\nprint(x)", timeout=30)
        assert note is None  # already running — nothing noteworthy
        assert "42" in result.stdout
    finally:
        k.shutdown(snapshot=False)


def test_snapshot_shutdown_revival_cycle():
    k = _mk("sess-revive")
    try:
        k.execute("greeting = 'hello'\ncount = 7", timeout=30)
        k.shutdown(snapshot=True)
        assert k.snapshot_path.exists()
        assert k.manifest_path.exists()
        assert not k.alive

        result, note = k.execute("print(greeting, count + 1)", timeout=30)
        assert note and "kernel revived" in note and "greeting" in note
        assert "hello 8" in result.stdout
    finally:
        k.shutdown(snapshot=False)


def test_soft_deadline_preserves_namespace():
    k = _mk("sess-deadline")
    try:
        k.execute("x = 1", timeout=30)
        result, _ = k.execute("import time\ntime.sleep(30)", timeout=2)
        assert "KeyboardInterrupt" in result.stderr  # aborted, not killed
        assert k.alive
        result, note = k.execute("print(x)", timeout=30)
        assert note is None  # same kernel, no respawn
        assert "1" in result.stdout
    finally:
        k.shutdown(snapshot=False)


def test_soft_cancel_preserves_namespace():
    k = _mk("sess-cancel")
    try:
        k.execute("x = 5", timeout=30)
        cancelled = {"flag": False}
        started = time.monotonic()

        def _cancel_check():
            # Trip after the cell has had a moment to start sleeping.
            if time.monotonic() - started > 0.5:
                cancelled["flag"] = True
            return cancelled["flag"]

        result, _ = k.execute("import time\ntime.sleep(30)", timeout=60, cancel_check=_cancel_check)
        assert "KeyboardInterrupt" in result.stderr
        assert k.alive
        result, _ = k.execute("print(x)", timeout=30)
        assert "5" in result.stdout
    finally:
        k.shutdown(snapshot=False)


def test_dead_child_respawns_with_revival():
    k = _mk("sess-die")
    try:
        k.execute("marker = 'survives'", timeout=30)
        k.snapshot_now()

        # Crash detected BETWEEN cells: ensure_started notices and respawns
        # transparently with revival — no error surfaces.
        k._repl.kill()
        result, note = k.execute("print(marker)", timeout=30)
        assert note and "kernel revived" in note
        assert "survives" in result.stdout

        # Crash MID-cell: the running execute raises KernelError; the next
        # call respawns and revives.
        with pytest.raises(kernel_mod.KernelError):
            k.execute("import os\nos.kill(os.getpid(), 9)", timeout=30)
        result, note = k.execute("print(marker)", timeout=30)
        assert note and "kernel revived" in note
        assert "survives" in result.stdout
    finally:
        k.shutdown(snapshot=False)


def test_spawn_failure_surfaces_as_kernel_error(monkeypatch):
    """repl_tool only catches KernelError. A start() failure raising
    RLMChildDied straight through would escape that handler and surface as a
    raw tool error instead of the "kernel will restart" path."""
    from core.extensions.rlm.child_env import ChildREPL
    from core.extensions.rlm.types import RLMChildDied

    def _boom(self):
        raise RLMChildDied("child REPL failed to start: simulated")

    monkeypatch.setattr(ChildREPL, "start", _boom)
    k = _mk("sess-nostart")
    with pytest.raises(kernel_mod.KernelError) as excinfo:
        k.execute("x = 1", timeout=30)
    assert isinstance(excinfo.value.__cause__, RLMChildDied)  # cause preserved
    assert k._repl is None  # no half-built kernel left behind


def test_busy_lock_is_reported_not_reaped():
    """A driver that can't get the round-trip lock reports busy; the kernel is
    alive and working, so it must not be cleaned up."""
    from core.extensions.rlm.child_env import ChildBusy

    k = _mk("sess-busy")
    try:
        k.execute("x = 1", timeout=30)

        def _busy(what, timeout=None):
            raise ChildBusy(f"child REPL busy: {what}")

        k._repl._acquire_rt = _busy  # instance attr shadows the method
        with pytest.raises(kernel_mod.KernelError) as excinfo:
            k.execute("print(x)", timeout=30)
        assert "busy" in str(excinfo.value)
        del k._repl._acquire_rt

        assert k.alive  # not torn down
        result, note = k.execute("print(x)", timeout=30)
        assert note is None and "1" in result.stdout  # same kernel, namespace intact
    finally:
        k.shutdown(snapshot=False)


def test_bind_variable():
    k = _mk("sess-bind")
    try:
        payload = "line one\nline two\n" * 500
        k.bind_variable("tool_result_1", payload)
        result, _ = k.execute("print(len(tool_result_1), tool_result_1[:8])", timeout=30)
        assert str(len(payload)) in result.stdout
        assert "line one" in result.stdout
    finally:
        k.shutdown(snapshot=False)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_lru_cap(monkeypatch):
    monkeypatch.setattr("config.settings.kernel_max_concurrent", 1)
    reg = KernelRegistry()
    try:
        k1 = reg.get_or_create("lru-1")
        k1.execute("a = 1", timeout=30)
        k2 = reg.get_or_create("lru-2")
        k2.execute("b = 2", timeout=30)
        assert not k1.alive  # LRU-evicted with a snapshot
        assert k1.snapshot_path.exists()
        assert k2.alive
    finally:
        reg.shutdown_session("lru-1", snapshot=False)
        reg.shutdown_session("lru-2", snapshot=False)


def test_registry_reap_idle(monkeypatch):
    monkeypatch.setattr("config.settings.kernel_idle_seconds", 0)
    reg = KernelRegistry()
    try:
        k = reg.get_or_create("reap-1")
        k.execute("x = 1", timeout=30)
        time.sleep(0.05)
        assert reg.any_reapable()
        reaped = reg.reap_idle()
        assert reaped == 1
        assert not k.alive
        assert k.snapshot_path.exists()
        assert reg.stats()["kernels"] == 0  # dead entry pruned
    finally:
        reg.shutdown_session("reap-1", snapshot=False)


def test_registry_purge_state(tmp_path):
    reg = KernelRegistry()
    k = reg.get_or_create("purge-1")
    k.execute("x = 1", timeout=30)
    state_dir = k.state_dir
    assert state_dir.exists()
    reg.shutdown_session("purge-1", snapshot=False, purge_state=True)
    assert not state_dir.exists()


def test_child_survives_its_spawning_threads_death(monkeypatch, tmp_path):
    """The child must be tied to the parent PROCESS, not the spawning thread.

    child_runner used to arm prctl(PR_SET_PDEATHSIG, SIGKILL), which prctl(2)
    scopes to the THREAD that spawned the child: a kernel created from any
    short-lived thread was SIGKILLed the moment that thread exited (exit -9,
    empty child.log, next round-trip = "connection lost mid-cell"). The two
    test_repl_tool binding tests hit exactly this — execute_tool_round runs
    binding via asyncio.to_thread inside asyncio.run(), whose teardown
    retires the worker thread. Production dodged it only because tool-pool
    and default-executor threads happen to be immortal on a live server.
    The replacement is a ppid watcher in the child: process semantics.
    """
    import threading
    import time as _time

    import core.kernel as kernel_mod
    from core.kernel import SessionKernel, get_kernel_registry

    monkeypatch.setattr(kernel_mod, "KERNEL_STATE_ROOT", tmp_path / "kernels")
    monkeypatch.setattr(SessionKernel, "_interpreter", lambda self: sys.executable)
    monkeypatch.setattr("config.settings.session_kernel_enabled", True)

    box = {}

    def _spawn():
        k = get_kernel_registry().get_or_create("thread-death-1")
        k.bind_variable("v1", "DATA-" + "z" * 500 + "-END")
        box["k"] = k

    t = threading.Thread(target=_spawn)
    t.start()
    t.join()
    _time.sleep(1.5)  # the old pdeathsig kill landed well inside this window

    k = box["k"]
    assert k.alive, "child died with its spawning thread (pdeathsig regression)"
    result, _ = k.execute("print(len(v1))", timeout=15)
    assert "509" in result.stdout
    get_kernel_registry().shutdown_session("thread-death-1", snapshot=False)
