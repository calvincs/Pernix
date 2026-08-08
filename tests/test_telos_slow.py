"""TELOS slow loops: ordo, binding, hevel, entropy, reconciliation."""

from __future__ import annotations

import json
import re
import time

import pytest

from config import settings
from core.telos.binding import run_binding_monitor
from core.telos.entropy import novelty_entropy, run_entropy_control
from core.telos.hevel import audit_completion, run_hevel_rollup
from core.telos.ordo import review_dream_register, run_ordo_pass
from core.telos.reconcile import reconcile
from core.telos.store import TelosObject, TelosStore


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setattr(settings, "telos_enabled", True)
    s = TelosStore.open()
    s.ensure_root()
    return s


def _goal(store, gid, kind="task", parent="g_root", state="active", **extra):
    obj = TelosObject(
        id=gid,
        kind="goal",
        meta={
            "kind": kind,
            "parent": parent,
            "state": state,
            "completable": kind in ("milestone", "task"),
            "justification": "advances the parent question",
            "tags": [],
            **extra,
        },
    )
    store.write(obj)
    return obj


# --- ordo ------------------------------------------------------------------


def test_ordo_suspends_orphans_never_deletes(store):
    _goal(store, "g_fine")
    _goal(store, "g_orphan", parent="g_ghost")
    counts = run_ordo_pass(store)
    assert counts["orphaned"] == 1
    orphan = store.read("goal", "g_orphan")
    assert orphan.get("state") == "suspended"
    assert "orphan" in orphan.get("suspended_reason")
    # Re-attach the chain: the next pass un-suspends.
    _goal(store, "g_ghost", kind="milestone")
    counts = run_ordo_pass(store)
    assert counts["unsuspended"] == 1
    assert store.read("goal", "g_orphan").get("state") == "active"


def test_ordo_cycle_is_orphan(store):
    _goal(store, "g_a", parent="g_b")
    _goal(store, "g_b", parent="g_a")
    counts = run_ordo_pass(store)
    assert counts["orphaned"] == 2


def test_ordo_ranks_siblings_with_vapor_discount(store):
    _goal(store, "g_solid", tags=["solid"])
    _goal(store, "g_vapor", tags=["vapor"])
    store.set_state(vapor_classes=["task:vapor"])
    run_ordo_pass(store)
    solid = store.read("goal", "g_solid")
    vapor = store.read("goal", "g_vapor")
    assert solid.get("ordo_rank") == 1
    assert vapor.get("ordo_rank") == 2
    assert vapor.get("ordo_score") < solid.get("ordo_score")


def test_dream_register_flags_violations(store):
    _goal(store, "g_dream_ok", kind="dream", capability_gap=True, completable=False)
    _goal(store, "g_dream_bad", kind="dream", capability_gap=False, completable=False)
    result = review_dream_register(store)
    assert result["flagged"] == 1
    flagged = store.read("goal", "g_dream_bad")
    assert any("capability_gap" in p for p in flagged.get("register_flags"))


# --- binding ---------------------------------------------------------------


def _spend(store, goal, tokens, epoch_ms=None):
    store.trace_append("spend", {"goal": goal, "tokens": tokens})
    if epoch_ms is not None:
        # rewrite last line's epoch for slope tests (test-only surgery)
        p = store.trace_path()
        lines = p.read_text().splitlines()
        ev = json.loads(lines[-1])
        ev["epoch_ms"] = epoch_ms
        lines[-1] = json.dumps(ev)
        p.write_text("\n".join(lines) + "\n")


