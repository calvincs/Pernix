"""Pernix — Adaptive Layer engine (adaptation plan 4a/4c).

Covers the §6 done-when list: byte-for-byte rollback including
delete-when-before-absent, version-moved rejection with sibling survival,
evidence refusal, apply-on-approve minting a swept batch, and deferral
while work is active.
"""

import json

import pytest

from core.adaptive import engine
from core.adaptive.engine import (
    AdaptiveError,
    apply_batch,
    approve_proposal,
    compute_risk,
    drain_pending,
    queue_edits,
    rollback,
    validate_edit,
)
from db import models as db


@pytest.fixture(autouse=True)
def _adaptive_on(monkeypatch, tmp_path):
    monkeypatch.setattr("config.settings.adaptive_enabled", True)
    monkeypatch.setattr("config.settings.adaptive_auto_apply", True)
    import core.adaptive.render as render

    monkeypatch.setattr(render, "MIRROR_PATH", tmp_path / "ADAPTIVE.md")


def _edit(action="create", kind="routing_hint", title="Prefer rg", content="use rg over grep", **kw):
    e = {"action": action, "kind": kind, "title": title, "content": content, "evidence": ["pm:abc"]}
    e.update(kw)
    return e


def _idle_manager(monkeypatch, active=False):
    from types import SimpleNamespace

    monkeypatch.setattr(
        "sessions.manager.get_manager",
        lambda: SimpleNamespace(has_active_work=lambda: active),
    )


# ---------------------------------------------------------------------------
# Risk + validation
# ---------------------------------------------------------------------------


def test_compute_risk_tiers():
    assert compute_risk("routing_hint", "global", "create", "refine") == "low"
    assert compute_risk("prompt_note", "session:x", "update", "candor") == "low"
    assert compute_risk("policy", "global", "create", "refine") == "high"
    # Delete of another producer's entry escalates.
    assert compute_risk("routing_hint", "global", "delete", "refine", entry_source="candor") == "high"
    assert compute_risk("routing_hint", "global", "delete", "refine", entry_source="refine") == "low"
    # Global edits from Dream escalate.
    assert compute_risk("routing_hint", "global", "create", "dream") == "high"
    assert compute_risk("routing_hint", "session:x", "create", "dream") == "low"


def test_edit_without_evidence_refused():
    e = _edit()
    e["evidence"] = []
    assert "evidence" in validate_edit(e, "refine")
    result = queue_edits([e], "refine")
    assert result["queued"] == 0 and result["rejected"]


def test_prompt_note_length_cap():
    e = _edit(kind="prompt_note", content="x" * 401)
    assert "400" in validate_edit(e, "refine")


# ---------------------------------------------------------------------------
# Queue split
# ---------------------------------------------------------------------------


def test_queue_splits_low_and_high():
    result = queue_edits(
        [
            _edit(title="hint one"),
            _edit(kind="policy", title="always gate deploys", content="rule text"),
        ],
        "refine",
    )
    assert result["queued"] == 1 and result["batch_id"]
    assert result["gated"] == 1 and result["proposal_id"]
    batch = db.adaptive_get_batch(result["batch_id"])
    assert batch["status"] == "pending"
    prop = db.adaptive_get_proposal(result["proposal_id"])
    assert prop["status"] == "pending"
    assert json.loads(prop["payload_json"])[0]["kind"] == "policy"


def test_queue_all_gated_when_auto_apply_off(monkeypatch):
    monkeypatch.setattr("config.settings.adaptive_auto_apply", False)
    result = queue_edits([_edit()], "refine")
    assert result["batch_id"] is None and result["gated"] == 1


