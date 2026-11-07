"""A user in a long-running turn could not get a word in.

Field case 2026-11-07 (session 937fd96364ba, a goal-driven batch job):
1. A request to pause was injected while the agent sat in `sleep 1800`. It reached
   the model 8 minutes later, the agent agreed to pause, and the forced
   follow-up — worded from the open goal — fired on that very reply. The
   agent read it as an override of the request and kept working.
2. A later message waited behind `sleep 600` and was lost when the turn was
   cancelled before the sleep ended.
3. read_session_summary reported the last-active time from sessions.updated_at,
   which a long turn never bumps, so the session looked stuck for 1h47m.
"""

from __future__ import annotations

import subprocess
import threading
import time
from types import SimpleNamespace

from core.agent import _round_answers_a_mid_turn_message
from core.tools.builtin import core_tools


class _FakeSession:
    def __init__(self, speak_after_start: float | None = None):
        self.user_steer_seq = 0
        self._procs = {}
        self._speak_after_start = speak_after_start

    def register_process(self, proc, owner=""):
        self._procs[owner] = proc
        if self._speak_after_start is not None:
            threading.Timer(self._speak_after_start, lambda: setattr(self, "user_steer_seq", 1)).start()
        return owner

    def release_process(self, handle):
        self._procs.pop(handle, None)


def test_a_round_that_carried_a_mid_turn_message_gets_no_forced_followup():
    assert _round_answers_a_mid_turn_message(SimpleNamespace(delivered_message_ids=(91147,)))
    assert not _round_answers_a_mid_turn_message(SimpleNamespace(delivered_message_ids=()))
    assert not _round_answers_a_mid_turn_message(SimpleNamespace())


def test_wait_returns_none_when_the_command_just_finishes(monkeypatch):
    monkeypatch.setattr(core_tools.settings, "shell_user_interrupt_after_s", 1)
    proc = subprocess.Popen(["sleep", "0.2"])
    assert core_tools._wait_or_user_interrupt(proc, 10, _FakeSession()) is None


def test_wait_gives_way_when_the_user_speaks(monkeypatch):
    monkeypatch.setattr(core_tools.settings, "shell_user_interrupt_after_s", 1)
    session = _FakeSession()
    proc = subprocess.Popen(["sleep", "30"])
    try:
        threading.Timer(0.3, lambda: setattr(session, "user_steer_seq", 1)).start()
        t0 = time.monotonic()
        elapsed = core_tools._wait_or_user_interrupt(proc, 30, session)
        assert elapsed is not None and 1 <= elapsed <= 3
        assert time.monotonic() - t0 < 5
    finally:
        proc.kill()
        proc.wait()


def test_wait_still_times_out_normally(monkeypatch):
    monkeypatch.setattr(core_tools.settings, "shell_user_interrupt_after_s", 1)
    proc = subprocess.Popen(["sleep", "30"])
    try:
        try:
            core_tools._wait_or_user_interrupt(proc, 1.5, _FakeSession())
            raise AssertionError("expected TimeoutExpired")
        except subprocess.TimeoutExpired:
            pass
    finally:
        proc.kill()
        proc.wait()


def test_bash_stops_a_sleep_and_says_why(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    monkeypatch.setattr(core_tools.settings, "shell_user_interrupt_after_s", 1)
    session = _FakeSession(speak_after_start=0.3)
    monkeypatch.setattr(core_tools, "_get_session_from_context", lambda _ctx: session)
    t0 = time.monotonic()
    out, meta = core_tools.bash("echo started; sleep 60", timeout=120, _context={"session_id": "x"})
    assert time.monotonic() - t0 < 10
    assert out.startswith("[interrupted after")
    assert "started" in out, "partial output is kept"
    assert meta["interrupted_by_user"] is True and meta["was_error"] is False


def test_steer_from_the_user_bumps_the_counter_and_a_worker_does_not():
    import inspect

    from sessions.manager import SessionManager

    src = inspect.getsource(SessionManager.steer)
    assert 'if origin == "user":' in src and "session.user_steer_seq += 1" in src


def test_last_active_follows_the_newest_message(monkeypatch):
    from core.extensions import session_tools
    from db import models as db

    sid = db.create_session(title="long job")
    db.add_message(sid, "user", "go")
    monkeypatch.setattr(
        db, "get_messages", lambda _sid: [{"role": "user", "content": "go", "created_at": "2099-01-01T00:00:00+00:00"}]
    )
    out = session_tools.read_session_summary(sid)
    assert "Last active: 2099-01-01T00:00:00" in out