def test_binding_full_signature_escalates(store, monkeypatch):
    monkeypatch.setattr(settings, "telos_budget_share_max", 0.35)
    g = _goal(store, "g_shiny")
    q = store.add_question("Does the shiny thing shine?", parent_goal="g_shiny")

    now = int(time.time() * 1000)
    early, late = now - 3 * 86400_000, now - 1000
    # Budget share 100%, proxy slope positive (more hypothesis events late),
    # no narrowings, no claims.
    _spend(store, "g_shiny", 5000, epoch_ms=early)
    _spend(store, "g_shiny", 9000, epoch_ms=late)
    p = store.trace_path()
    with p.open("a") as f:
        f.write(json.dumps({"type": "hypothesis", "question": q.id, "epoch_ms": early}) + "\n")
        f.write(json.dumps({"type": "hypothesis", "question": q.id, "epoch_ms": late}) + "\n")
        f.write(json.dumps({"type": "hypothesis", "question": q.id, "epoch_ms": late}) + "\n")

    r1 = run_binding_monitor(store)
    assert r1["alarms"] == [{"target": "g_shiny", "level": 1}]
    assert store.read("goal", "g_shiny").get("state") == "active"  # L1 keeps budget

    # Time-anchored ladder: an immediate re-run (as a 4-hourly cron would
    # produce) must NOT escalate — the window hasn't elapsed.
    r1b = run_binding_monitor(store)
    assert r1b["alarms"] == [{"target": "g_shiny", "level": 1}]
    assert store.read("goal", "g_shiny").get("state") == "active"

    # Backdate the alarm's window stamp past the advance threshold: now the
    # signature has genuinely persisted a window, so the ladder climbs.
    alarm = next(a for a in store.list_alarms() if a.get("type") == "binding")
    store.update(alarm, window_advanced_at="2026-01-01T00:00:00Z")
    r2 = run_binding_monitor(store)
    assert r2["alarms"][0]["level"] == 2
    assert store.read("goal", "g_shiny").get("state") == "suspended"  # L2 freeze

    # The freeze is a hold, not an exit: the frozen goal stays monitored at
    # its current level until a window elapses.
    r3 = run_binding_monitor(store)
    assert r3["alarms"] == [{"target": "g_shiny", "level": 2}]
    assert store.read("goal", "g_shiny").get("state") == "suspended"

    # Another window with the signature still holding climbs to L3.
    store.update(store.read("alarm", alarm.id), window_advanced_at="2026-01-01T00:00:00Z")
    r4 = run_binding_monitor(store)
    assert r4["alarms"] == [{"target": "g_shiny", "level": 3}]
    assert store.read("alarm", alarm.id).get("level") == 3


def test_binding_l2_freeze_lifts_when_signature_clears(store, monkeypatch):
    """L2 must not be a dead end: a frozen goal whose signature stops holding
    is un-suspended and its alarm cleared."""
    monkeypatch.setattr(settings, "telos_budget_share_max", 0.35)
    g = _goal(store, "g_frozen")
    q = store.add_question("Is the frozen goal still bound?", parent_goal="g_frozen")
    now = int(time.time() * 1000)
    _spend(store, "g_frozen", 5000, epoch_ms=now - 3 * 86400_000)
    _spend(store, "g_frozen", 9000, epoch_ms=now - 1000)
    p = store.trace_path()
    with p.open("a") as f:
        f.write(json.dumps({"type": "hypothesis", "question": q.id, "epoch_ms": now - 3 * 86400_000}) + "\n")
        f.write(json.dumps({"type": "hypothesis", "question": q.id, "epoch_ms": now - 1000}) + "\n")
        f.write(json.dumps({"type": "hypothesis", "question": q.id, "epoch_ms": now - 900}) + "\n")

    run_binding_monitor(store)
    alarm = next(a for a in store.list_alarms() if a.get("type") == "binding")
    store.update(alarm, window_advanced_at="2026-01-01T00:00:00Z")
    run_binding_monitor(store)
    assert store.read("goal", "g_frozen").get("state") == "suspended"

    # The parent question moves — signature broken while the goal is frozen.
    with p.open("a") as f:
        f.write(json.dumps({"type": "question_narrowed", "id": q.id, "epoch_ms": now - 100}) + "\n")
    r = run_binding_monitor(store)
    assert r["alarms"] == []
    assert store.read("goal", "g_frozen").get("state") == "active"
    assert store.read("goal", "g_frozen").get("suspended_reason") is None
    assert store.read("alarm", alarm.id).get("state") == "cleared"


