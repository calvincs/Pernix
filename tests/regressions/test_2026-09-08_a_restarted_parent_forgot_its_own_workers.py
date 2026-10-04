"""A restarted parent was told it had zero workers, and ordered to collect them.

`AgentSession.worker_ids` was never persisted and never rebuilt. Hydration
restored state_v2, the turn id, the watch-set, the model override, the kind
allowlist and the space fields — everything except the one list that says which
children this session owns. So after a restart the boot reconcile hydrated each
watched worker without attaching it, and `_build_resume_message` enumerated an
empty list: the parent received "[Watched workers have completed — 0 total]"
followed by an instruction to call `get_worker_result` for every worker "listed
above". `check_workers` answered "No workers spawned." with three durable
children in the database.

Three more things died with it. A hydrated worker's `termination_reason` was
None, so a round-capped child read as a clean one — and `get_worker_result`'s
durable fallback was unreachable, because it only fired when the worker was
absent from memory and hydration guarantees it is present. A worker persisted
mid-turn was neither running nor purged, so its parent stayed parked in
AWAITING_WORKERS until the reaper's thirty-minute safety net. And the inherited
`active_goal_id` was gone, which meant the goal budget check shipped this
morning in `b257532` — guarded on `session.active_goal_id` — quietly no-opped
for every hydrated worker: the live worker breaks on budget, the revived one
ran on.

The relationship is durable already: `sessions.parent_session_id`. It is now
queried rather than mirrored. Terminal metadata comes back from the state log,
and goal attribution from the worker's own `token_usage` rows — the same
identity the spend was billed to. A goal the parent created AFTER the worker
was made is not attached to it retroactively, and a recovered relationship
never resumes a cancelled parent: `d69c641`'s authority is unchanged.
"""

from __future__ import annotations

import asyncio

import pytest

from core.agent import _pre_round_gate
from core.extensions.orchestration import check_workers, get_worker_result
from db import models as db
from sessions import state_v2 as sv2
from sessions.manager import SessionManager


@pytest.fixture(autouse=True)
def _goals_on(monkeypatch):
    monkeypatch.setattr("config.settings.goals_enabled", True)


@pytest.fixture
def live(monkeypatch):
    """A parent mid-fan-out: two workers, one capped and one clean, a live goal
    their spend is billed to, and everything the state machine would have
    written along the way."""
    mgr = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", mgr)

    parent_id = mgr.create_session(title="orchestrator")
    parent = mgr.get(parent_id)
    goal_id = db.create_goal(parent_id, "survey the harness", token_budget=100)
    parent.active_goal_id = goal_id
    sv2.transition(parent, sv2.SessionStateV2.SCOUTING, "prompt-arrived")
    sv2.transition(parent, sv2.SessionStateV2.PROCESSING, "scout-done")

    ids = []
    for i, term in enumerate((sv2.TerminationReason.ROUND_CEILING, sv2.TerminationReason.COMPLETE)):
        wid = mgr.create_session(title=f"W{i}", session_type="worker", parent_session_id=parent_id)
        db.update_session(wid, worker_kind="research", model_override=f"vendor/m{i}")
        w = mgr.get(wid)
        w.active_goal_id = goal_id  # what spawn_worker stamps
        db.add_token_usage(wid, model=f"vendor/m{i}", total_tokens=300, goal_id=goal_id)
        db.add_message(wid, "user", "go")
        db.add_message(wid, "assistant", f"W{i} deliverable")
        parent.worker_ids.append(wid)
        parent._watched_worker_ids.add(wid)
        sv2.transition(w, sv2.SessionStateV2.SCOUTING, "prompt-arrived")
        sv2.transition(w, sv2.SessionStateV2.PROCESSING, "scout-done")
        sv2.transition(
            w,
            sv2.SessionStateV2.FINALIZING,
            "round-ceiling" if term is sv2.TerminationReason.ROUND_CEILING else "loop-complete",
            termination_reason=term,
        )
        sv2.transition(w, sv2.SessionStateV2.IDLE_READY, "turn-complete")
        ids.append(wid)

    mgr._persist_watched(parent)
    sv2.transition(parent, sv2.SessionStateV2.AWAITING_WORKERS, "workers-dispatched")
    return mgr, parent_id, ids, goal_id