def test_auto_apply_off_mints_one_proposal_per_tier(monkeypatch):
    """Approval must not be all-or-nothing across risk tiers."""
    monkeypatch.setattr("config.settings.adaptive_auto_apply", False)
    result = queue_edits(
        [_edit(title="safe hint"), _edit(kind="policy", title="gate deploys", content="rule text")],
        "refine",
    )
    assert result["batch_id"] is None and result["gated"] == 2
    assert len(result["proposal_ids"]) == 2
    assert result["proposal_id"] == result["proposal_ids"][0]

    payloads = {pid: json.loads(db.adaptive_get_proposal(pid)["payload_json"]) for pid in result["proposal_ids"]}
    # Each proposal is single-tier and the two tiers are separated.
    assert all(len({e["risk"] for e in pl}) == 1 for pl in payloads.values())
    assert sorted(e["kind"] for pl in payloads.values() for e in pl) == ["policy", "routing_hint"]
    low_pid = next(pid for pid, pl in payloads.items() if pl[0]["risk"] == "low")

    # Taking the low-risk one leaves the high-risk one untouched and pending.
    approve_proposal(low_pid)
    assert db.adaptive_get_entry("safe-hint")["status"] == "active"
    assert db.adaptive_get_entry("gate-deploys") is None
    assert [p["id"] for p in db.adaptive_list_proposals(status="pending")] == [
        pid for pid in result["proposal_ids"] if pid != low_pid
    ]


def test_queue_noop_when_disabled(monkeypatch):
    monkeypatch.setattr("config.settings.adaptive_enabled", False)
    result = queue_edits([_edit()], "refine")
    assert result == {
        "batch_id": None,
        "queued": 0,
        "proposal_id": None,
        "proposal_ids": [],
        "gated": 0,
        "rejected": [],
    }
    assert db.adaptive_list_batches() == []


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def test_apply_create_update_delete_lifecycle():
    r = queue_edits([_edit(title="lifecycle hint")], "refine")
    apply_batch(r["batch_id"])
    entry = db.adaptive_get_entry("lifecycle-hint")
    assert entry["version"] == 1 and entry["status"] == "active" and entry["source"] == "refine"

    r2 = queue_edits(
        [_edit(action="update", entry_id="lifecycle-hint", content="use rg always", baseline_version=1)],
        "refine",
    )
    apply_batch(r2["batch_id"], actor="user")  # auto would hit the 24h cooldown
    entry = db.adaptive_get_entry("lifecycle-hint")
    assert entry["version"] == 2 and entry["content"] == "use rg always"

    r3 = queue_edits([_edit(action="delete", entry_id="lifecycle-hint", baseline_version=2)], "refine")
    apply_batch(r3["batch_id"], actor="user")
    entry = db.adaptive_get_entry("lifecycle-hint")
    assert entry["status"] == "deleted" and entry["version"] == 3
    # Deleted entries drop out of default listings.
    assert db.adaptive_list_entries(kind="routing_hint") == []


def test_version_moved_rejected_sibling_applies():
    apply_batch(queue_edits([_edit(title="stable"), _edit(title="moving")], "refine")["batch_id"])

    # Plan against v1 of both, then move "moving" to v2 behind the plan's back.
    stale_batch = queue_edits(
        [
            _edit(action="update", entry_id="stable", content="new stable", baseline_version=1),
            _edit(action="update", entry_id="moving", content="stale write", baseline_version=1),
        ],
        "refine",
    )["batch_id"]
    apply_batch(
        queue_edits(
            [_edit(action="update", entry_id="moving", content="moved first", baseline_version=1)],
            "refine",
        )["batch_id"],
        actor="user",  # dodge the auto cooldown; version check is the subject
    )

    result = apply_batch(stale_batch, actor="user")
    assert result["applied"] == ["stable"]
    assert len(result["rejected"]) == 1
    assert "changed during planning" in result["rejected"][0]["reason"]
    assert db.adaptive_get_entry("moving")["content"] == "moved first"
    assert db.adaptive_get_entry("stable")["content"] == "new stable"