def test_binding_freeze_does_not_lift_someone_elses_suspension(store, monkeypatch):
    """Only the binding monitor's own freeze is released — a goal suspended
    by ordo (orphan) that happens to carry a cleared alarm stays suspended."""
    monkeypatch.setattr(settings, "telos_budget_share_max", 0.35)
    g = _goal(store, "g_orphaned_too")
    alarm = TelosObject(
        id=store.mint_id("alarm"),
        kind="alarm",
        meta={"type": "binding", "target": g.id, "level": 2, "state": "open", "windows": 2},
    )
    store.write(alarm)
    store.update(g, state="suspended", suspended_reason="orphan: parent g_ghost missing")
    r = run_binding_monitor(store)
    assert r["alarms"] == []
    assert store.read("alarm", alarm.id).get("state") == "cleared"
    assert store.read("goal", "g_orphaned_too").get("state") == "suspended"


def test_binding_ack_does_not_reset_the_ladder(store, monkeypatch):
    """Acknowledgement silences the notification, not the evidence: the next
    pass must continue the same alarm, not mint a fresh L1."""
    monkeypatch.setattr(settings, "telos_budget_share_max", 0.35)
    g = _goal(store, "g_acked")
    q = store.add_question("Does acking reset the ladder?", parent_goal="g_acked")
    now = int(time.time() * 1000)
    _spend(store, "g_acked", 5000, epoch_ms=now - 3 * 86400_000)
    _spend(store, "g_acked", 9000, epoch_ms=now - 1000)
    p = store.trace_path()
    with p.open("a") as f:
        f.write(json.dumps({"type": "hypothesis", "question": q.id, "epoch_ms": now - 3 * 86400_000}) + "\n")
        f.write(json.dumps({"type": "hypothesis", "question": q.id, "epoch_ms": now - 1000}) + "\n")
        f.write(json.dumps({"type": "hypothesis", "question": q.id, "epoch_ms": now - 900}) + "\n")

    run_binding_monitor(store)
    alarm = next(a for a in store.list_alarms() if a.get("type") == "binding")
    store.update(alarm, state="acknowledged")  # what /alarms/{id}/ack does

    r = run_binding_monitor(store)
    assert len([a for a in store.list_alarms(open_only=False) if a.get("type") == "binding"]) == 1
    assert r["alarms"] == [{"target": "g_acked", "level": 1}]
    assert store.read("alarm", alarm.id).get("state") == "acknowledged"  # still silenced

    # A window elapses: the ladder climbs from where it was, and the climb
    # reopens the alarm (a new level is new information).
    store.update(alarm, window_advanced_at="2026-01-01T00:00:00Z")
    r = run_binding_monitor(store)
    assert r["alarms"] == [{"target": "g_acked", "level": 2}]
    assert store.read("alarm", alarm.id).get("state") == "open"


def test_binding_clears_when_question_moves(store):
    g = _goal(store, "g_busy")
    q = store.add_question("Is the busy goal actually moving?", parent_goal="g_busy")
    now = int(time.time() * 1000)
    _spend(store, "g_busy", 9000, epoch_ms=now - 1000)
    p = store.trace_path()
    with p.open("a") as f:
        f.write(json.dumps({"type": "hypothesis", "question": q.id, "epoch_ms": now - 500}) + "\n")
        # The parent question narrowed — entropy reduced, signature broken.
        f.write(json.dumps({"type": "question_narrowed", "id": q.id, "epoch_ms": now - 400}) + "\n")
    r = run_binding_monitor(store)
    assert r["alarms"] == []


# --- hevel -----------------------------------------------------------------


def test_hevel_discharge_and_vapor_marking(store):
    for i in range(3):
        g = _goal(store, f"g_treadmill_{i}", tags=["treadmill"])
        store.update(g, state="completed")
        d = audit_completion(store, g)
        assert d < 0.10  # nothing narrowed, nothing spawned
    result = run_hevel_rollup(store)
    assert result["marked"] == 1
    assert "task:treadmill" in store.get_state()["vapor_classes"]
    # Vapor is a ranking, not a verdict: good discharges clear it.
    for e in range(4):
        store.trace_append("hevel_discharge", {"goal": "gx", "class": "task:treadmill", "discharge": 0.8})
    result = run_hevel_rollup(store)
    assert result["cleared"] == 1
    assert store.get_state()["vapor_classes"] == []


