"""Pernix — Adaptive Layer integration (adaptation plan 4d/4e/4f).

Producers (contract + dream promotion), consumption (compiler block
placement + flag-off byte-identity, scout hints/search), the tripwire,
and snooze Activity 15.
"""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from db import models as db


@pytest.fixture(autouse=True)
def _adaptive_on(monkeypatch, tmp_path):
    monkeypatch.setattr("config.settings.adaptive_enabled", True)
    monkeypatch.setattr("config.settings.adaptive_auto_apply", True)
    import core.adaptive.render as render

    monkeypatch.setattr(render, "MIRROR_PATH", tmp_path / "ADAPTIVE.md")


def _apply_hint(title="use rg", content="prefer rg over grep", producer="refine"):
    from core.adaptive import apply_batch, queue_edits

    r = queue_edits(
        [{"action": "create", "kind": "routing_hint", "title": title, "content": content, "evidence": ["pm:1"]}],
        producer,
    )
    apply_batch(r["batch_id"])
    return r["batch_id"]


# ---------------------------------------------------------------------------
# Producer contract
# ---------------------------------------------------------------------------


def test_refine_parse_carries_adaptive_edits():
    from core.refine import _parse_refine_output

    raw = json.dumps(
        {
            "nothing_actionable": False,
            "proposals": [],
            "lessons": [],
            "adaptive_edits": [{"action": "create", "kind": "prompt_note", "title": "t", "content": "c"}],
        }
    )
    _, _, edits, _, _ = _parse_refine_output(raw)
    assert edits and edits[0]["kind"] == "prompt_note"


def test_queue_producer_edits_stamps_session_evidence():
    from core.adaptive.contract import queue_producer_edits

    result = queue_producer_edits(
        [
            {
                "action": "create",
                "kind": "routing_hint",
                "title": "no refs",
                "content": "prefer browse_web when pages are js-heavy",
                "evidence": [],
            }
        ],
        "refine",
        session_id="sess-1234",
    )
    assert result["queued"] == 1  # evidence auto-stamped, not refused
    from core.adaptive import apply_batch

    apply_batch(result["batch_id"])
    ev = db.adaptive_list_events(entry_id="no-refs")[0]
    assert "session:sess-1234" in json.loads(ev["evidence_json"])


def test_producer_prompt_suffix_gated_on_flag(monkeypatch):
    from core.adaptive.contract import ADAPTIVE_EDITS_PROMPT

    assert "adaptive_edits" in ADAPTIVE_EDITS_PROMPT
    # queue path no-ops entirely when the layer is off.
    monkeypatch.setattr("config.settings.adaptive_enabled", False)
    from core.adaptive.contract import queue_producer_edits

    out = queue_producer_edits([{"action": "create", "kind": "routing_hint", "title": "t", "content": "c"}], "refine")
    assert out["queued"] == 0 and db.adaptive_list_batches() == []


# ---------------------------------------------------------------------------
# Consumption: scout
# ---------------------------------------------------------------------------


def test_routing_hints_block_scout_only():
    from core.adaptive.render import build_routing_hints_block

    _apply_hint(title="rg wins", content="prefer rg for code search")
    block = build_routing_hints_block()
    assert "[ADAPTIVE ROUTING HINTS]" in block and "prefer rg" in block


# ---------------------------------------------------------------------------
# Tripwire
# ---------------------------------------------------------------------------