def test_per_kind_cap(monkeypatch):
    monkeypatch.setattr("config.settings.adaptive_max_entries_per_kind", 2)
    b = queue_edits([_edit(title=f"hint {i}") for i in range(3)], "refine")["batch_id"]
    result = apply_batch(b)
    assert len(result["applied"]) == 2
    assert "max entries" in result["rejected"][0]["reason"]


def test_all_rejected_batch_is_not_applied(monkeypatch):
    """Nothing landed → terminal 'rejected', never 'applied'."""
    monkeypatch.setattr("config.settings.adaptive_max_entries_per_kind", 0)
    b = queue_edits([_edit(title="never lands")], "refine")["batch_id"]
    result = apply_batch(b)
    assert result["applied"] == [] and result["status"] == "rejected"
    assert db.adaptive_get_batch(b)["status"] == "rejected"

    # 'rejected' is inert: the tripwire sweep only considers applied/suspect,
    # so a batch that changed nothing can never be flagged for a regression.
    from core.adaptive.tripwire import evaluate_tripwire

    monkeypatch.setattr("core.canary.scan_canaries", lambda *a, **k: [])
    assert evaluate_tripwire() == []


def test_delete_entry_frees_cap_and_journals(monkeypatch):
    from core.adaptive import delete_entry

    monkeypatch.setattr("config.settings.adaptive_max_entries_per_kind", 1)
    apply_batch(queue_edits([_edit(title="occupier")], "refine")["batch_id"])
    blocked = apply_batch(queue_edits([_edit(title="newcomer")], "refine")["batch_id"], actor="user")
    assert "max entries" in blocked["rejected"][0]["reason"]

    out = delete_entry("occupier")
    assert out["status"] == "deleted" and out["version"] == 2
    # Soft delete: live count and the default listing both drop it.
    assert db.adaptive_entry_count("routing_hint") == 0
    assert db.adaptive_list_entries(kind="routing_hint") == []

    ev = db.adaptive_list_events(entry_id="occupier")[0]
    assert ev["action"] == "delete" and ev["actor"] == "human"
    assert json.loads(ev["before_json"])["status"] == "active"

    # Slot freed — the create that was capped out now lands.
    assert apply_batch(queue_edits([_edit(title="newcomer")], "refine")["batch_id"], actor="user")["applied"] == [
        "newcomer"
    ]
    with pytest.raises(AdaptiveError, match="not active"):
        delete_entry("occupier")
    # The delete is journaled, so it is rollback-able like any other edit.
    rollback(event_id=ev["id"])
    assert db.adaptive_get_entry("occupier")["status"] == "active"


def test_auto_cooldown_blocks_rapid_updates():
    apply_batch(queue_edits([_edit(title="cool")], "refine")["batch_id"])
    b = queue_edits([_edit(action="update", entry_id="cool", content="again", baseline_version=1)], "refine")[
        "batch_id"
    ]
    result = apply_batch(b, actor="auto")
    assert result["applied"] == [] and "cooldown" in result["rejected"][0]["reason"]
    # A human applying the same edit is not subject to the auto cooldown.
    b2 = queue_edits([_edit(action="update", entry_id="cool", content="again", baseline_version=1)], "refine")[
        "batch_id"
    ]
    assert apply_batch(b2, actor="user")["applied"] == ["cool"]


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