@pytest.fixture
def restarted(live, monkeypatch):
    """The process died. Nothing is in memory; the database is untouched."""
    _old, parent_id, ids, goal_id = live
    fresh = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", fresh)
    return fresh, parent_id, ids, goal_id


def _reconcile_and_capture(mgr, monkeypatch):
    """Run the REAL boot reconcile and the REAL resume construction, capturing
    only the final dispatch so no turn actually starts."""
    seen: dict = {}

    async def _capture(session, msg, *a, **k):
        seen["sid"] = session.session_id
        seen["msg"] = msg

    monkeypatch.setattr(mgr, "_run_agent_safe", _capture)

    async def _go():
        resumed = await mgr.reconcile_awaiting_workers()
        for _ in range(25):
            if "msg" in seen:
                break
            await asyncio.sleep(0.02)
        return resumed

    return asyncio.run(_go()), seen


def test_hydration_rebuilds_the_child_inventory(restarted):
    mgr, parent_id, ids, _goal = restarted
    parent = mgr.get_or_create(parent_id)
    assert parent.worker_ids == ids, "durable parent_session_id rows are the inventory"


def test_only_this_parents_workers_are_attached(restarted):
    """A recovered relationship is the durable one, not everything nearby."""
    mgr, parent_id, ids, _goal = restarted
    stranger_parent = mgr.create_session(title="someone else")
    stranger = mgr.create_session(title="X", session_type="worker", parent_session_id=stranger_parent)
    plain_child = mgr.create_session(title="not a worker", parent_session_id=parent_id)

    parent = mgr.get_or_create(parent_id)
    assert parent.worker_ids == ids
    assert stranger not in parent.worker_ids
    assert plain_child not in parent.worker_ids, "only session_type='worker' children count"


def test_the_resume_manifest_names_every_worker_and_its_outcome(restarted, monkeypatch):
    mgr, parent_id, ids, _goal = restarted
    resumed, seen = _reconcile_and_capture(mgr, monkeypatch)

    assert resumed == 1
    assert seen.get("sid") == parent_id
    msg = seen["msg"]
    assert "2 total" in msg
    for wid in ids:
        assert wid in msg, "the agent copies these ids verbatim into get_worker_result"
    assert "round_ceiling" in msg, "a capped worker's ending must survive the restart"
    assert "research" in msg and "vendor/m0" in msg, "kind and model identify who produced what"


def test_check_workers_after_a_restart_lists_the_durable_children(restarted):
    mgr, parent_id, ids, _goal = restarted
    mgr.get_or_create(parent_id)
    out = check_workers(_context={"session_id": parent_id})
    assert out != "No workers spawned."
    assert "2/2 done" in out
    for wid in ids:
        assert wid[:8] in out
    assert "research" in out


def test_a_hydrated_capped_worker_still_warns_its_parent(restarted):
    mgr, _parent_id, ids, _goal = restarted
    mgr.get_or_create(ids[0])
    out = get_worker_result(ids[0])
    assert "round_ceiling" in out, "the hard-cap warning is not memory-only"

    clean = get_worker_result(ids[1])
    assert "round_ceiling" not in clean, "and it is not blanket-applied either"


def test_a_running_worker_does_not_inherit_the_previous_runs_ending(restarted):
    """The durable lookup answers for a settled worker, never for a live one."""
    mgr, _parent_id, ids, _goal = restarted
    w = mgr.get_or_create(ids[0])
    w.termination_reason = None
    sv2.transition(w, sv2.SessionStateV2.SCOUTING, "prompt-arrived")
    sv2.transition(w, sv2.SessionStateV2.PROCESSING, "scout-done")
    assert "round_ceiling" not in get_worker_result(ids[0])