def _seed_canary_history(batch_id, baseline_pass=True, post_pass=False, confirm=True):
    # Trailing scheduled baseline (3 runs) strictly BEFORE the batch, then
    # the batch's post_batch sweep (backdating avoids same-second ties).
    # A failing sweep normally carries its confirm-rerun row too — two
    # gate_fails is what the per-task tripwire calls a confirmed regression.
    from db.database import connect_sessions

    for _ in range(3):
        db.add_canary_run(
            "t1", "scheduled", None, "[]", baseline_pass, outcome="pass" if baseline_pass else "gate_fail"
        )
    with connect_sessions() as conn:
        conn.execute("UPDATE canary_runs SET created_at = '2026-01-01T00:00:00+00:00' WHERE trigger = 'scheduled'")
    db.adaptive_create_batch(batch_id, "refine", "[]", status="applied")
    db.add_canary_run(
        "t1", "post_batch", None, "[]", post_pass, batch_id=batch_id, outcome="pass" if post_pass else "gate_fail"
    )
    if not post_pass and confirm:
        db.add_canary_run("t1", "post_batch", None, "[]", False, batch_id=batch_id, outcome="gate_fail")


def test_tripwire_flags_canary_regression(monkeypatch):
    from core.adaptive.tripwire import evaluate_tripwire

    monkeypatch.setattr("core.canary.scan_canaries", lambda *a, **k: [])
    _seed_canary_history("ab-bad", baseline_pass=True, post_pass=False)
    actions = evaluate_tripwire()
    assert any(a["action"] == "flagged" and a["batch_id"] == "ab-bad" for a in actions)
    assert db.adaptive_get_batch("ab-bad")["status"] == "suspect"
    notes = db.get_notifications()
    assert any("tripwire" in (n.get("title") or "") for n in notes)


def test_tripwire_refuses_to_judge_against_a_zero_baseline(monkeypatch):
    """A task that was already red before the apply cannot testify.

    Under the old aggregate math a 0% baseline certified every batch clean
    (drop could never go positive) — the exact state the box was in for five
    days. The per-task form keeps the same guarantee via the green
    precondition: no green history, no verdict, and the signal reports
    unavailable rather than issuing a false all-clear.
    """
    from core.adaptive.tripwire import evaluate_tripwire

    monkeypatch.setattr("core.canary.scan_canaries", lambda *a, **k: [])
    _seed_canary_history("ab-blind", baseline_pass=False, post_pass=False)
    db.adaptive_update_batch("ab-blind", status="suspect", flagged_reason="earlier flake")
    actions = evaluate_tripwire()
    # Neither flagged nor cleared: with no usable baseline there is no verdict
    # to give, and a false all-clear would silently dismiss a real flag.
    assert not [a for a in actions if a["batch_id"] == "ab-blind"]
    assert db.adaptive_get_batch("ab-blind")["status"] == "suspect"


def test_tripwire_clears_on_clean_comparison(monkeypatch):
    from core.adaptive.tripwire import evaluate_tripwire

    monkeypatch.setattr("core.canary.scan_canaries", lambda *a, **k: [])
    _seed_canary_history("ab-fine", baseline_pass=True, post_pass=True)
    db.adaptive_update_batch("ab-fine", status="suspect", flagged_reason="earlier flake")
    actions = evaluate_tripwire()
    assert any(a["action"] == "cleared" for a in actions)
    batch = db.adaptive_get_batch("ab-fine")
    assert batch["status"] == "applied" and batch["cleared_at"]


def test_tripwire_auto_rollback_when_enabled(monkeypatch):
    from core.adaptive.tripwire import evaluate_tripwire

    monkeypatch.setattr("core.canary.scan_canaries", lambda *a, **k: [])
    monkeypatch.setattr("config.settings.adaptive_auto_rollback", True)
    # A real applied batch with an entry, then a CONFIRMED regressing sweep:
    # the original gate_fail plus its confirm-rerun gate_fail.
    batch_id = _apply_hint(title="regressor", content="bad hint")
    for _ in range(3):
        db.add_canary_run("t1", "scheduled", None, "[]", True, outcome="pass")
    # Backdate the scheduled baseline strictly before the batch's created_at.
    from db.database import connect_sessions

    with connect_sessions() as conn:
        conn.execute("UPDATE canary_runs SET created_at = '2026-01-01T00:00:00+00:00' WHERE trigger = 'scheduled'")
    db.add_canary_run("t1", "post_batch", None, "[]", False, batch_id=batch_id, outcome="gate_fail")
    db.add_canary_run("t1", "post_batch", None, "[]", False, batch_id=batch_id, outcome="gate_fail")

    actions = evaluate_tripwire()
    assert any(a["action"] == "auto_rolled_back" for a in actions)
    assert db.adaptive_get_entry("regressor") is None  # create reversed = hard delete
    assert db.adaptive_get_batch(batch_id)["status"] == "rolled_back"


