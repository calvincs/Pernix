"""Pernix — Persistent goals + continuations (adaptation plan 3b)."""

import asyncio
from types import SimpleNamespace

import pytest

from db import models as db


@pytest.fixture(autouse=True)
def _goals_on(monkeypatch):
    monkeypatch.setattr("config.settings.goals_enabled", True)


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------


def test_goal_crud_and_single_active():
    sid = db.create_session(title="g")
    gid = db.create_goal(sid, "finish the report end to end", token_budget=1000, continuation_budget=2)
    assert gid is not None
    assert db.create_goal(sid, "second goal should be refused") is None

    goal = db.get_active_goal(sid)
    assert goal["objective"].startswith("finish the report")
    assert goal["continuation_budget"] == 2

    db.update_goal(gid, status="complete")
    assert db.get_active_goal(sid) is None
    assert db.create_goal(sid, "a new goal after completion is fine") is not None


def test_goal_token_usage_sums_across_sessions():
    sid = db.create_session(title="parent")
    wid = db.create_session(title="worker", session_type="worker", parent_session_id=sid)
    gid = db.create_goal(sid, "budgeted goal with worker fan-out")
    db.add_token_usage(sid, model="m", total_tokens=100, goal_id=gid)
    db.add_token_usage(wid, model="m", total_tokens=250, goal_id=gid)  # worker bills its own sid
    db.add_token_usage(sid, model="m", total_tokens=999)  # unstamped — not the goal's
    assert db.goal_token_usage(gid) == 350


def test_reconcile_orphan_goals():
    sid = db.create_session(title="doomed")
    gid = db.create_goal(sid, "goal that will be orphaned soon")
    db.delete_session(sid)
    assert db.reconcile_orphan_goals() == 1
    with db.connect_sessions() as conn:
        row = conn.execute("SELECT status FROM session_goals WHERE id = ?", (gid,)).fetchone()
    assert row["status"] == "error"


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def test_goal_tools_lifecycle(monkeypatch):
    from core.tools.builtin.goal_tools import goal_complete, goal_create, goal_status, goal_update

    monkeypatch.setattr("config.settings.gates_enabled", False)
    sid = db.create_session(title="tools")
    ctx = {"session_id": sid}

    out = goal_create("write and verify the deployment runbook", continuation_budget=1, _context=ctx)
    assert "created" in out
    assert "already has a live goal" in goal_create("another goal entirely", _context=ctx)

    status = goal_status(_context=ctx)
    assert "deployment runbook" in status and "[active]" in status

    assert "updated" in goal_update(status="paused", _context=ctx)
    assert "[paused]" in goal_status(_context=ctx)

    assert "completed" in goal_complete(summary="done", _context=ctx)
    assert "No live goal" in goal_status(_context=ctx)


def test_goal_complete_refused_on_failing_gate(monkeypatch):
    from core.tools.builtin.goal_tools import goal_complete, goal_create

    monkeypatch.setattr("config.settings.gates_enabled", True)
    from core.tools.paths import workspace

    workspace().mkdir(parents=True, exist_ok=True)
    sid = db.create_session(title="gated")
    ctx = {"session_id": sid}
    goal_create("goal guarded by a deterministic gate", _context=ctx)
    db.add_gate(sid, "must-pass", "exit 1", scope="goal")

    out = goal_complete(_context=ctx)
    assert out.startswith("Error:") and "must-pass" in out
    assert db.get_active_goal(sid) is not None  # still live

    db.add_gate(sid, "must-pass", "true", scope="goal")  # upsert to passing
    assert "completed" in goal_complete(_context=ctx)


def test_goal_complete_gates_honor_workspace_override(monkeypatch, tmp_path):
    """goal_complete must resolve the workspace the way core.gates does: a
    session with a workspace_override (canary run, isolated task) runs its
    gates there, not in the shared global workspace."""
    import core.tools.builtin.goal_tools as gt
    from core.tools.paths import workspace

    monkeypatch.setattr("config.settings.gates_enabled", True)
    workspace().mkdir(parents=True, exist_ok=True)
    ws = tmp_path / "override-ws"
    ws.mkdir()
    (ws / "override-marker.txt").write_text("here")

    sid = db.create_session(title="override")
    ctx = {"session_id": sid}
    gt.goal_create("goal whose gates must run in the overridden workspace", _context=ctx)
    # Passes only when the gate's cwd is the override workspace.
    db.add_gate(sid, "cwd-check", "test -f override-marker.txt", scope="goal")

    # No override anywhere -> global workspace -> the gate fails.
    monkeypatch.setattr(gt, "_session", lambda _c: None)
    out = gt.goal_complete(_context=ctx)
    assert out.startswith("Error:") and "cwd-check" in out

    # Override carried on the tool context is honored.
    assert "completed" in gt.goal_complete(_context={**ctx, "workspace_override": str(ws)})


def test_goal_complete_prefers_live_session_workspace_override(monkeypatch, tmp_path):
    import core.tools.builtin.goal_tools as gt
    from core.tools.paths import workspace

    monkeypatch.setattr("config.settings.gates_enabled", True)
    workspace().mkdir(parents=True, exist_ok=True)
    ws = tmp_path / "live-ws"
    ws.mkdir()
    (ws / "override-marker.txt").write_text("here")

    sid = db.create_session(title="live-override")
    ctx = {"session_id": sid}
    gt.goal_create("goal completed from a session with a live override", _context=ctx)
    db.add_gate(sid, "cwd-check", "test -f override-marker.txt", scope="goal")

    live = SimpleNamespace(workspace_override=str(ws), active_goal_id=1)
    monkeypatch.setattr(gt, "_session", lambda _c: live)
    assert "completed" in gt.goal_complete(_context=ctx)
    assert live.active_goal_id is None