def test_hevel_needs_min_samples(store):
    g = _goal(store, "g_once", tags=["once"])
    store.update(g, state="completed")
    audit_completion(store, g)
    result = run_hevel_rollup(store)
    assert result.get("marked", 0) == 0


def test_hevel_discharge_rewards_spawned_questions(store):
    g = _goal(store, "g_fertile", tags=["fertile"])
    store.add_question("What did completing the fertile goal reveal?", parent_goal="g_fertile", surprise=0.9)
    store.add_question("A second revelation from the fertile goal?", parent_goal="g_fertile", surprise=0.9)
    store.update(g, state="completed")
    d = audit_completion(store, g)
    assert d >= 0.10


# --- entropy ---------------------------------------------------------------


def test_entropy_raises_temperature_when_cold(store):
    # All executed hypotheses in one near-band bucket -> entropy 0. 'gated'
    # is deliberately NOT executed (see novelty_entropy), so these run.
    for i in range(5):
        store.write(
            TelosObject(
                id=store.mint_id("hypothesis"),
                kind="hypothesis",
                meta={"band": "near", "status": "running", "mapping": {"source_domain": "same"}, "question": "q"},
            )
        )
        store.trace_append("hypothesis_resolved", {"band": "near", "question": "q"})
    assert novelty_entropy(store) == 0.0
    result = run_entropy_control(store)
    assert result["starving"] and result["adjusted"]
    assert store.band_mix()["far"] > 0.20
    assert store.serendipity_budget() > settings.telos_serendipity_budget
    alarms = [a for a in store.list_alarms() if a.get("type") == "acedia"]
    assert len(alarms) == 1


def test_entropy_decays_back_when_healthy(store):
    store.set_state(soup_bands={"near": 0.4, "mid": 0.25, "far": 0.35}, serendipity_budget=0.3)
    # Diverse executed hypotheses across bands/domains.
    for band, dom in [("near", "a"), ("mid", "b"), ("far", "c"), ("far", "d")]:
        store.write(
            TelosObject(
                id=store.mint_id("hypothesis"),
                kind="hypothesis",
                meta={"band": band, "status": "supported", "mapping": {"source_domain": dom}, "question": "q"},
            )
        )
        store.trace_append("hypothesis_resolved", {"band": band, "question": "q"})
    result = run_entropy_control(store)
    assert not result["starving"] and result["adjusted"]
    assert store.band_mix()["far"] < 0.35
    assert store.serendipity_budget() < 0.3


def _hypothesis(store, band, dom, status="supported", updated_at=None):
    obj = TelosObject(
        id=store.mint_id("hypothesis"),
        kind="hypothesis",
        meta={"band": band, "status": status, "mapping": {"source_domain": dom}, "question": "q"},
    )
    store.write(obj)
    if updated_at is not None:
        # write() always re-stamps updated_at, so backdate the file on disk.
        text = re.sub(r"^updated_at: .*$", f"updated_at: '{updated_at}'", obj.path.read_text(), flags=re.M)
        obj.path.write_text(text)
    return obj


def test_novelty_entropy_honours_its_window(store):
    """Old variety must not mask a drive that went flat this week — the days
    argument was accepted and ignored, desensitizing the acedia detector as
    history grew."""
    for band, dom in [("near", "a"), ("mid", "b"), ("far", "c"), ("far", "d")]:
        _hypothesis(store, band, dom, updated_at="2020-01-01T00:00:00Z")
    # All-time the spread is wide; the last 7 days are one collapsed bucket.
    assert novelty_entropy(store, days=4000) == 1.0
    for _ in range(4):
        _hypothesis(store, "near", "same")
    assert novelty_entropy(store, days=7) == 0.0
    assert novelty_entropy(store, days=4000) > 0.0


def test_realized_band_shares_counts_executed_not_generated(store):
    from core.telos.entropy import realized_band_shares

    # Generation events are candidates, not executions: they must not count.
    for _ in range(9):
        store.trace_append("hypothesis", {"band": "far", "question": "q"})
    assert realized_band_shares(store)["total"] == 0
    for band in ("near", "near", "far"):
        store.trace_append("hypothesis_resolved", {"band": band, "question": "q"})
    shares = realized_band_shares(store)
    assert shares["total"] == 3
    assert shares["far"] == round(1 / 3, 3)