def test_passive_only_suspect_expires_after_ttl(monkeypatch):
    """A passive-signal flag's comparison windows are frozen at the apply, so
    it can never self-clear — 4 batches sat suspect 12 days on the live box.
    Passive-only flags now age out; canary-confirmed flags never do."""
    from core.adaptive.tripwire import evaluate_tripwire

    monkeypatch.setattr("core.canary.scan_canaries", lambda *a, **k: [])
    monkeypatch.setattr("config.settings.adaptive_suspect_ttl_days", 7)
    from datetime import datetime, timedelta, timezone

    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()

    db.adaptive_create_batch("ab-passive", "dream", "[]", status="applied")
    db.adaptive_update_batch("ab-passive", status="suspect", flagged_reason="post-mortem retry rate 25% vs 5%")
    db.set_snooze_state("adaptive_suspect_since:ab-passive", old)

    db.adaptive_create_batch("ab-canary-hit", "dream", "[]", status="applied")
    db.adaptive_update_batch("ab-canary-hit", status="suspect", flagged_reason="canary regression: t1 (confirmed)")
    db.set_snooze_state("adaptive_suspect_since:ab-canary-hit", old)

    actions = evaluate_tripwire()
    assert any(a["action"] == "suspect_expired" and a["batch_id"] == "ab-passive" for a in actions)
    cleared = db.adaptive_get_batch("ab-passive")
    assert cleared["status"] == "applied" and cleared["cleared_at"]
    assert "auto-cleared" in cleared["flagged_reason"]
    assert db.adaptive_get_batch("ab-canary-hit")["status"] == "suspect"  # exempt


def test_legacy_suspect_without_marker_starts_its_clock_not_clears(monkeypatch):
    """A pre-v3.1 suspect has no timestamp: first sight stamps one, and the
    batch expires only after a full TTL from THEN — never instantly."""
    from core.adaptive.tripwire import evaluate_tripwire

    monkeypatch.setattr("core.canary.scan_canaries", lambda *a, **k: [])
    monkeypatch.setattr("config.settings.adaptive_suspect_ttl_days", 7)
    db.adaptive_create_batch("ab-legacy", "dream", "[]", status="applied")
    db.adaptive_update_batch("ab-legacy", status="suspect", flagged_reason="post-mortem retry rate 50% vs 30%")

    evaluate_tripwire()
    assert db.adaptive_get_batch("ab-legacy")["status"] == "suspect"  # clock started, not cleared
    assert db.get_snooze_state("adaptive_suspect_since:ab-legacy")


def test_tripwire_unconfirmed_gate_fail_flags_but_never_rolls_back(monkeypatch):
    """One gate_fail with no confirm-rerun row = the rerun itself died.
    Suspicion is warranted; an automatic rollback is not."""
    from core.adaptive.tripwire import evaluate_tripwire

    monkeypatch.setattr("core.canary.scan_canaries", lambda *a, **k: [])
    monkeypatch.setattr("config.settings.adaptive_auto_rollback", True)
    _seed_canary_history("ab-lone", baseline_pass=True, post_pass=False, confirm=False)
    actions = evaluate_tripwire()
    assert any(a["action"] == "flagged" and a["batch_id"] == "ab-lone" for a in actions)
    assert not any(a["action"] == "auto_rolled_back" for a in actions)
    assert db.adaptive_get_batch("ab-lone")["status"] == "suspect"