# ---------------------------------------------------------------------------
# Continuations (manager logic, exercised directly)
# ---------------------------------------------------------------------------


def _fake_session(sid, termination="complete"):
    from collections import deque

    from sessions import state_v2 as sv2

    s = SimpleNamespace(
        session_id=sid,
        session_type="normal",
        termination_reason=termination,
        pending_messages=deque(),
        emit_event=lambda e: None,
    )
    # Continuations require FINALIZING; fake the state probe.
    return s


def _run_continuation(manager_like, session):
    from sessions.manager import SessionManager

    return asyncio.run(SessionManager._maybe_enqueue_goal_continuation(manager_like, session))


@pytest.fixture
def mgr(monkeypatch):
    from sessions import state_v2 as sv2
    from sessions.manager import SessionManager

    monkeypatch.setattr(sv2, "_current_state", lambda s: sv2.SessionStateV2.FINALIZING)
    m = SimpleNamespace(broadcast=lambda *a, **k: None)
    m._limit_goal = lambda session, goal, reason: SessionManager._limit_goal(m, session, goal, reason)
    return m


def test_continuation_enqueued_with_ordinal(mgr):
    sid = db.create_session(title="cont")
    db.create_goal(sid, "long objective needing continuations", continuation_budget=2)
    s = _fake_session(sid, termination="round_ceiling")
    _run_continuation(mgr, s)
    assert len(s.pending_messages) == 1
    msg = s.pending_messages[0]
    assert "[goal continuation 1/2]" in msg.message
    assert msg.msg_id is None  # synthetic — no budget reset on dispatch
    assert db.get_active_goal(sid)["continuations_used"] == 1

    # Second qualifying end uses ordinal 2; third is refused (budget spent).
    s2 = _fake_session(sid, termination="complete")
    _run_continuation(mgr, s2)
    assert "[goal continuation 2/2]" in s2.pending_messages[0].message
    s3 = _fake_session(sid)
    _run_continuation(mgr, s3)
    assert len(s3.pending_messages) == 0


def test_continuation_refused_on_wrong_conditions(mgr):
    sid = db.create_session(title="refuse")
    db.create_goal(sid, "objective for refusal cases", continuation_budget=5)

    # Cancelled turns need a human.
    s = _fake_session(sid, termination="cancelled")
    _run_continuation(mgr, s)
    assert not s.pending_messages

    # Queued user messages outrank the machine.
    s2 = _fake_session(sid, termination="complete")
    s2.pending_messages.append("queued-user-msg")
    _run_continuation(mgr, s2)
    assert len(s2.pending_messages) == 1  # nothing added

    # Paused goals don't continue.
    goal = db.get_active_goal(sid)
    db.update_goal(goal["id"], status="paused")
    s3 = _fake_session(sid, termination="complete")
    _run_continuation(mgr, s3)
    assert not s3.pending_messages


def test_token_budget_exhaustion_limits_goal(mgr, monkeypatch):
    notified = []
    monkeypatch.setattr(
        "db.models.add_notification",
        lambda **k: notified.append(k) or "nid",
    )
    sid = db.create_session(title="limit")
    gid = db.create_goal(sid, "tightly budgeted objective", token_budget=100, continuation_budget=5)
    db.add_token_usage(sid, model="m", total_tokens=150, goal_id=gid)

    s = _fake_session(sid, termination="complete")
    _run_continuation(mgr, s)
    assert not s.pending_messages
    assert db.get_active_goal(sid)["status"] == "budget_limited"
    assert notified and "budget-limited" in notified[0]["title"]


def test_budget_exhausted_termination_extends_session_budget(mgr, monkeypatch):
    extended = []
    monkeypatch.setattr("core.llm.client.extend_session_budget", lambda sid, secs: extended.append((sid, secs)) or 0.0)
    sid = db.create_session(title="extend")
    db.create_goal(sid, "objective that outlives the llm session clock", continuation_budget=1)

    s = _fake_session(sid, termination="budget_exhausted")
    _run_continuation(mgr, s)
    assert len(s.pending_messages) == 1
    assert extended and extended[0][0] == sid  # inherited clock deliberately extended


# ---------------------------------------------------------------------------
# Compiler blocks
# ---------------------------------------------------------------------------


def test_goal_block_and_burn(monkeypatch):
    from core.context.compiler import _build_goal_block, _build_goal_burn

    sid = db.create_session(title="block")
    assert _build_goal_block(sid) == ""  # no goal -> no block

    gid = db.create_goal(sid, "compile-visible objective", token_budget=1000, continuation_budget=3)
    block = _build_goal_block(sid)
    assert "[ACTIVE GOAL" in block and "compile-visible objective" in block
    assert "goal_complete" in block  # completion doctrine stated
    assert "token budget 1,000" in block

    db.add_token_usage(sid, model="m", total_tokens=400, goal_id=gid)
    burn = _build_goal_burn(sid)
    assert "400" in burn and "1,000" in burn

    monkeypatch.setattr("config.settings.goals_enabled", False)
    assert _build_goal_block(sid) == ""
