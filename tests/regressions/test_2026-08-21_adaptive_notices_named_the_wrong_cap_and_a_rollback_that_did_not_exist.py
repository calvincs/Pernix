"""Regression — 2026-08-21, box session dce9a6de7f81.

Asked to explain four adaptive notifications, the agent got every proposal
id wrong. The notices gave it nothing to go on: bare ids, a queue-full text
that named the global 40-cap while the 12-row per-producer share was what
tripped, and an auto-approve text promising "roll back the batch in the
Adaptive panel" for memory corrections that never create a batch. The API
hid resolved rows behind a silent `status=pending` default and returned []
for unknown statuses, so `?status=applied` looked like "the rows vanished".
Corrective memory entries said "human-approved" for auto-approved drains.

Pinned here: every notice names the cap that tripped and the real undo
path; the API documents its status enum, rejects unknown ones, serves
`status=all` and single-id lookups; provenance follows the approver.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from db import models as db


@pytest.fixture(autouse=True)
def _adaptive_on(monkeypatch, tmp_path):
    monkeypatch.setattr("config.settings.adaptive_enabled", True)
    monkeypatch.setattr("config.settings.adaptive_auto_apply", True)
    monkeypatch.setattr("config.settings.adaptive_auto_approve_after_hours", 24)
    import core.adaptive.render as render

    monkeypatch.setattr(render, "MIRROR_PATH", tmp_path / "ADAPTIVE.md")


def _backdate(pid: int, hours: int) -> None:
    from db.database import connect_sessions

    old = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with connect_sessions() as conn:
        conn.execute("UPDATE adaptive_proposals SET created_at = ? WHERE id = ?", (old, pid))


_POLICY = [
    {
        "action": "create",
        "kind": "policy",
        "title": "veto-window-regression",
        "content": "a policy",
        "evidence": ["pm:1"],
        "entry_id": "veto-window-regression",
        "scope": "global",
        "risk": "high",
    }
]
_CORRECTION = [
    {
        "action": "memory_correction",
        "kind": "memory_stale",
        "statement": "M6 is stale",
        "files": ["cai.youtube_processing"],
        "hypothesis_id": "ebc1798aebb9ffff",
    }
]
_CANARY = {"canary": {"name": "tar-archive-loop-no-overwrite", "prompt": "p"}}


def _latest_notification() -> dict:
    return max(db.get_notifications(), key=lambda n: n["created_at"])


# ---------------------------------------------------------------------------
# Queue-full notice names the cap that tripped
# ---------------------------------------------------------------------------


def test_queue_full_notice_names_the_per_producer_share_when_that_is_what_tripped(monkeypatch):
    from core.adaptive import queue_edits

    monkeypatch.setattr("config.settings.adaptive_max_pending_proposals", 40)
    monkeypatch.setattr("config.settings.adaptive_max_pending_per_producer", 1)
    # dream already owns its one slot; a canary waits on a human alongside it.
    db.adaptive_add_proposal("dream", json.dumps([{"x": 1}]), "[]", "taken")
    db.adaptive_add_proposal("canary_propose", json.dumps(_CANARY), "[]", "new canary")

    r = queue_edits(
        [{"action": "create", "kind": "policy", "title": "p2", "content": "c", "evidence": ["pm:2"]}],
        "dream",
    )
    assert r["rejected"] and "proposal queue" in r["rejected"][0]["reason"]
    body = _latest_notification()["body"]
    assert "per-producer share" in body and "adaptive_max_pending_per_producer" in body
    assert "dream has 1 proposals pending" in body
    assert "2 are pending overall (global cap 40 — not the cap that tripped)" in body
    assert "at the 40-proposal cap" not in body  # the old, wrong claim
    assert "1 of the pending are canary proposals, which never auto-approve" in body


def test_queue_full_notice_names_the_global_cap_when_the_queue_really_is_full(monkeypatch):
    from core.adaptive import queue_edits

    monkeypatch.setattr("config.settings.adaptive_max_pending_proposals", 2)
    monkeypatch.setattr("config.settings.adaptive_max_pending_per_producer", 0)
    db.adaptive_add_proposal("telos", json.dumps([{"x": 1}]), "[]", "a")
    db.adaptive_add_proposal("candor", json.dumps([{"x": 2}]), "[]", "b")

    queue_edits([{"action": "create", "kind": "policy", "title": "p3", "content": "c", "evidence": ["pm:3"]}], "dream")
    body = _latest_notification()["body"]
    assert "2 proposals are pending, at the 2-proposal cap (adaptive_max_pending_proposals)" in body
    assert "per-producer share" not in body


# ---------------------------------------------------------------------------
# Auto-approve summaries tell the truth about the undo path
# ---------------------------------------------------------------------------


def test_auto_approve_summary_for_a_memory_correction_says_there_is_no_batch(monkeypatch):
    import core.memory.ingest as ingest_mod
    from core.adaptive import auto_approve_stale_proposals

    seen = {}

    def _fake(files, statement, source_ref="", kind="", approved_by="human"):
        seen.update(files=list(files), approved_by=approved_by, source_ref=source_ref)
        return list(files)

    monkeypatch.setattr(ingest_mod, "apply_memory_correction", _fake)
    pid = db.adaptive_add_proposal("dream", json.dumps(_CORRECTION), "[]", "why")
    _backdate(pid, 30)

    out = auto_approve_stale_proposals()
    assert out["approved"] == [pid]
    # Provenance follows the approver: the drain is not a human click.
    assert seen == {"files": ["cai.youtube_processing"], "approved_by": "auto", "source_ref": "dream:ebc1798aebb9"}
    (line,) = out["summaries"]
    assert line.startswith(f"#{pid} dream: memory correction (memory_stale) into cai.youtube_processing")
    assert "wrote a corrective entry into cai.youtube_processing" in line
    assert "No batch" in line and "deleting the entry tagged dream:ebc1798aebb9" in line
    assert "roll back" not in line.lower()


def test_auto_approve_summary_for_a_batch_names_the_batch_to_roll_back():
    from core.adaptive import auto_approve_stale_proposals

    pid = db.adaptive_add_proposal("refine", json.dumps(_POLICY), "[]", "why")
    _backdate(pid, 30)

    out = auto_approve_stale_proposals()
    (line,) = out["summaries"]
    batch_id = out["results"][0]["batch_id"]
    assert batch_id.startswith("ab-")
    assert line.startswith(
        f"#{pid} refine: create policy 'veto-window-regression' → batch {batch_id}: 1 edit(s) applied"
    )
    assert f"Undo: roll back {batch_id} in the Adaptive panel" in line


def test_describe_proposal_covers_every_payload_shape():
    from core.adaptive import annotate_proposal, describe_proposal

    canary = {
        "id": 1,
        "producer": "canary_propose",
        "status": "pending",
        "payload_json": json.dumps(_CANARY),
        "created_at": "2026-08-17T05:14:15+00:00",
    }
    assert describe_proposal(canary) == (
        "canary_propose: new canary 'tar-archive-loop-no-overwrite' (waits for a human approve/reject; never auto-approves)"
    )
    ann = annotate_proposal(canary)
    assert ann["auto_approve_exempt"] is True and ann["auto_approve_after"] is None

    correction = {
        "id": 2,
        "producer": "dream",
        "status": "pending",
        "payload_json": json.dumps(_CORRECTION),
        "created_at": "2026-08-20T01:56:10+00:00",
    }
    ann = annotate_proposal(correction)
    assert ann["summary"] == "dream: memory correction (memory_stale) into cai.youtube_processing"
    assert ann["auto_approve_exempt"] is False
    assert ann["auto_approve_after"] == "2026-08-21T01:56:10+00:00"

    review_only = {"id": 3, "producer": "dream", "status": "approved", "payload_json": "[]", "created_at": "x"}
    ann = annotate_proposal(review_only)
    assert ann["summary"].startswith("dream: review-only") and ann["auto_approve_after"] is None


def test_correction_preamble_distinguishes_the_drain_from_a_human(monkeypatch):
    from core.memory.ingest import correction_preamble

    monkeypatch.setattr("config.settings.adaptive_auto_approve_after_hours", 24)
    assert correction_preamble("memory_stale", "human", "dream:abc") == (
        "STALE-INFO CORRECTION (human-approved via adaptive review, dream:abc)"
    )
    assert correction_preamble("contradiction", "auto", "dream:abc") == (
        "CONTRADICTION RESOLVED (auto-approved after the 24h veto window, adaptive review, dream:abc)"
    )


# ---------------------------------------------------------------------------
# API: documented enum, loud on unknown status, status=all, single-id lookup
# ---------------------------------------------------------------------------


async def test_proposals_api_documents_its_statuses_and_rejects_unknown_ones():
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from api.routers import adaptive as adaptive_router

    pending = db.adaptive_add_proposal("dream", json.dumps(_CORRECTION), "[]", "p")
    resolved = db.adaptive_add_proposal("dream", json.dumps(_POLICY), "[]", "r")
    db.adaptive_resolve_proposal(resolved, "auto_approved")
    canary = db.adaptive_add_proposal("canary_propose", json.dumps(_CANARY), "[]", "c")

    app = FastAPI()
    app.include_router(adaptive_router.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # The default is still pending — the UI depends on it.
        default = (await client.get("/api/adaptive/proposals")).json()
        assert {p["id"] for p in default["proposals"]} == {pending, canary}
        assert default["statuses"] == ["pending", "approved", "auto_approved", "auto_applied", "rejected", "expired"]

        # A wrong guess is a 400 that names the enum, not an empty list.
        bad = await client.get("/api/adaptive/proposals?status=applied")
        assert bad.status_code == 400
        assert "auto_approved" in bad.json()["detail"] and "'all'" in bad.json()["detail"]

        # status=all shows resolved rows; they never vanished.
        everything = (await client.get("/api/adaptive/proposals?status=all")).json()
        assert {p["id"] for p in everything["proposals"]} == {pending, canary, resolved}
        by_id = {p["id"]: p for p in everything["proposals"]}
        assert by_id[resolved]["status"] == "auto_approved"
        assert by_id[canary]["auto_approve_exempt"] is True
        assert by_id[pending]["auto_approve_after"] is not None
        assert by_id[pending]["summary"] == "dream: memory correction (memory_stale) into cai.youtube_processing"

        # Single-id lookup, two spellings, regardless of status.
        one = (await client.get(f"/api/adaptive/proposals?id={resolved}")).json()
        assert [p["id"] for p in one["proposals"]] == [resolved]
        direct = await client.get(f"/api/adaptive/proposals/{resolved}")
        assert direct.status_code == 200 and direct.json()["status"] == "auto_approved"
        assert (await client.get("/api/adaptive/proposals/999999")).status_code == 404