def test_tripwire_ignores_timeouts_errors_and_legacy_failures(monkeypatch):
    """Timeout/error/noop outcomes and pre-v30 NULL-outcome failures are
    suite-health trouble, never evidence against a batch — with nothing else
    to judge, the signal reports unavailable instead of flagging."""
    from core.adaptive.tripwire import evaluate_tripwire

    monkeypatch.setattr("core.canary.scan_canaries", lambda *a, **k: [])
    from db.database import connect_sessions

    for _ in range(3):
        db.add_canary_run("t1", "scheduled", None, "[]", True, outcome="pass")
    with connect_sessions() as conn:
        conn.execute("UPDATE canary_runs SET created_at = '2026-01-01T00:00:00+00:00' WHERE trigger = 'scheduled'")
    db.adaptive_create_batch("ab-noise", "refine", "[]", status="applied")
    db.add_canary_run("t1", "post_batch", None, "[]", False, batch_id="ab-noise", outcome="timeout")
    db.add_canary_run("t1", "post_batch", None, "[]", False, batch_id="ab-noise", outcome="error")
    db.add_canary_run("t1", "post_batch", None, "[]", False, batch_id="ab-noise", outcome="noop")
    db.add_canary_run("t1", "post_batch", None, "[]", False, batch_id="ab-noise")  # legacy NULL

    actions = evaluate_tripwire()
    assert not [a for a in actions if a["batch_id"] == "ab-noise"]
    assert db.adaptive_get_batch("ab-noise")["status"] == "applied"


def _backdate(table, created_at, where, params=()):
    from db.database import connect_sessions

    with connect_sessions() as conn:
        conn.execute(f"UPDATE {table} SET created_at = ? WHERE {where}", (created_at, *params))


def _post_mortem(created_at, verdict):
    sid = db.create_session(title="tripwire-window-test")
    pm_id = db.add_post_mortem(sid, 1, verdict, "cause", 0.9, "m", 1, None, None, "{}")
    _backdate("post_mortems", created_at, "id = ?", (pm_id,))
    return pm_id


def test_tripwire_after_window_is_the_turns_right_after_the_apply(monkeypatch):
    """The passive window must be the OLDEST turns after the apply, not the
    newest turns overall — otherwise it drifts away from the batch."""
    from core.adaptive.tripwire import evaluate_tripwire

    monkeypatch.setattr("core.canary.scan_canaries", lambda *a, **k: [])
    monkeypatch.setattr("config.settings.adaptive_tripwire_window_turns", 30)

    for h in range(30):
        _post_mortem(f"2026-01-01T00:{h:02d}:00+00:00", "pass")
    batch_id = _apply_hint(title="drifter", content="x")
    _backdate("adaptive_batches", "2026-01-02T00:00:00+00:00", "batch_id = ?", (batch_id,))
    _backdate("adaptive_events", "2026-01-02T00:00:00+00:00", "batch_id = ?", (batch_id,))
    # The turns immediately after the apply regressed...
    for h in range(30):
        _post_mortem(f"2026-01-03T00:{h:02d}:00+00:00", "retry")
    # ...and the system later recovered. Slicing the newest-first feed would
    # score the recovery and miss the regression entirely.
    for h in range(30):
        _post_mortem(f"2026-01-09T00:{h:02d}:00+00:00", "pass")

    actions = evaluate_tripwire()
    flagged = [a for a in actions if a["action"] == "flagged" and a["batch_id"] == batch_id]
    assert flagged, "the 30 regressed turns right after the apply should flag the batch"
    assert "0/30 (0%) succeeded after the apply vs 30/30 (100%) before" in flagged[0]["detail"]