# --- reconciliation (mechanical part) --------------------------------------


def _events() -> list[dict]:
    """A small trace window with real content to reconcile against."""
    return [
        {"type": "question_narrowed", "id": "q_2026_0807_001", "resolved": 3},
        {"type": "hypothesis_resolved", "id": "h_0012", "verdict": "refuted", "band": "far"},
        {"type": "ordo_pass", "orphaned": ["g_stray"], "reranked": []},
    ]


def test_reconcile_opens_the_cited_event(store):
    """Support requires shared evidence with the cited event, not a ref
    number in range. Both claims below cite live refs; only one bears on
    what its event actually records."""
    events = _events()
    claims = [
        {"claim": "I narrowed q_2026_0807_001 after resolving its hypotheses.", "refs": ["T1"]},
        {"claim": "I rewrote the scheduler to prefer cheaper models.", "refs": ["T3"]},
    ]
    rec = reconcile(store, claims, events)
    assert [c["refs"] for c in rec["supported"]] == [["T1"]]
    assert [c["refs"] for c in rec["unsupported"]] == [["T3"]]
    assert rec["divergence"] == 0.5


def test_reconcile_supports_on_named_identifier(store):
    """An id present in the event JSON entails the claim even when no words
    overlap — the identifier IS the shared evidence."""
    rec = reconcile(store, [{"claim": "The far-band idea h_0012 did not survive.", "refs": ["T2"]}], _events())
    assert len(rec["supported"]) == 1


def test_reconcile_flags_out_of_range_refs(store):
    """The old bounds check is retained as a precondition, not the test."""
    rec = reconcile(store, [{"claim": "I invented a memory.", "refs": ["T99"]}], _events())
    assert len(rec["unsupported"]) == 1
    assert rec["divergence"] == 1.0


def test_reconcile_rejects_a_plausible_paraphrase(store):
    """The regression this replaces: any claim citing any in-range ref used
    to be 'supported by the trace'. A fluent sentence that shares nothing
    with its cited event must now fail."""
    claims = [{"claim": "I improved my performance considerably.", "refs": ["T1", "T2", "T3"]}]
    rec = reconcile(store, claims, _events())
    assert len(rec["unsupported"]) == 1


async def test_full_reconciliation_writes_ledger_and_alarms(store, mock_llm_client, monkeypatch):
    monkeypatch.setattr(settings, "telos_divergence_max", 0.15)
    for i in range(4):
        store.trace_append("turn", {"session": f"s{i}", "termination": "complete"})
    mock_llm_client.responses = [
        _chat_resp(
            json.dumps(
                [
                    # T1 is root_seeded (ensure_root); T2..T5 are the turns.
                    {"claim": "I completed four turns.", "refs": ["T2"]},
                    {"claim": "I flew to the moon.", "refs": ["T400"]},
                ]
            )
        )
    ]
    from core.telos.reconcile import run_reconciliation

    result = await run_reconciliation(store)
    assert result["claims"] == 2 and result["divergence"] == 0.5
    files = list((store.root / "ledgers" / "first_person").glob("AUTO-*.md"))
    assert len(files) == 1
    content = files[0].read_text()
    assert "confabulation_repaired" in content
    # Supported claim escaped the cap; repaired one is capped at self_report.
    classes = {c.get("epistemic_class") for c in store.list("claim")}
    assert {"observation_of_self", "self_report"} <= classes
    alarms = [a for a in store.list_alarms() if a.get("type") == "divergence"]
    assert len(alarms) == 1
    assert store.get_state()["coherence_series"][-1]["divergence"] == 0.5


def _chat_resp(content: str):
    from core.llm.types import ChatResponse, TokenUsage

    return ChatResponse(
        content=content,
        tool_calls=None,
        usage=TokenUsage(total_tokens=200),
        model="test",
        provider="fake",
        finish_reason="stop",
    )
