"""TELOS store: object round-trip, ids, claims + humility caps, trace ledger."""

from __future__ import annotations

import json

import pytest

from config import settings
from core.telos.store import EPISTEMIC_CAPS, TelosObject, TelosStore


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setattr(settings, "telos_enabled", True)
    return TelosStore.open()


def test_open_creates_layout(store):
    for d in ("config", "questions", "soup", "goals", "claims", "alarms", "ledgers/first_person", "ledgers/trace"):
        assert (store.root / d).is_dir()


def test_object_roundtrip(store):
    obj = TelosObject(
        id=store.mint_id("hypothesis"),
        kind="hypothesis",
        meta={"statement": "test claim", "band": "far", "status": "soup", "eig": 0.4},
        body="Free-form notes.",
    )
    store.write(obj)
    back = store.read("hypothesis", obj.id)
    assert back is not None
    assert back.get("statement") == "test claim"
    assert back.get("eig") == 0.4
    assert back.body == "Free-form notes."
    assert back.get("created_at") and back.get("updated_at")


def test_question_ids_are_dated_and_sequential(store):
    q1 = store.add_question("Why does the first thing happen at all?")
    q2 = store.add_question("Why does the second thing happen at all?")
    assert q1.id.startswith("q_") and q1.id.endswith("_001")
    assert q2.id.endswith("_002")


def test_list_filters_by_frontmatter(store):
    store.add_question("An open question about behavior?")
    q = store.add_question("A soon-closed question about behavior?")
    store.update(q, state="closed")
    assert len(store.list_questions(state="open")) == 1
    assert len(store.list_questions(state="closed")) == 1


def test_question_duplicate_detection(store):
    store.add_question("Why did tool 'browse_web' fail 3/4 calls this turn?")
    assert store.question_is_duplicate("Why did tool 'browse_web' fail 3/4 calls this turn?")
    assert not store.question_is_duplicate("Why is memory recall slower on Tuesdays?")


def test_claim_caps_enforced(store):
    c = store.commit_claim("I am excellent at everything", "self_report", confidence=0.99)
    assert c.get("confidence") == EPISTEMIC_CAPS["self_report"]
    c2 = store.commit_claim("Observed p99 under load", "observation", confidence=0.999)
    assert c2.get("confidence") == EPISTEMIC_CAPS["observation"]
    # observation_of_self escapes the self_report cap
    c3 = store.commit_claim("Trace-corroborated self claim", "observation_of_self", confidence=0.9)
    assert c3.get("confidence") == 0.9


def test_trace_append_only_and_readback(store):
    store.trace_append("turn", {"session": "s1", "termination": "complete"})
    store.trace_append("spend", {"goal": "g_root", "tokens": 100})
    events = store.trace_events(days=1)
    assert [e["type"] for e in events] == ["root_seeded", "turn", "spend"] or [e["type"] for e in events] == [
        "turn",
        "spend",
    ]
    only_spend = store.trace_events(days=1, types={"spend"})
    assert len(only_spend) == 1 and only_spend[0]["tokens"] == 100
    # Bad lines are skipped, not fatal.
    with store.trace_path().open("a") as f:
        f.write("not json\n")
    assert len(store.trace_events(days=1)) == len(events)


def test_ensure_root_seeds_once(store):
    r1 = store.ensure_root()
    r2 = store.ensure_root()
    assert r1.id == "g_root" == r2.id
    assert r1.get("completable") is False
    assert r1.get("satisfaction_predicate") is None
    assert (store.root / "config" / "telos.yaml").is_file()
    seeded = [e for e in store.trace_events(days=1, types={"root_seeded"})]
    assert len(seeded) == 1


def test_band_mix_normalizes_and_defaults(store):
    assert store.band_mix() == {"near": 0.50, "mid": 0.30, "far": 0.20}
    store.set_state(soup_bands={"near": 1, "mid": 1, "far": 2})
    mix = store.band_mix()
    assert abs(mix["far"] - 0.5) < 1e-9
    assert abs(sum(mix.values()) - 1.0) < 1e-9


def test_serendipity_budget_clamped(store):
    store.set_state(serendipity_budget=0.9)
    assert store.serendipity_budget() == 0.5
    store.set_state(serendipity_budget="junk")
    assert store.serendipity_budget() == settings.telos_serendipity_budget


async def test_run_step_inert_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "telos_enabled", False)
    from pathlib import Path

    from core.telos import run_slow_loops, run_step

    stats = await run_step(lambda: False)
    assert all(v == 0 for v in stats.values())
    assert await run_slow_loops() == {}
    assert not Path(settings.telos_dir).exists()
