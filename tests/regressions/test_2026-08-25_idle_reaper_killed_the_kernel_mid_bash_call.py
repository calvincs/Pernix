"""The kernel idle-reaper killed a live env while its session ran a long bash
call (field case: cd82 session 41e10cf3c7bd, 2026-08-25 04:42).

The reaper skipped kernels busy executing a cell, but "kernel idle" is not
"session idle": a 10-minute solver bash call kept the agent away from the
repl past kernel_idle_seconds (1500s), the kernel was snapshotted and shut
down, and the live game env — a socket-backed object dill cannot carry —
was gone on revival. The agent paid a NameError and a full env rebuild.
Kernels of sessions actively processing a turn are now never reap/evict
candidates, no matter how long the repl itself has sat idle.
"""

import time

from core.kernel import KernelRegistry
from db import models as db


class _StubKernel:
    def __init__(self, session_id):
        self.session_id = session_id
        self.alive = True
        self.last_used = time.monotonic() - 10_000  # far past any idle cap
        self._repl = None  # -> _is_busy() False
        self.shutdowns = []

    def shutdown(self, snapshot=True):
        self.shutdowns.append(snapshot)
        self.alive = False


def _registry_with(sid):
    reg = KernelRegistry()
    stub = _StubKernel(sid)
    reg._kernels[sid] = stub
    return reg, stub


def test_processing_session_protects_its_kernel():
    sid = db.create_session(title="mid-turn ARC solver")
    db.update_session(sid, state_v2="processing")
    reg, stub = _registry_with(sid)

    assert reg.reap_idle(max_idle=1.0) == 0
    assert stub.alive and stub.shutdowns == []


def test_idle_session_kernel_still_reaped():
    sid = db.create_session(title="parked session")
    db.update_session(sid, state_v2="idle_ready")
    reg, stub = _registry_with(sid)

    assert reg.reap_idle(max_idle=1.0) == 1
    assert stub.shutdowns == [True]  # snapshotted, not dropped


def test_cap_eviction_defers_mid_turn_kernels():
    sid = db.create_session(title="mid-turn under cap pressure")
    db.update_session(sid, state_v2="processing")
    reg, stub = _registry_with(sid)

    picked = reg._pick_lru_beyond_cap(exclude="someone-else")
    assert stub not in picked


def test_unknown_session_does_not_block_reaping():
    reg, stub = _registry_with("no-such-session-xyz")
    assert reg.reap_idle(max_idle=1.0) == 1