def test_rollback_batch_byte_for_byte():
    apply_batch(queue_edits([_edit(title="keeper", content="v1 text")], "refine")["batch_id"])
    before_snapshot = db.adaptive_get_entry("keeper")

    # A 3-edit batch: update keeper, create newbie, create second.
    b = queue_edits(
        [
            _edit(action="update", entry_id="keeper", content="v2 text", baseline_version=1),
            _edit(title="newbie", content="fresh"),
            _edit(kind="prompt_note", title="second", content="note"),
        ],
        "refine",
    )["batch_id"]
    result = apply_batch(b, actor="user")
    assert len(result["applied"]) == 3
    assert db.adaptive_get_entry("keeper")["version"] == 2

    rb = rollback(batch_id=b)
    assert len(rb["reversed_events"]) == 3
    # Update reversed byte-for-byte against before_json.
    assert db.adaptive_get_entry("keeper") == before_snapshot
    # Creates reversed by hard delete (before absent).
    assert db.adaptive_get_entry("newbie") is None
    assert db.adaptive_get_entry("second") is None
    assert db.adaptive_get_batch(b)["status"] == "rolled_back"
    # Rollback is itself journaled.
    actions = [e["action"] for e in db.adaptive_events_for_batch(b)]
    assert actions.count("rollback") == 3


def test_rollback_single_event_and_guards():
    apply_batch(queue_edits([_edit(title="solo")], "refine")["batch_id"])
    ev = db.adaptive_list_events(entry_id="solo")[0]
    rollback(event_id=ev["id"])
    assert db.adaptive_get_entry("solo") is None
    rb_ev = db.adaptive_list_events(entry_id="solo")[0]
    assert rb_ev["action"] == "rollback"
    with pytest.raises(AdaptiveError, match="rollback event"):
        rollback(event_id=rb_ev["id"])
    with pytest.raises(AdaptiveError):
        rollback()
    with pytest.raises(AdaptiveError):
        rollback(batch_id="nope", event_id=1)


# ---------------------------------------------------------------------------
# Drain (idle window discipline)
# ---------------------------------------------------------------------------


def test_drain_defers_while_active(monkeypatch):
    _idle_manager(monkeypatch, active=True)
    r = queue_edits([_edit(title="waiting")], "refine")
    out = drain_pending()
    assert out["applied_batches"] == [] and out["deferred"] == 1
    assert db.adaptive_get_batch(r["batch_id"])["status"] == "pending"

    _idle_manager(monkeypatch, active=False)
    out = drain_pending()
    assert out["applied_batches"] == [r["batch_id"]]
    assert db.adaptive_get_entry("waiting") is not None


def test_drain_respects_daily_cap(monkeypatch):
    _idle_manager(monkeypatch, active=False)
    monkeypatch.setattr("config.settings.adaptive_max_auto_applies_per_day", 1)
    queue_edits([_edit(title="first")], "refine")
    queue_edits([_edit(title="second hint")], "refine")
    out = drain_pending()
    assert len(out["applied_batches"]) == 1 and out["deferred"] == 1
    # Cap consumed — next drain applies nothing.
    assert drain_pending()["applied_batches"] == []


# ---------------------------------------------------------------------------
# Apply-on-approve
# ---------------------------------------------------------------------------


def test_approve_applies_and_enqueues_sweep(monkeypatch):
    swept = []
    monkeypatch.setattr(
        "core.extensions.scheduling.enqueue_post_batch_sweep",
        lambda batch_id: swept.append(batch_id) or True,
    )
    pid = queue_edits([_edit(kind="policy", title="gate deploys", content="rule")], "refine")["proposal_id"]
    result = approve_proposal(pid)
    assert result["applied"] == ["gate-deploys"]
    assert db.adaptive_get_entry("gate-deploys")["risk"] == "high"
    assert db.adaptive_get_proposal(pid)["status"] == "approved"
    assert swept == [result["batch_id"]]
    # The event chain records the proposal linkage.
    ev = db.adaptive_list_events(entry_id="gate-deploys")[0]
    assert ev["proposal_id"] == pid and ev["actor"] == "user"
    with pytest.raises(AdaptiveError, match="not pending"):
        approve_proposal(pid)


def test_mirror_regenerated(monkeypatch, tmp_path):
    import core.adaptive.render as render

    apply_batch(queue_edits([_edit(title="mirrored")], "refine")["batch_id"])
    text = render.MIRROR_PATH.read_text()
    assert "mirrored" in text and "read-only" in text.lower()
