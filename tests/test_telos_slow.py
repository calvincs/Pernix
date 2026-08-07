"""TELOS slow loops: ordo, binding, hevel, entropy, reconciliation."""

from __future__ import annotations

import json
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

    r3 = run_binding_monitor(store)
    # frozen goal is no longer active, so it drops out of monitoring
    assert r3["alarms"] == []


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
    # All executed hypotheses in one near-band bucket -> entropy 0.
    for i in range(5):
        store.write(
            TelosObject(
                id=store.mint_id("hypothesis"),
                kind="hypothesis",
                meta={"band": "near", "status": "gated", "mapping": {"source_domain": "same"}, "question": "q"},
            )
        )
        store.trace_append("hypothesis", {"band": "near", "question": "q"})
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
        store.trace_append("hypothesis", {"band": band, "question": "q"})
    result = run_entropy_control(store)
    assert not result["starving"] and result["adjusted"]
    assert store.band_mix()["far"] < 0.35
    assert store.serendipity_budget() < 0.3


# --- reconciliation (mechanical part) --------------------------------------


def test_reconcile_flags_unsupported_refs(store):
    claims = [
        {"claim": "I resolved a question.", "refs": ["T1", "T2"]},
        {"claim": "I invented a memory.", "refs": ["T99"]},
    ]
    rec = reconcile(store, claims, trace_count=10)
    assert len(rec["supported"]) == 1
    assert len(rec["unsupported"]) == 1
    assert rec["divergence"] == 0.5


async def test_full_reconciliation_writes_ledger_and_alarms(store, mock_llm_client, monkeypatch):
    monkeypatch.setattr(settings, "telos_divergence_max", 0.15)
    for i in range(4):
        store.trace_append("turn", {"session": f"s{i}", "termination": "complete"})
    mock_llm_client.responses = [
        _chat_resp(
            json.dumps(
                [
                    {"claim": "I completed four turns.", "refs": ["T1"]},
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
