"""A worker inherits its parent's goal, bills that goal, and used to be the
one session the goal's budget could not stop.

spawn_worker stamps the parent's active_goal_id on the worker, every
token_usage row the worker writes carries it, and goal_token_usage() sums
across sessions — so the worker's spend is what exhausts the budget. But the
mid-turn checkpoint resolved the goal with db.get_active_goal(running
session), and a worker owns no goal row. The lookup returned None, the
checkpoint read that as "no goal, nothing to enforce", and every worker in a
fan-out kept spending against a budget that had already stopped the parent —
while the parent sat suspended in await_workers waiting for them.

The check now resolves by goal id (db.get_goal), the same identity the spend
is attributed to, so ownership no longer decides who the budget binds.
"""

from __future__ import annotations

import pytest

from core.agent import _goal_budget_exceeded, _pre_round_gate
from db import models as db
from sessions.manager import SessionManager


@pytest.fixture(autouse=True)
def _goals_on(monkeypatch):
    monkeypatch.setattr("config.settings.goals_enabled", True)


@pytest.fixture
def fanout(monkeypatch):
    """A parent-owned goal with a 100-token budget, blown by its worker."""
    mgr = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", mgr)

    parent_id = mgr.create_session(title="parent")
    worker_id = mgr.create_session(title="worker", session_type="worker", parent_session_id=parent_id)
    goal_id = db.create_goal(parent_id, "budgeted fan-out", token_budget=100)

    db.add_token_usage(parent_id, model="m", total_tokens=30, goal_id=goal_id)
    db.add_token_usage(worker_id, model="m", total_tokens=500, goal_id=goal_id)

    parent = mgr.get(parent_id)
    worker = mgr.get(worker_id)
    for s in (parent, worker):
        s.active_goal_id = goal_id  # what spawn_worker stamps on the worker
        s.pause_event.set()
    return parent, worker, goal_id


def test_the_worker_owns_no_goal_but_its_spend_is_what_blew_the_budget(fanout):
    """The premise: the DB agrees the budget is gone and the worker owns nothing."""
    _parent, worker, goal_id = fanout
    assert db.goal_token_usage(goal_id) == 530
    assert db.get_active_goal(worker.session_id) is None
    assert db.get_goal(goal_id)["session_id"] != worker.session_id


def test_the_worker_reports_the_inherited_overrun(fanout):
    _parent, _worker, goal_id = fanout
    reason = _goal_budget_exceeded(goal_id)
    assert reason is not None, "the shipped bug returned None for the worker's inherited goal"
    assert "530" in reason and "100" in reason


async def test_the_workers_next_round_gate_breaks_on_budget_exhausted(fanout):
    _parent, worker, _goal_id = fanout
    assert await _pre_round_gate(worker, worker.session_id, 3) == "break"
    assert worker.termination_reason == "budget_exhausted"


async def test_the_parent_still_breaks_on_the_goal_it_owns(fanout):
    """The owner-scoped case must not regress out of the by-id rewrite."""
    parent, _worker, _goal_id = fanout
    assert await _pre_round_gate(parent, parent.session_id, 3) == "break"
    assert parent.termination_reason == "budget_exhausted"


async def test_a_worker_inside_the_budget_keeps_running(fanout):
    """Enforcement, not a blanket stop for workers."""
    _parent, worker, goal_id = fanout
    db.update_goal(goal_id, token_budget=10_000)
    assert await _pre_round_gate(worker, worker.session_id, 3) == "run"
    assert worker.termination_reason is None


async def test_a_settled_goal_stops_binding_anyone(fanout):
    """get_active_goal's status filter was load-bearing: a completed goal is
    settled, so its spent budget must not break the next turn's rounds."""
    _parent, worker, goal_id = fanout
    db.update_goal(goal_id, status="complete")
    assert _goal_budget_exceeded(goal_id) is None
    assert await _pre_round_gate(worker, worker.session_id, 3) == "run"


def test_a_time_budget_binds_the_worker_too(fanout):
    _parent, _worker, goal_id = fanout
    db.update_goal(goal_id, token_budget=0, time_budget_s=1)
    with db.connect_sessions() as conn:
        conn.execute("UPDATE session_goals SET started_at = ? WHERE id = ?", ("2020-01-01T00:00:00+00:00", goal_id))
    reason = _goal_budget_exceeded(goal_id)
    assert reason is not None and "time budget" in reason
