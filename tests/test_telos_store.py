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


def test_mint_id_is_unique_under_concurrent_threads(store):
    """The fast loop (snooze) and the slow loop (cron) mint in one process:
    without locking both read the same directory listing, mint the same
    c_NNNN, and the second write silently overwrites the first."""
    import threading

    minted: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def claim():
        barrier.wait()
        for _ in range(5):
            c = store.commit_claim("concurrent claim", "observation", confidence=0.5)
            with lock:
                minted.append(c.id)

    threads = [threading.Thread(target=claim) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(minted) == 40
    assert len(set(minted)) == 40  # no duplicate ids handed out
    assert len(store.list("claim")) == 40  # and none silently overwritten


def test_mint_id_skips_gaps_rather_than_colliding(store):
    """The listing count is a guess, not an authority: a deleted mid-sequence
    file must not make the next mint reuse a live id."""
    ids = [store.mint_id("alarm") for _ in range(3)]
    assert ids == ["a_0001", "a_0002", "a_0003"]
    for i in ids:
        store.write(TelosObject(id=i, kind="alarm", meta={"type": "binding"}))
    (store.root / "alarms" / "a_0002.md").unlink()  # count now 2, a_0003 lives
    assert store.mint_id("alarm") == "a_0004"


def test_mint_id_never_reuses_after_the_tail_is_pruned(store):
    """Ids must be monotonic, not merely unused.

    Retention deletes pooled hypotheses, and the old disk-count scheme would
    hand the freed numbers straight back out — silently re-pointing every
    claim, trace event and derived_from edge still naming them.
    """
    ids = [store.mint_id("hypothesis") for _ in range(3)]
    for i in ids:
        store.write(TelosObject(id=i, kind="hypothesis", meta={"status": "soup"}))
    # Prune the whole tail, exactly as retention does.
    for i in ids:
        (store.root / "soup" / f"{i}.md").unlink()
    assert not list((store.root / "soup").glob("h_*.md"))
    assert store.mint_id("hypothesis") == "h_0004"


def test_soup_prune_removes_only_aged_pooled_hypotheses(store, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from core.telos.retire import prune_speculation_pool

    monkeypatch.setattr(settings, "telos_soup_retention_days", 30)
    old = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    new = datetime.now(timezone.utc).isoformat()
    cases = [("soup", old, True), ("soup", new, False), ("gated", old, False), ("supported", old, False)]
    for status, created, _ in cases:
        store.write(
            TelosObject(
                id=store.mint_id("hypothesis"),
                kind="hypothesis",
                meta={"status": status, "created_at": created, "statement": "x"},
            )
        )

    assert prune_speculation_pool(store)["pruned"] == 1
    survivors = {h.get("status") for h in store.list_hypotheses()}
    assert survivors == {"soup", "gated", "supported"}  # queued work and the
    # falsification record both survive; only the aged pool row went.


def test_soup_prune_disabled_by_zero_retention(store, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from core.telos.retire import prune_speculation_pool

    monkeypatch.setattr(settings, "telos_soup_retention_days", 0)
    store.write(
        TelosObject(
            id=store.mint_id("hypothesis"),
            kind="hypothesis",
            meta={"status": "soup", "created_at": (datetime.now(timezone.utc) - timedelta(days=900)).isoformat()},
        )
    )
    assert prune_speculation_pool(store)["pruned"] == 0
    assert len(store.list_hypotheses()) == 1


def test_acknowledged_alarms_stay_live(store):
    """Ack silences the notification; only 'cleared' retires an alarm. The
    escalation ladder reads this list, so an acked alarm must remain."""
    for state in ("open", "acknowledged", "cleared"):
        store.write(
            TelosObject(
                id=store.mint_id("alarm"), kind="alarm", meta={"type": "binding", "target": "g_x", "state": state}
            )
        )
    live = {a.get("state") for a in store.list_alarms(open_only=True)}
    assert live == {"open", "acknowledged"}
    assert len(store.list_alarms(open_only=False)) == 3


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
