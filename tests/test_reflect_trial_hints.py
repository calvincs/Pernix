"""Tests for the stuck-mode trial-hint peek + lessons recall in sessions/hooks.py.

Boundary checks:
- pending proposals are surfaced only when stuck-conditions hold
- trial_uses increments on injection; trial_successes increments on subsequent verdict=='pass'
- proposals in status 'applied' / 'rejected' never surface
- approval flow is never triggered by trial activity (status stays 'pending')
"""

import json

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_pending_proposal(skill_name: str = "test-skill", confidence: float = 0.8) -> str:
    from db import models as db

    return db.add_skill_proposal(
        workflow_name=None,
        run_id=None,
        skill_name=skill_name,
        section="Pre-flight",
        problem="needs check",
        proposed_change="add pre-flight",
        confidence=confidence,
        source_origin="session",
        session_id="prior-session",
    )


# ---------------------------------------------------------------------------
# get_pending_proposals_for_skill — gating
# ---------------------------------------------------------------------------


def test_pending_only_pending_status_surfaced():
    from db import models as db

    pending_id = _seed_pending_proposal()
    applied_id = _seed_pending_proposal()
    rejected_id = _seed_pending_proposal()
    db.resolve_skill_proposal(applied_id, "applied")
    db.resolve_skill_proposal(rejected_id, "rejected")

    pending = db.get_pending_proposals_for_skill("test-skill", limit=10)
    ids = {p["id"] for p in pending}
    assert pending_id in ids
    assert applied_id not in ids
    assert rejected_id not in ids


def test_pending_filters_by_min_confidence():
    from db import models as db

    high_id = _seed_pending_proposal(confidence=0.85)
    low_id = _seed_pending_proposal(confidence=0.55)

    pending = db.get_pending_proposals_for_skill("test-skill", min_confidence=0.6)
    ids = {p["id"] for p in pending}
    assert high_id in ids
    assert low_id not in ids


def test_pending_unknown_skill_returns_empty():
    from db import models as db

    pending = db.get_pending_proposals_for_skill("does-not-exist")
    assert pending == []


# ---------------------------------------------------------------------------
# Trial counter helpers
# ---------------------------------------------------------------------------


def test_record_proposal_trial_use_increments():
    from db import models as db

    pid = _seed_pending_proposal()
    db.record_proposal_trial_use(pid)
    db.record_proposal_trial_use(pid)
    p = db.get_skill_proposal(pid)
    assert p["trial_uses"] == 2
    assert p["last_trial_at"] is not None
    # Status must remain pending — trial activity never auto-approves.
    assert p["status"] == "pending"


def test_record_proposal_trial_success_increments():
    from db import models as db

    pid = _seed_pending_proposal()
    db.record_proposal_trial_success(pid)
    p = db.get_skill_proposal(pid)
    assert p["trial_successes"] == 1
    # Counter does not change status.
    assert p["status"] == "pending"


def test_many_trial_successes_does_not_auto_approve():
    """Hard rule: trial signal is advisory; status only changes via the API."""
    from db import models as db

    pid = _seed_pending_proposal()
    for _ in range(50):
        db.record_proposal_trial_use(pid)
        db.record_proposal_trial_success(pid)
    p = db.get_skill_proposal(pid)
    assert p["status"] == "pending"
    assert p["trial_uses"] == 50
    assert p["trial_successes"] == 50


# ---------------------------------------------------------------------------
# add_skill_proposal — session-origin defaults
# ---------------------------------------------------------------------------


def test_add_skill_proposal_session_origin_defaults():
    from db import models as db

    pid = db.add_skill_proposal(
        workflow_name=None,
        run_id=None,
        skill_name="x",
        section="Notes",
        problem="p",
        proposed_change="c",
        confidence=0.7,
        source_origin="session",
        session_id="s123",
    )
    row = db.get_skill_proposal(pid)
    assert row["source_origin"] == "session"
    assert row["session_id"] == "s123"
    assert row["workflow_name"] is None
    assert row["run_id"] is None
    assert row["trial_uses"] == 0
    assert row["trial_successes"] == 0


def test_add_skill_proposal_workflow_origin_default():
    """Existing workflow callers (no source_origin kwarg) still get 'workflow'."""
    from db import models as db

    pid = db.add_skill_proposal(
        workflow_name="my-wf",
        run_id="run-1",
        skill_name="x",
        section="Notes",
        problem="p",
        proposed_change="c",
        confidence=0.7,
        source_step_id="step-1",
        source_worker_id="worker-1",
    )
    row = db.get_skill_proposal(pid)
    assert row["source_origin"] == "workflow"
    assert row["workflow_name"] == "my-wf"
    assert row["session_id"] is None


# ---------------------------------------------------------------------------
# list_skill_proposals — origin filter
# ---------------------------------------------------------------------------


def test_list_proposals_filter_by_origin():
    from db import models as db

    db.add_skill_proposal(
        workflow_name="wf",
        run_id="r1",
        skill_name="x",
        section="",
        problem="p",
        proposed_change="c",
        confidence=0.7,
    )
    db.add_skill_proposal(
        workflow_name=None,
        run_id=None,
        skill_name="x",
        section="",
        problem="p",
        proposed_change="c",
        confidence=0.7,
        source_origin="session",
        session_id="s1",
    )
    workflow_only = db.list_skill_proposals(source_origin="workflow")
    session_only = db.list_skill_proposals(source_origin="session")
    both = db.list_skill_proposals()
    assert len(workflow_only) == 1
    assert len(session_only) == 1
    assert len(both) == 2
