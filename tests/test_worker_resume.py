"""Tests for resumable workers (spec Feature 5) — resume_worker's three faces:
release a paused worker, refuse a running one, and REVIVE a terminated or
reaped one from its persisted state.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import core.extensions.orchestration as orch
from config import settings
from db import models as db
from sessions import state_v2 as sv2
from sessions.manager import SessionManager


@pytest.fixture
def mgr(monkeypatch):
    fresh = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", fresh)
    return fresh


@pytest.fixture
def loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    loop.close()


@pytest.fixture
def prompt_calls(mgr, monkeypatch):
    """Record manager.prompt calls instead of running real turns."""
    calls: list[tuple[str, str]] = []

    async def fake_prompt(session_id, message, system_prompt="", idempotency_key=None):
        calls.append((session_id, message))

    monkeypatch.setattr(mgr, "prompt", fake_prompt)
    return calls


def _driver(loop, fn, *args, **kwargs):
    """Run a sync orchestration function inside the running loop so
    call_on_loop takes its inline branch, then drain scheduled coroutines."""

    async def _run():
        result = fn(*args, **kwargs)
        await asyncio.sleep(0)  # let run_coroutine_threadsafe callbacks land
        return result

    return loop.run_until_complete(_run())


def _make_worker(mgr, *, terminal: str | None = "cancelled", turn_id: int = 3):
    parent_id = mgr.create_session(title="Parent")
    wid = mgr.create_session(title="W", session_type="worker", parent_session_id=parent_id)
    db.add_message(wid, "user", "original task")
    db.add_message(wid, "assistant", "partial work done")
    w = mgr.get(wid)
    w._turn_id = turn_id
    w.termination_reason = terminal
    return parent_id, wid


def test_resume_paused_worker_releases(mgr, loop):
    _parent, wid = _make_worker(mgr, terminal=None)
    w = mgr.get(wid)
    w._state_v2 = sv2.SessionStateV2.PAUSED
    w.pause_event.clear()
    out = _driver(loop, orch.resume_worker, wid)
    assert "resumed" in out
    assert "revived" not in out
    assert w.pause_event.is_set()


def test_resume_running_worker_refused(mgr, loop, prompt_calls):
    _parent, wid = _make_worker(mgr, terminal=None)
    mgr.get(wid)._state_v2 = sv2.SessionStateV2.PROCESSING
    out = _driver(loop, orch.resume_worker, wid)
    assert "already running" in out
    assert not prompt_calls


def test_resume_queued_worker_refused(mgr, loop, prompt_calls):
    _parent, wid = _make_worker(mgr, terminal=None, turn_id=0)
    out = _driver(loop, orch.resume_worker, wid)
    assert "not started" in out
    assert not prompt_calls


def test_revive_terminal_worker_in_memory(mgr, loop, prompt_calls, tmp_path):
    parent_id, wid = _make_worker(mgr, terminal="cancelled")
    parent = mgr.get(parent_id)
    parent.worker_ids.clear()  # simulate a parent that lost track

    # Stale auto-stamp from the failed run — must be cleared on revival.
    ws = Path(settings.workspace_dir)
    ws.mkdir(parents=True, exist_ok=True)
    stamp = ws / f".worker_{wid[:12]}_summary.md"
    stamp.write_text("# CANCELLED (worker stopped before reflect ran)\n\nold")

    out = _driver(loop, orch.resume_worker, wid, "focus on part two")
    assert "revived" in out
    assert "cancelled" in out  # names the prior termination

    assert prompt_calls, "revival must start a continuation turn"
    sid, msg = prompt_calls[0]
    assert sid == wid
    assert "previous run ended: cancelled" in msg
    assert "focus on part two" in msg  # operator note rode along
    assert "CONTINUE the original task" in msg

    assert not stamp.exists(), "stale summary stamp must be cleared"
    assert wid in parent.worker_ids, "worker re-attached to parent"
    assert mgr.get(wid).termination_reason is None
    # Parent got the worker.resumed event.
    assert any(e.get("type") == "worker.resumed" for e in parent.events)


def test_revive_reaped_worker_rehydrates(mgr, loop, prompt_calls):
    parent_id, wid = _make_worker(mgr, terminal="round_ceiling")
    db.update_session(wid, worker_kind="explore", model_override="")
    # Reap from memory; the DB row and messages persist.
    mgr._sessions.pop(wid)

    out = _driver(loop, orch.resume_worker, wid)
    assert "revived" in out
    assert prompt_calls and prompt_calls[0][0] == wid
    w = mgr.get(wid)
    assert w is not None, "worker rehydrated into memory"
    assert w.tool_allowlist is not None and "file_read" in w.tool_allowlist


def test_revive_refuses_non_worker_row(mgr, loop, prompt_calls):
    sid = mgr.create_session(title="Normal")
    mgr._sessions.pop(sid)
    out = _driver(loop, orch.resume_worker, sid)
    assert "not a worker session" in out
    assert not prompt_calls


def test_revive_respects_worker_cap(mgr, loop, prompt_calls, monkeypatch):
    _parent, wid = _make_worker(mgr, terminal="error")
    monkeypatch.setattr("config.settings.max_concurrent_workers", 0)
    out = _driver(loop, orch.resume_worker, wid)
    assert out.startswith("Error: Max active workers")
    assert not prompt_calls


def test_revive_auto_resume_parent_registers_watch(mgr, loop, prompt_calls):
    parent_id, wid = _make_worker(mgr, terminal="cancelled")
    parent = mgr.get(parent_id)
    out = _driver(loop, orch.resume_worker, wid, "", True)  # auto_resume_parent=True
    assert "revived" in out
    assert wid in parent._watched_worker_ids
    # Persisted, so a restart mid-revival keeps the watch.
    row = db.get_session(parent_id)
    assert wid in (row.get("watched_worker_ids") or "")


def test_revive_extends_parent_budget(mgr, loop, prompt_calls, monkeypatch):
    parent_id, wid = _make_worker(mgr, terminal="error")
    calls: list[tuple] = []
    monkeypatch.setattr("core.llm.client.extend_session_budget", lambda sid, secs: calls.append((sid, secs)))
    monkeypatch.setattr("config.settings.llm_session_timeout", 1800)
    out = _driver(loop, orch.resume_worker, wid)
    assert "revived" in out
    assert calls and calls[0][0] == parent_id
    assert calls[0][1] == 2 * 1800.0


def test_revive_clears_stale_model_override(mgr, loop, prompt_calls, monkeypatch):
    _parent, wid = _make_worker(mgr, terminal="error")
    w = mgr.get(wid)
    w.model_override = "gone/model:404"

    class _Reg:
        def resolve_model_id(self, m):
            return m

        def get_model_info(self, m):
            return None  # model vanished

    class _Client:
        class router:
            registry = _Reg()

    monkeypatch.setattr("core.llm.client.get_llm_client", lambda: _Client())
    out = _driver(loop, orch.resume_worker, wid)
    assert "revived" in out
    assert w.model_override is None
    assert "no longer available" in prompt_calls[0][1]
