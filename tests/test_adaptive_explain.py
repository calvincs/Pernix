"""2026-09-15: the Learning tab's proposals explain themselves.

The owner, looking at "Proposals awaiting review" on the live box: "in most
circumstances I as the user have no idea what's happening or being described
here by these items." Ten rows were pending; the panel could not say that
seven were self-tests that never auto-apply, one was held because its
evidence resolved to nothing, and two were inside their 24-hour window.

Four pieces, tested here:
  - core/adaptive/explain.py turns a row into WHAT / WHY / IF-YOU-DO-NOTHING
    and a fate_kind that mirrors the veto-window drain exactly;
  - deletes are exempt from the unfounded hold (dream's retirement sweep
    cites "retired: ..." as evidence, so #499 sat on the box forever);
  - the agent can read and — when told to — decide proposals from a session
    (adaptive_proposals / adaptive_proposal_decide);
  - POST /api/adaptive/proposals/{id}/discuss mints the session the Chat
    button opens.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from core.adaptive import explain as ex
from db import models as db


@pytest.fixture(autouse=True)
def _adaptive_on(monkeypatch):
    monkeypatch.setattr("config.settings.adaptive_enabled", True)
    monkeypatch.setattr("config.settings.adaptive_auto_approve_after_hours", 24)
    monkeypatch.setattr("config.settings.adaptive_max_auto_approvals_per_day", 40)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(delta_hours: float = 0) -> str:
    return (_now() + timedelta(hours=delta_hours)).isoformat()


# The three shapes the live box held on 2026-09-15, trimmed.
CANARY_ROW = {
    "id": 245,
    "producer": "canary_propose",
    "status": "pending",
    "created_at": "2026-08-22T05:09:12+00:00",
    "rationale": (
        "[new canary 'append-only-thread-update-safety'] Pins the failure class where an agent "
        "confuses file_write with file_edit. (proposed by refine; approving writes "
        "data/canaries/x/CANARY.md and queues a vetting run; not auto-admitted: gate "
        "'history_preserved': shell metacharacters)"
    ),
    "payload_json": json.dumps(
        {
            "canary": {
                "name": "append-only-thread-update-safety",
                "prompt": "Update ONLY the CURRENT block of thread.md and keep the history.",
                "gates": [{"name": "history_preserved", "command": "diff <(a) <(b)"}],
            }
        }
    ),
    "evidence_json": json.dumps(["session:3c24fb9600a1"]),
}

RETIRE_PAYLOAD = [
    {
        "action": "delete",
        "kind": "policy",
        "scope": "global",
        "entry_id": "verify-file-writes-before-completion",
        "baseline_version": 1,
        "evidence": ["retired: originating hypothesis f5be319f no longer exists"],
        "risk": "high",
    }
]
RETIRE_EVIDENCE = json.dumps(["retired: originating hypothesis f5be319f no longer exists"])


def _policy_payload(title="no-fabrication-from-blocked-sources", content="Never cite stats from a blocked page."):
    return [{"action": "create", "kind": "policy", "scope": "global", "title": title, "content": content}]


def _grounded_evidence() -> str:
    sid = db.create_session(title="receipt-source")
    pm_id = db.add_post_mortem(sid, 1, "retry", "agent", 0.9, "m", 1, None, None, "{}")
    return json.dumps([f"pm:{pm_id}", "Session x: the agent cited numbers from a login wall.", f"session:{sid}"])


# ---------------------------------------------------------------------------
# explain_proposal
# ---------------------------------------------------------------------------


def test_canary_explains_itself_and_never_auto_applies():
    out = ex.explain_proposal(CANARY_ROW)
    assert out["fate_kind"] == ex.NEEDS_YOU
    assert "self-test" in out["what"] and "append-only-thread-update-safety" in out["what"]
    assert "history_preserved" in out["what"]
    # The reason it fell to the human path is lifted out of the rationale.
    assert "shell metacharacters" in out["fate"]
    # The machine suffix is not repeated in the WHY.
    assert "proposed by refine" not in out["why"]
    assert "session 3c24fb9600" in out["why"]


def test_policy_create_inside_window_says_when_it_applies():
    row = {
        "id": 542,
        "producer": "refine",
        "status": "pending",
        "created_at": _iso(-2),
        "rationale": "refine post-mortem lesson",
        "payload_json": json.dumps(_policy_payload()),
        "evidence_json": _grounded_evidence(),
    }
    out = ex.explain_proposal(row)
    assert out["fate_kind"] == ex.AUTO
    assert out["fate"].startswith("Applies on its own in 2")
    assert "unless you reject it first" in out["fate"]
    assert "Refine (" in out["what"] and "no-fabrication-from-blocked-sources" in out["what"]
    assert "a rule the agent must follow" in out["what"]
    assert "1 graded turn" in out["why"]
    assert "login wall" in out["why"]


def test_policy_create_past_window_says_next_quiet_moment():
    row = {
        "id": 1,
        "producer": "dream",
        "status": "pending",
        "created_at": _iso(-30),
        "rationale": "r",
        "payload_json": json.dumps(_policy_payload()),
        "evidence_json": _grounded_evidence(),
    }
    out = ex.explain_proposal(row)
    assert out["fate_kind"] == ex.AUTO
    assert "next quiet moment" in out["fate"]


def test_unfounded_create_is_held():
    row = {
        "id": 2,
        "producer": "dream",
        "status": "pending",
        "created_at": _iso(-30),
        "rationale": "r",
        "payload_json": json.dumps(_policy_payload()),
        "evidence_json": json.dumps(["it felt right"]),
    }
    out = ex.explain_proposal(row)
    assert out["fate_kind"] == ex.HELD
    assert "Held for you" in out["fate"]
    assert "it felt right" in out["why"]


def test_retirement_delete_reads_as_removal_and_goes_to_the_clock():
    from core.adaptive.engine import create_entry

    made = create_entry(
        "policy", "verify file writes before completion", "Before claiming a write landed, read the file back."
    )
    assert made["entry_id"] == "verify-file-writes-before-completion"
    row = {
        "id": 499,
        "producer": "dream",
        "status": "pending",
        "created_at": _iso(-72),
        "rationale": "dream adaptive-entry retirement (evidence no longer holds)",
        "payload_json": json.dumps(RETIRE_PAYLOAD),
        "evidence_json": RETIRE_EVIDENCE,
    }
    out = ex.explain_proposal(row)
    assert out["what"].startswith("Dream (")
    assert "remove a rule the agent must follow" in out["what"]
    assert "no longer exists" in out["what"]
    assert 'it currently says "Before claiming a write landed' in out["what"]
    assert out["fate_kind"] == ex.AUTO, out["fate"]
    assert "original evidence no longer holds" in out["why"]


def test_auto_apply_off_means_needs_you(monkeypatch):
    monkeypatch.setattr("config.settings.adaptive_auto_approve_after_hours", 0)
    row = {
        "id": 3,
        "producer": "refine",
        "status": "pending",
        "created_at": _iso(-30),
        "rationale": "r",
        "payload_json": json.dumps(_policy_payload()),
        "evidence_json": _grounded_evidence(),
    }
    out = ex.explain_proposal(row)
    assert out["fate_kind"] == ex.NEEDS_YOU
    assert "adaptive_auto_approve_after_hours" in out["fate"]


def test_explain_never_raises_on_garbage():
    out = ex.explain_proposal({"id": 9, "producer": None, "payload_json": "{not json", "evidence_json": 42})
    assert out["what"] and out["why"] and out["fate"]
    assert out["fate_kind"] in (ex.AUTO, ex.NEEDS_YOU, ex.HELD)


def test_annotate_proposal_carries_the_explanation():
    from core.adaptive import annotate_proposal

    pid = db.adaptive_add_proposal("refine", json.dumps(_policy_payload()), _grounded_evidence(), "why")
    row = annotate_proposal(db.adaptive_get_proposal(pid))
    assert row["explanation"]["fate_kind"] == ex.AUTO
    assert set(row["explanation"]) >= {"what", "why", "fate", "fate_kind", "producer_label"}


# ---------------------------------------------------------------------------
# The veto clock takes deletes even when their evidence is prose
# ---------------------------------------------------------------------------


def _backdate(pid: int, hours: float) -> None:
    from db.database import connect_sessions

    old = (_now() - timedelta(hours=hours)).isoformat()
    with connect_sessions() as conn:
        conn.execute("UPDATE adaptive_proposals SET created_at = ? WHERE id = ?", (old, pid))


def test_retirement_delete_is_not_held_as_unfounded(monkeypatch):
    """#499 on the live box: dream's retirement sweep proposed deleting a rule
    whose hypothesis had vanished, with "retired: ..." as its only evidence.
    The receipt guard held it every 20 minutes for three days. Removing a
    rule cannot make unfounded prose into policy — the clock takes it."""
    from core.adaptive import auto_approve_stale_proposals
    from core.adaptive.engine import _hold_unfounded, create_entry

    monkeypatch.setattr(
        "sessions.manager.get_manager", lambda: type("M", (), {"has_active_work": lambda self: False})()
    )
    # The entry is minutes old here; on the box it is weeks old. The auto
    # actor's edit cooldown is a different guard and not the one under test.
    monkeypatch.setattr("config.settings.adaptive_edit_cooldown_hours", 0)
    create_entry(
        "policy", "verify file writes before completion", "Before claiming a write landed, read the file back."
    )
    pid = db.adaptive_add_proposal(
        "dream", json.dumps(RETIRE_PAYLOAD), RETIRE_EVIDENCE, "dream adaptive-entry retirement"
    )
    assert _hold_unfounded(db.adaptive_get_proposal(pid)) is False
    _backdate(pid, hours=30)

    out = auto_approve_stale_proposals()
    assert out["skipped_unfounded"] == 0
    assert out["approved"] == [pid], out
    assert db.adaptive_get_proposal(pid)["status"] == "auto_approved"
    entry = db.adaptive_get_entry("verify-file-writes-before-completion")
    assert entry is None or entry.get("status") != "active"


def test_unfounded_create_is_still_held(monkeypatch):
    from core.adaptive.engine import _hold_unfounded

    pid = db.adaptive_add_proposal("dream", json.dumps(_policy_payload()), json.dumps(["it felt right"]), "r")
    assert _hold_unfounded(db.adaptive_get_proposal(pid)) is True


# ---------------------------------------------------------------------------
# Agent tools
# ---------------------------------------------------------------------------


def _registry():
    from core.tools.builtin import adaptive_tools
    from core.tools.registry import ToolRegistry

    reg = ToolRegistry()
    adaptive_tools.register(reg)
    return reg


def test_tools_register_without_agent_notes(monkeypatch):
    monkeypatch.setattr("config.settings.adaptive_agent_notes_enabled", False)
    reg = _registry()
    assert reg.get("adaptive_proposals") is not None
    assert reg.get("adaptive_proposal_decide") is not None
    assert reg.get("adaptive_note") is None
    for name in ("adaptive_proposals", "adaptive_proposal_decide"):
        assert {"canary", "worker", "cron"} <= reg.get(name).denied_session_types


def test_tool_list_and_show_read_like_the_card():
    from core.tools.builtin.adaptive_tools import adaptive_proposals

    pid = db.adaptive_add_proposal("refine", json.dumps(_policy_payload()), _grounded_evidence(), "why")
    listing = adaptive_proposals(action="list")
    assert f"#{pid} [pending] from refine" in listing
    assert "WHAT:" in listing and "IF YOU DO NOTHING:" in listing
    shown = adaptive_proposals(action="show", proposal_id=pid)
    assert "PAYLOAD (as written)" in shown and "no-fabrication-from-blocked-sources" in shown
    assert adaptive_proposals(action="show").startswith("Error")
    assert adaptive_proposals(action="show", proposal_id=999999).startswith("Error")
    assert adaptive_proposals(action="nope").startswith("Error")


def test_tool_decide_reject_and_approve():
    from core.tools.builtin.adaptive_tools import adaptive_proposal_decide

    rej = db.adaptive_add_proposal("refine", json.dumps(_policy_payload("a", "A.")), _grounded_evidence(), "why")
    assert adaptive_proposal_decide(rej, "reject").startswith("Rejected")
    assert db.adaptive_get_proposal(rej)["status"] == "rejected"
    assert adaptive_proposal_decide(rej, "reject").startswith("Nothing to do")

    payload = _policy_payload("b-rule", "B.")
    payload[0]["entry_id"] = "b-rule"
    payload[0]["evidence"] = ["pm:1"]
    payload[0]["risk"] = "high"
    app = db.adaptive_add_proposal("refine", json.dumps(payload), _grounded_evidence(), "why")
    msg = adaptive_proposal_decide(app, "approve")
    assert msg.startswith("Approved"), msg
    assert db.adaptive_get_proposal(app)["status"] == "approved"
    assert db.adaptive_get_entry("b-rule") is not None
    assert adaptive_proposal_decide(app, "maybe").startswith("Error")


# ---------------------------------------------------------------------------
# Discuss endpoint
# ---------------------------------------------------------------------------


def _client():
    from api.routers import adaptive as adaptive_router

    app = FastAPI()
    app.include_router(adaptive_router.router)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_discuss_mints_a_session_and_an_opener(monkeypatch):
    created = {}

    class _Mgr:
        def create_session(self, title="", session_type="normal", **kw):
            created["title"] = title
            created["type"] = session_type
            return "sid-discuss"

    monkeypatch.setattr("sessions.manager.get_manager", lambda: _Mgr())
    pid = db.adaptive_add_proposal("refine", json.dumps(_policy_payload()), _grounded_evidence(), "why")
    async with _client() as client:
        r = await client.post(f"/api/adaptive/proposals/{pid}/discuss")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["session_id"] == "sid-discuss"
        assert created["type"] == "normal"
        assert created["title"].startswith(f"Proposal #{pid} — ")
        assert f"proposal_id={pid}" in body["opener"]
        assert "don't approve or reject anything unless I say so" in body["opener"]
        missing = await client.post("/api/adaptive/proposals/999999/discuss")
        assert missing.status_code == 404
