"""get_worker_result told the model a running worker had failed.

In the 2026-11-02 task test, the parent asked for a result right after
spawn_worker. The worker was still scouting, so it had no output yet, and the
reply was "produced no output. It may have failed silently or timed out.
Consider retrying". The model called retry_worker, which cancelled the live
worker. It did that twice and reported four failures. The two retries in fact
finished.

A worker that is queued or mid-turn now says so and points at await_workers.
retry_worker is gone: it cancelled with no check and dropped the task. Keep the
work with resume_worker, or start fresh with spawn_worker, and a worker with no
result shows its original task so the parent can reuse it.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from core.extensions import orchestration as orch
from core.tools.registry import ToolRegistry
from db import models as db


@pytest.fixture
def worker(monkeypatch):
    wid = db.create_session(session_type="worker")
    obj = SimpleNamespace(error="", termination_reason=None, _turn_id=0)
    monkeypatch.setattr("sessions.manager.get_manager", lambda: SimpleNamespace(get=lambda _: obj))
    monkeypatch.setattr(orch, "_worker_is_mid_turn", lambda _: False)
    return wid, obj


def _says_running(text: str) -> bool:
    return "still running" in text and "await_workers" in text and "produced no output" not in text


def test_a_queued_worker_is_not_called_failed(worker):
    wid, _ = worker

    assert _says_running(orch.get_worker_result(wid))


def test_a_worker_mid_turn_is_not_called_failed(worker, monkeypatch):
    wid, obj = worker
    obj._turn_id = 1
    monkeypatch.setattr(orch, "_worker_is_mid_turn", lambda _: True)

    assert _says_running(orch.get_worker_result(wid))


def test_a_finished_worker_with_nothing_is_still_reported(worker):
    wid, obj = worker
    obj._turn_id = 1

    assert "finished with no output" in orch.get_worker_result(wid)


def test_a_finished_worker_reloaded_after_a_restart_is_not_called_running(worker, monkeypatch):
    wid, _ = worker
    monkeypatch.setattr(db, "latest_turn_id", lambda _: 3)

    assert "finished with no output" in orch.get_worker_result(wid)


def test_a_worker_error_wins(worker):
    wid, obj = worker
    obj.error = "model down"

    assert "FAILED with error: model down" in orch.get_worker_result(wid)


def test_a_worker_with_no_result_shows_its_task_and_the_way_forward(worker):
    wid, obj = worker
    obj._turn_id = 1
    db.add_message(wid, "user", "check report.md against sales.csv")

    out = orch.get_worker_result(wid)

    assert "Original task: check report.md against sales.csv" in out
    assert "resume_worker" in out and "spawn_worker" in out


def test_a_long_task_is_cut_short(worker):
    wid, obj = worker
    obj._turn_id = 1
    db.add_message(wid, "user", "x" * 5000)

    assert len(orch.get_worker_result(wid)) < 2000


def test_retry_worker_is_gone():
    reg = ToolRegistry()
    orch.register(reg)

    assert not reg.exists("retry_worker")
    assert reg.exists("spawn_worker") and reg.exists("resume_worker")
    assert "retry_worker" not in inspect.getsource(orch)


def test_a_cancel_names_who_did_it_and_the_workers_age(worker):
    wid, _ = worker
    state = orch.TrustState.__new__(orch.TrustState)
    state.term_reason = "cancelled"
    state.worker_id = "w"

    assert orch._age_note(wid).startswith(" at ")
    assert orch._age_note("no-such-worker") == ""
    assert "not a worker failure" in state.interruption_note()