def test_tripwire_anchors_on_apply_time_not_queue_time(monkeypatch):
    """A batch can sit pending for days; the baseline boundary is the APPLY."""
    from core.adaptive.tripwire import evaluate_tripwire

    monkeypatch.setattr("core.canary.scan_canaries", lambda *a, **k: [])
    batch_id = _apply_hint(title="late apply", content="x")
    _backdate("adaptive_batches", "2026-01-01T00:00:00+00:00", "batch_id = ?", (batch_id,))
    _backdate("adaptive_events", "2026-01-03T00:00:00+00:00", "batch_id = ?", (batch_id,))

    # Baseline runs land BETWEEN queue and apply — only an apply-anchored
    # boundary admits them, and without a baseline there is no signal at all.
    for _ in range(3):
        db.add_canary_run("t1", "scheduled", None, "[]", True, outcome="pass")
    _backdate("canary_runs", "2026-01-02T00:00:00+00:00", "trigger = 'scheduled'")
    db.add_canary_run("t1", "post_batch", None, "[]", False, batch_id=batch_id, outcome="gate_fail")
    db.add_canary_run("t1", "post_batch", None, "[]", False, batch_id=batch_id, outcome="gate_fail")

    actions = evaluate_tripwire()
    assert any(a["action"] == "flagged" and a["batch_id"] == batch_id for a in actions)


