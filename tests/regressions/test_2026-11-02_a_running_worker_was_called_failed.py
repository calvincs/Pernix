"""get_worker_result told the model a running worker had failed.

In the 2026-11-02 task test, the parent asked for a result right after
spawn_worker. The worker was still scouting, so it had no output yet, and the
reply was "produced no output. It may have failed silently or timed out.
Consider retrying". The model called retry_worker, which cancelled the live
worker. It did that twice and reported four failures. The two retries in fact
finished.

A worker that is queued or mid-turn now says so and points at await_workers.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.extensions import orchestration as orch
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

    assert "produced no output" in orch.get_worker_result(wid)


def test_a_finished_worker_reloaded_after_a_restart_is_not_called_running(worker, monkeypatch):
    wid, _ = worker
    monkeypatch.setattr(db, "latest_turn_id", lambda _: 3)

    assert "produced no output" in orch.get_worker_result(wid)


def test_a_worker_error_wins(worker):
    wid, obj = worker
    obj.error = "model down"

    assert "FAILED with error: model down" in orch.get_worker_result(wid)