async def test_a_hydrated_worker_keeps_the_goal_budget_it_inherited(restarted):
    """b257532 guards on session.active_goal_id; hydration used to clear it."""
    mgr, _parent_id, ids, goal_id = restarted
    w = mgr.get_or_create(ids[0])
    assert w.active_goal_id == goal_id, "restored from the rows its spend was billed to"
    w.pause_event.set()
    assert await _pre_round_gate(w, ids[0], 3) == "break"
    assert w.termination_reason == "budget_exhausted"


def test_a_goal_created_after_the_worker_is_not_attached_retroactively(restarted):
    """The parent moved on to a new objective while old work sat unspent. That
    work is not billed to an objective that did not exist when it started."""
    mgr, parent_id, _ids, goal_id = restarted
    old_work = db.create_session(title="old", session_type="worker", parent_session_id=parent_id)
    db.update_goal(goal_id, status="complete")
    new_goal = db.create_goal(parent_id, "a different objective", token_budget=10)
    assert new_goal is not None and new_goal != goal_id
    with db.connect_sessions() as conn:
        conn.execute("UPDATE sessions SET created_at = ? WHERE id = ?", ("2000-01-01T00:00:00+00:00", old_work))

    w = mgr.get_or_create(old_work)
    assert w.active_goal_id is None


def test_an_unspent_worker_inherits_the_goal_that_predates_it(restarted):
    """No token_usage rows yet — the parent's goal still binds, because it
    already existed when the worker was created."""
    mgr, parent_id, _ids, goal_id = restarted
    quiet = db.create_session(title="quiet", session_type="worker", parent_session_id=parent_id)
    w = mgr.get_or_create(quiet)
    assert w.active_goal_id == goal_id


async def test_a_cancelled_parent_is_not_resumed_by_a_recovered_relationship(restarted, monkeypatch):
    """d69c641's authority: recovering the inventory must not restart work the
    user stopped."""
    mgr, parent_id, _ids, _goal = restarted
    parent = mgr.get_or_create(parent_id)
    parent.cancel_requested = True

    started: list = []

    async def _capture(session, msg, *a, **k):
        started.append(session.session_id)

    monkeypatch.setattr(mgr, "_run_agent_safe", _capture)
    await mgr.reconcile_awaiting_workers()
    await asyncio.sleep(0.05)

    assert started == [], "a cancelled parent must not be resumed"
    assert sv2._current_state(parent) is sv2.SessionStateV2.IDLE_READY
    assert parent.worker_ids, "the inventory is still recovered — only the resume is refused"


def test_an_interrupted_child_is_reconciled_before_the_parent_synthesizes(live, monkeypatch):
    """A worker persisted mid-turn is neither running nor finished. Until this
    was handled the parent sat in AWAITING_WORKERS for thirty minutes."""
    mgr, parent_id, ids, _goal = live
    stuck = mgr.get(ids[0])
    sv2.transition(stuck, sv2.SessionStateV2.SCOUTING, "prompt-arrived")
    sv2.transition(stuck, sv2.SessionStateV2.PROCESSING, "scout-done")

    fresh = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", fresh)
    resumed, seen = _reconcile_and_capture(fresh, monkeypatch)

    assert resumed == 1, "the parent is released rather than parked until the reaper"
    assert sv2._current_state(fresh.get(ids[0])) is sv2.SessionStateV2.IDLE_READY
    assert "interrupted" in seen["msg"].lower()


def test_a_live_worker_is_not_declared_interrupted(live, monkeypatch):
    """Only a child with no live task is reconciled — a running one is not."""
    mgr, parent_id, ids, _goal = live
    running = mgr.get(ids[0])
    sv2.transition(running, sv2.SessionStateV2.SCOUTING, "prompt-arrived")
    sv2.transition(running, sv2.SessionStateV2.PROCESSING, "scout-done")

    started: list = []

    async def _capture(session, msg, *a, **k):
        started.append(session.session_id)

    async def _go():
        forever = asyncio.get_running_loop().create_future()
        running.task = asyncio.ensure_future(asyncio.wait_for(forever, timeout=5))
        monkeypatch.setattr(mgr, "_run_agent_safe", _capture)
        n = await mgr.reconcile_awaiting_workers()
        running.task.cancel()
        return n

    assert asyncio.run(_go()) == 0
    assert started == []
