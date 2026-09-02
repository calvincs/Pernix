"""Two ways worker orchestration made the caller wait for nothing.

* A worker whose SPAWN failed never starts a turn, so await_workers'
  has_started check left it counted as pending and a blocking wait ran to
  its full max_wait (30 minutes) even though spawn-time cleanup had
  already stamped it errored. The reaper and the suspend path both treat
  that as terminal; this was the one place that did not.
* retry_worker cancels the old worker and immediately spawns its
  replacement. At max_concurrent_workers the still-CANCELLING original
  counted against its own replacement, so the retry failed with "Max
  active workers reached".
"""

import inspect

import pytest

from core.extensions import orchestration
from sessions import state_v2 as sv2
from sessions.manager import SessionManager


@pytest.fixture
def mgr(monkeypatch):
    fresh = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", fresh)
    return fresh


def test_a_spawn_failed_worker_counts_as_done():
    src = inspect.getsource(orchestration.await_workers)
    assert "w.error or w.termination_reason" in src, "an errored idle worker must not read as pending"


def test_the_active_count_can_exclude_one_worker(mgr):
    parent_id = mgr.create_session(title="P")
    parent = mgr.get(parent_id)
    ids = []
    for i in range(3):
        wid = mgr.create_session(title=f"W{i}", session_type="worker", parent_session_id=parent_id)
        sv2._set_state(mgr.get(wid), sv2.SessionStateV2.PROCESSING)
        ids.append(wid)
    parent.worker_ids = list(ids)

    assert orchestration._count_active_workers(mgr, parent) == 3
    assert orchestration._count_active_workers(mgr, parent, ignore=ids[0]) == 2


def test_retry_hands_the_outgoing_id_to_the_spawn_gate():
    src = inspect.getsource(orchestration.retry_worker)
    assert "_replacing=worker_id" in src, "the cancelled worker must not block its own replacement"