async def test_tripwire_dismiss_is_durable(monkeypatch):
    """cleared_at is terminal: the sweep must not re-flag a dismissed batch."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from api.routers import adaptive as adaptive_router
    from core.adaptive.tripwire import evaluate_tripwire

    monkeypatch.setattr("core.canary.scan_canaries", lambda *a, **k: [])
    _seed_canary_history("ab-dismissed", baseline_pass=True, post_pass=False)
    assert any(a["action"] == "flagged" for a in evaluate_tripwire())
    assert [n["category"] for n in db.get_notifications()] == ["adaptive.tripwire_suspect"]

    app = FastAPI()
    app.include_router(adaptive_router.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/adaptive/batches/ab-dismissed/dismiss")
    assert resp.status_code == 200 and resp.json()["cleared"]
    assert db.get_notifications() == []  # a human dismiss closes the flag's bell item too

    # Same evidence, next cycle — the dismiss holds instead of re-flagging.
    assert evaluate_tripwire() == []
    assert db.adaptive_get_batch("ab-dismissed")["status"] == "applied"


async def test_delete_entry_endpoint_frees_the_cap(monkeypatch):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from api.routers import adaptive as adaptive_router
    from core.adaptive.render import build_routing_hints_block

    monkeypatch.setattr("config.settings.adaptive_max_entries_per_kind", 1)
    _apply_hint(title="wedged", content="stale hint")
    assert "stale hint" in build_routing_hints_block()

    app = FastAPI()
    app.include_router(adaptive_router.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.delete("/api/adaptive/entries/wedged")
        assert resp.status_code == 200 and resp.json()["status"] == "deleted"
        # Second delete is a 404 — the soft delete is idempotent-by-refusal.
        assert (await client.delete("/api/adaptive/entries/wedged")).status_code == 404

    assert build_routing_hints_block() == ""  # gone from the scout prompt
    assert db.adaptive_entry_count("routing_hint") == 0  # and from the cap
    ev = db.adaptive_list_events(entry_id="wedged")[0]
    assert ev["action"] == "delete" and ev["actor"] == "human" and ev["before_json"]

    # Cap freed: a fresh hint lands where the wedged one blocked it.
    _apply_hint(title="successor", content="fresh hint")
    assert db.adaptive_get_entry("successor")["status"] == "active"


def test_tripwire_flaky_canaries_never_trip(monkeypatch):
    from core.adaptive.tripwire import evaluate_tripwire

    flaky_def = SimpleNamespace(name="t1", flaky=True)
    monkeypatch.setattr("core.canary.scan_canaries", lambda *a, **k: [flaky_def])
    _seed_canary_history("ab-flaky", baseline_pass=True, post_pass=False)
    actions = evaluate_tripwire()
    assert not any(a["action"] == "flagged" for a in actions)
    assert db.adaptive_get_batch("ab-flaky")["status"] == "applied"


# ---------------------------------------------------------------------------
# Activity 15
# ---------------------------------------------------------------------------


async def test_adaptive_step_drains_and_enqueues_sweeps(monkeypatch):
    from core.adaptive import queue_edits
    from core.snooze import SnoozeRunner

    monkeypatch.setattr("config.settings.canary_enabled", True)
    monkeypatch.setattr(
        "sessions.manager.get_manager",
        lambda: SimpleNamespace(has_active_work=lambda: False),
    )
    swept = []
    monkeypatch.setattr(
        "core.extensions.scheduling.enqueue_post_batch_sweep",
        lambda bid: swept.append(bid) or True,
    )
    r = queue_edits(
        [{"action": "create", "kind": "routing_hint", "title": "drained", "content": "x", "evidence": ["e"]}],
        "refine",
    )
    runner = SnoozeRunner.__new__(SnoozeRunner)
    runner._stats = {}
    runner._is_cancelled = lambda: False
    await SnoozeRunner._adaptive_step(runner)

    assert db.adaptive_get_entry("drained") is not None
    assert swept == [r["batch_id"]]
    # v42: "edits auto-applied" is a log-tier category, so it lives in the activity log, not the bell.
    assert any("auto-applied" in (n.get("title") or "") for n in db.list_notifications("log"))


async def test_adaptive_step_skips_sweep_and_notify_when_nothing_applied(monkeypatch):
    """A fully-rejected batch changed nothing: no sweep to join, no news."""
    from core.adaptive import queue_edits
    from core.snooze import SnoozeRunner

    monkeypatch.setattr("config.settings.canary_enabled", True)
    monkeypatch.setattr("config.settings.adaptive_max_entries_per_kind", 0)
    monkeypatch.setattr(
        "sessions.manager.get_manager",
        lambda: SimpleNamespace(has_active_work=lambda: False),
    )
    swept = []
    monkeypatch.setattr(
        "core.extensions.scheduling.enqueue_post_batch_sweep",
        lambda bid: swept.append(bid) or True,
    )
    r = queue_edits(
        [{"action": "create", "kind": "routing_hint", "title": "doomed", "content": "x", "evidence": ["e"]}],
        "refine",
    )
    runner = SnoozeRunner.__new__(SnoozeRunner)
    runner._stats = {}
    runner._is_cancelled = lambda: False
    await SnoozeRunner._adaptive_step(runner)

    assert db.adaptive_get_batch(r["batch_id"])["status"] == "rejected"
    assert swept == []
    assert runner._stats.get("adaptive_batches_applied") is None
    assert not any("auto-applied" in (n.get("title") or "") for n in db.list_notifications("log"))


# ---------------------------------------------------------------------------
# Proposal queue bounds
# ---------------------------------------------------------------------------


def test_proposal_queue_dedupes_identical_pending_payloads():
    """Re-deriving a finding from the same evidence is normal producer
    behaviour, not new information — it must not stack copies."""
    payload = json.dumps([{"action": "create", "kind": "policy", "title": "t"}])
    first = db.adaptive_add_proposal("dream", payload, "[]", "why")
    again = db.adaptive_add_proposal("dream", payload, "[]", "why")
    assert again == first
    assert db.adaptive_count_pending_proposals() == 1
    # A different producer with the same payload is a genuinely separate claim.
    assert db.adaptive_add_proposal("telos", payload, "[]", "why") != first
    assert db.adaptive_count_pending_proposals() == 2


def test_proposal_queue_refuses_past_the_cap():
    for i in range(3):
        db.adaptive_add_proposal("dream", json.dumps([{"n": i}]), "[]", f"r{i}", max_pending=3)
    assert db.adaptive_count_pending_proposals() == 3
    # Full: the next one is refused rather than silently deepening a queue
    # nobody is going to finish reading.
    assert db.adaptive_add_proposal("dream", json.dumps([{"n": 99}]), "[]", "r99", max_pending=3) is None
    assert db.adaptive_count_pending_proposals() == 3
    # Resolving one frees the slot.
    db.adaptive_resolve_proposal(db.adaptive_list_proposals(status="pending")[0]["id"], "rejected")
    assert db.adaptive_add_proposal("dream", json.dumps([{"n": 99}]), "[]", "r99", max_pending=3) is not None


def test_pending_proposals_lapse_after_the_ttl():
    """A proposal is a snapshot of evidence; approving a stale one blind is
    worse than letting the producer re-raise it from current evidence."""
    from db.database import connect_sessions

    fresh = db.adaptive_add_proposal("dream", json.dumps([{"a": 1}]), "[]", "fresh")
    stale = db.adaptive_add_proposal("dream", json.dumps([{"a": 2}]), "[]", "stale")
    old = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    with connect_sessions() as conn:
        conn.execute("UPDATE adaptive_proposals SET created_at = ? WHERE id = ?", (old, stale))

    assert db.adaptive_expire_stale_proposals(30) == 1
    assert db.adaptive_get_proposal(stale)["status"] == "expired"
    assert db.adaptive_get_proposal(fresh)["status"] == "pending"
    # Disabled by zero.
    assert db.adaptive_expire_stale_proposals(0) == 0


def test_one_producer_cannot_own_the_whole_review_queue():
    """Every one of the 126 backed-up proposals on the live box came from
    dream; once it filled the queue, Candor/Refine/Telos were refused too."""
    for i in range(3):
        assert db.adaptive_add_proposal(
            "dream", json.dumps([{"n": i}]), "[]", f"d{i}", max_pending=40, max_pending_per_producer=3
        )
    # dream is at its share — refused, even though the queue has room.
    assert (
        db.adaptive_add_proposal(
            "dream", json.dumps([{"n": 9}]), "[]", "d9", max_pending=40, max_pending_per_producer=3
        )
        is None
    )
    # A quieter producer still gets through.
    assert db.adaptive_add_proposal(
        "candor", json.dumps([{"n": 9}]), "[]", "c", max_pending=40, max_pending_per_producer=3
    )


# ---------------------------------------------------------------------------
# Proposal auto-approval — the veto window (2026-08-15)
# ---------------------------------------------------------------------------


def _backdate_proposal(pid: int, hours: float) -> None:
    from db.database import connect_sessions

    old = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with connect_sessions() as conn:
        conn.execute("UPDATE adaptive_proposals SET created_at = ? WHERE id = ?", (old, pid))


_POLICY_EDIT = [
    {
        "action": "create",
        "kind": "policy",
        "title": "veto window test",
        "content": "auto-approved policies still apply through the engine",
        "evidence": ["pm:1"],
        "entry_id": "veto-window-test",
        "scope": "global",
        "risk": "high",
    }
]


def _grounded_evidence() -> str:
    """evidence_json carrying one receipt that really resolves.

    The veto clock only takes proposals whose evidence points at something
    recorded (core/adaptive/receipts.py), so a bare "[]" now waits for a
    human — which is what these tests would otherwise be measuring.
    """
    sid = db.create_session(title="receipt-source")
    pm_id = db.add_post_mortem(sid, 1, "retry", "agent", 0.9, "m", 1, None, None, "{}")
    return json.dumps([f"pm:{pm_id}"])


def test_ripe_proposal_auto_approves_and_applies():
    """Past the veto window the system applies the proposal itself — same
    engine as a human approval, distinct terminal status for the audit trail."""
    from core.adaptive import auto_approve_stale_proposals

    pid = db.adaptive_add_proposal("dream", json.dumps(_POLICY_EDIT), _grounded_evidence(), "why")
    _backdate_proposal(pid, hours=25)

    out = auto_approve_stale_proposals()
    assert out["approved"] == [pid]
    assert db.adaptive_get_proposal(pid)["status"] == "auto_approved"
    entry = db.adaptive_get_entry("veto-window-test")
    assert entry is not None and entry["kind"] == "policy"


def test_fresh_proposal_stays_inside_the_veto_window():
    from core.adaptive import auto_approve_stale_proposals

    pid = db.adaptive_add_proposal("dream", json.dumps(_POLICY_EDIT), "[]", "why")
    _backdate_proposal(pid, hours=2)  # window is 24h

    out = auto_approve_stale_proposals()
    assert out["approved"] == []
    assert db.adaptive_get_proposal(pid)["status"] == "pending"


def test_canary_proposals_keep_their_human_gate():
    """Materializing a canary keeps invariant I6 — it never auto-approves,
    no matter how stale; canary_auto_admit is its graduated-autonomy path."""
    from core.adaptive import auto_approve_stale_proposals

    pid = db.adaptive_add_proposal("canary", json.dumps({"canary": {"name": "x"}}), "[]", "why")
    _backdate_proposal(pid, hours=200)

    out = auto_approve_stale_proposals()
    assert out["approved"] == []
    assert out["skipped_canary"] == 1
    assert db.adaptive_get_proposal(pid)["status"] == "pending"


def test_auto_approvals_respect_the_daily_cap(monkeypatch):
    from core.adaptive import auto_approve_stale_proposals

    monkeypatch.setattr("config.settings.adaptive_max_auto_approvals_per_day", 1)
    first = db.adaptive_add_proposal("dream", json.dumps([]), _grounded_evidence(), "older")
    second = db.adaptive_add_proposal("dream", json.dumps([{"a": 1}]), _grounded_evidence(), "newer")
    _backdate_proposal(first, hours=48)
    _backdate_proposal(second, hours=30)

    out = auto_approve_stale_proposals()
    assert out["approved"] == [first]  # oldest first
    assert db.adaptive_get_proposal(second)["status"] == "pending"
    # The cap counts terminal 'auto_approved' rows, so a second pass in the
    # same day has no budget left.
    assert auto_approve_stale_proposals()["approved"] == []


def test_zero_window_restores_the_human_gate(monkeypatch):
    from core.adaptive import auto_approve_stale_proposals

    monkeypatch.setattr("config.settings.adaptive_auto_approve_after_hours", 0)
    pid = db.adaptive_add_proposal("dream", json.dumps(_POLICY_EDIT), "[]", "why")
    _backdate_proposal(pid, hours=500)

    assert auto_approve_stale_proposals()["approved"] == []
    assert db.adaptive_get_proposal(pid)["status"] == "pending"


def test_routing_hints_ranked_by_outcome_share():
    """When the cap bites, hints with the best smoothed success share render
    first — a much-cited failing hint no longer crowds out a reliable one."""
    from core.adaptive.render import build_routing_hints_block

    filler = "x" * 900  # two hints alone exceed the 1600-char cap -> ranking runs
    _apply_hint(title="loser", content=f"bad guidance {filler}")
    _apply_hint(title="winner", content=f"good guidance {filler}")
    hints = {h["title"]: h["id"] for h in db.adaptive_list_entries(kind="routing_hint")}
    # loser: cited constantly, fails constantly. winner: cited less, succeeds.
    db.upsert_signal("adaptive_entry", hints["loser"], delta_failures=6, delta_reinforcements=9)
    db.upsert_signal("adaptive_entry", hints["winner"], delta_successes=3, delta_reinforcements=3)
    block = build_routing_hints_block()
    assert "winner" in block
    # With the cap at 1600 chars only the top-ranked hint fits — the failing
    # one is cut despite triple the reinforcements.
    assert "loser" not in block
