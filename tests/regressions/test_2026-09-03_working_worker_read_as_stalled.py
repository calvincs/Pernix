"""A worker running a long subprocess was reported idle, then abandoned.

Field case, session 3dc5a307d751 (2026-09-03). `last_activity_time` only moves
on harness events — a tool call starting or finishing, a stream chunk. A
worker that handed a long build or solve to bash produced none of those while
it worked, so check_workers printed "idle 900s" and await_workers' stall test
counted it stalled and abandoned the wave, killing work that was making
progress the whole time.

Liveness now counts a running subprocess: `_worker_idle_seconds` answers 0
while any process the worker registered is still alive, and the check_workers
line says "running subprocess" instead of a bogus idle age.
"""

from __future__ import annotations

import time as _time

import pytest

import core.extensions.orchestration as orch
from core.extensions.orchestration import _worker_has_live_process, _worker_idle_seconds, check_workers
from sessions.manager import SessionManager
from sessions.state_v2 import SessionStateV2 as S
from sessions.state_v2 import _set_state


class _FakeProc:
    """A subprocess handle: poll() returns None while it runs."""

    def __init__(self, running: bool = True):
        self.pid = 4242
        self._running = running

    def poll(self):
        return None if self._running else 0


@pytest.fixture
def mgr(monkeypatch):
    fresh = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", fresh)
    return fresh


def _worker(mgr, parent_id, *, idle_seconds: int = 3600, state=S.PROCESSING):
    wid = mgr.create_session(title="Solver", session_type="worker", parent_session_id=parent_id)
    mgr.get(parent_id).worker_ids.append(wid)
    w = mgr.get(wid)
    _set_state(w, state)
    w._turn_id = 1
    w.last_activity_time = _time.time() - idle_seconds
    return wid, w


def test_a_live_subprocess_is_activity(mgr):
    parent_id = mgr.create_session(title="P")
    _wid, w = _worker(mgr, parent_id)
    assert _worker_idle_seconds(w) > 3000  # nothing running yet

    w.register_process(_FakeProc(), owner="call-1")

    assert _worker_has_live_process(w) is True
    assert _worker_idle_seconds(w) == 0


def test_an_exited_subprocess_is_not(mgr):
    """The process finished; the worker really has gone quiet."""
    parent_id = mgr.create_session(title="P")
    _wid, w = _worker(mgr, parent_id)
    w.register_process(_FakeProc(running=False), owner="call-1")

    assert _worker_has_live_process(w) is False
    assert _worker_idle_seconds(w) > 3000


def test_a_released_process_is_not_counted(mgr):
    parent_id = mgr.create_session(title="P")
    _wid, w = _worker(mgr, parent_id)
    handle = w.register_process(_FakeProc(), owner="call-1")
    w.release_process(handle)

    assert _worker_idle_seconds(w) > 3000


def test_check_workers_says_running_subprocess(mgr):
    parent_id = mgr.create_session(title="P")
    _wid, w = _worker(mgr, parent_id)
    w.register_process(_FakeProc(), owner="call-1")

    out = check_workers(_context={"session_id": parent_id})

    assert "running subprocess" in out
    assert "idle " not in out


def test_check_workers_still_reports_a_genuinely_idle_worker(mgr):
    parent_id = mgr.create_session(title="P")
    _worker(mgr, parent_id)

    out = check_workers(_context={"session_id": parent_id})

    assert "idle " in out
    assert "running subprocess" not in out


def test_await_workers_does_not_abandon_a_worker_running_a_subprocess(mgr, monkeypatch):
    """The wave-abandon path (the sanity case in test_orchestration.py fires
    on exactly this shape) must not trigger while a child process is alive."""
    parent_id = mgr.create_session(title="P")
    _wid, w = _worker(mgr, parent_id, idle_seconds=200)
    w.register_process(_FakeProc(), owner="call-1")

    fake_clock = [orch.time.time()]
    monkeypatch.setattr(orch.time, "time", lambda: fake_clock[0])
    monkeypatch.setattr(orch.time, "sleep", lambda secs: fake_clock.__setitem__(0, fake_clock[0] + max(secs, 60)))

    out = orch.await_workers(stale_threshold=10, _context={"session_id": parent_id})

    assert "appear stalled" not in out, out


def test_await_workers_still_reports_a_truly_stalled_worker(mgr):
    """Same shape without the live process — the stall detector still works."""
    parent_id = mgr.create_session(title="P")
    _worker(mgr, parent_id, idle_seconds=200)

    out = orch.await_workers(stale_threshold=10, _context={"session_id": parent_id})

    assert "appear stalled" in out, out
