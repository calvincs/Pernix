"""TELOS speculation-pool lifecycle: how an entry leaves the pool.

The pool only ever grew — 526 files in nine days on the live box — because
nothing archived, the evaluator's dead end cycled entries back to 'soup', and
the one pass that did fire deleted the exact cohort the calibration review
needs. These tests pin the replacement: two terminal statuses, archived out
of the scan path, never deleted before their own horizon.

The gate_reason shapes below are the real ones, in the proportions the live
pool holds them (265 eig-floor / 234 inconclusive-x2 / 19 no-falsifier /
17 admitted / 11 observable-absent) — synthetic ids, real strings.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from config import settings
from core.llm.types import ChatResponse, TokenUsage
from core.telos.retire import (
    archive_untestable_pool,
    prune_soup_archive,
    prune_speculation_pool,
    terminal_gate_class,
)
from core.telos.store import TelosObject, TelosStore


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setattr(settings, "telos_enabled", True)
    return TelosStore.open()


def _pooled(store, gate_reason, status="soup", created=None, **extra):
    obj = TelosObject(
        id=store.mint_id("hypothesis"),
        kind="hypothesis",
        meta={
            "status": status,
            "statement": "A structure-mapped claim about the target domain.",
            "gate_reason": gate_reason,
            "eig": 0.4,
            **({"created_at": created} if created else {}),
            **extra,
        },
    )
    store.write(obj)
    return obj


def _ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _age_archived(store, obj_id, days):
    """Backdate an archived file's `archived_at` — what N days of cron do.
    Written by hand because nothing in the store writes into the archive."""
    import yaml

    obj = store.read_archived("hypothesis", obj_id)
    obj.meta["archived_at"] = _ago(days)
    front = yaml.safe_dump(obj.meta, sort_keys=True, allow_unicode=True, default_flow_style=False)
    obj.path.write_text(f"---\n{front}---\n\n{obj.body}\n", encoding="utf-8")


# --- classification --------------------------------------------------------


def test_terminal_classes_are_evaluability_verdicts_only():
    """Terminal means "cannot be checked, ever, by this system"."""
    assert terminal_gate_class("no falsifier")
    assert terminal_gate_class(
        "observable absent from records (1/6 terms present, missing dns, latency) "
        "— evaluation could only be inconclusive"
    )
    assert terminal_gate_class("inconclusive x2: the evidence does not contain the named ledger")


def test_eig_floor_is_not_terminal():
    """The largest class in the live pool (265 of 543) and the one this sweep
    must not touch. Expected information gain is a prior about payoff; low
    payoff is not unanswerability, and the two axes were conflated once
    already. An eig-floor entry is often perfectly testable."""
    assert terminal_gate_class("eig 0.3 below floor 0.15") is None
    assert terminal_gate_class("eig 0.5 discounted to 0.125 by calibration, below floor 0.15") is None


def test_admitted_and_cost_reasons_are_not_terminal():
    assert terminal_gate_class("admitted") is None
    assert terminal_gate_class("cost 40000 exceeds budget 20000") is None
    assert terminal_gate_class("") is None
    assert terminal_gate_class("inconclusive x1: one pass, still a retry") is None


# --- the backfill sweep ----------------------------------------------------


def test_sweep_archives_only_the_terminal_classes(store):
    """Pre-registered by the agent against its own pool: the inconclusive-x2
    entries naming nonexistent ledgers go; the testable-as-written and
    eig-floor entries stay."""
    dead = [
        _pooled(store, "inconclusive x2: the evidence names no ledger by that name"),
        _pooled(store, "inconclusive x2: gathered evidence is tool reliability statistics only"),
        _pooled(store, "no falsifier"),
        _pooled(store, "observable absent from records (0/5 terms present, missing tcp) — evaluation could only be"),
    ]
    kept = [
        _pooled(store, "eig 0.3 below floor 0.15"),
        _pooled(store, "eig 0.5 discounted to 0.125 by calibration, below floor 0.15"),
        _pooled(store, "admitted", status="gated"),
        _pooled(store, "cost 90000 exceeds budget 20000"),
    ]

    result = archive_untestable_pool(store)
    assert result["archived"] == 4
    assert sum(result["classes"].values()) == 4

    live = {h.id for h in store.list_hypotheses()}
    assert live == {h.id for h in kept}
    for h in dead:
        assert store.read("hypothesis", h.id) is None
        assert store.read_archived("hypothesis", h.id).get("status") == "untestable"
    # Trace says what left and in which classes — a silent sweep would be
    # indistinguishable from data loss.
    events = store.trace_events(days=1, types={"soup_archived"})
    assert events and events[-1]["count"] == 4


def test_sweep_reads_the_reachable_boolean_before_the_reason_string(store):
    """A mint-time `reachable: false` verdict (E7) is authoritative even when
    the reason string would not prefix-match — the sweep no longer depends on
    the probe's error-message format. `reachable: true` grants no immunity:
    the reason string still classifies (an evaluator dead-end is terminal
    regardless of what the mint probe thought)."""
    unreachable = _pooled(store, "eig 0.6 cleared, coverage 1/6", reachable=False)
    reachable_kept = _pooled(store, "eig 0.3 below floor 0.15", reachable=True)
    reachable_dead = _pooled(store, "inconclusive x2: no such ledger", reachable=True)

    result = archive_untestable_pool(store)
    assert result["archived"] == 2
    assert result["classes"]["observable absent from the records"] == 1

    assert {h.id for h in store.list_hypotheses()} == {reachable_kept.id}
    assert store.read_archived("hypothesis", unreachable.id).get("status") == "untestable"
    assert store.read_archived("hypothesis", reachable_dead.id).get("status") == "untestable"


def test_sweep_leaves_the_pool_at_the_expected_size(store):
    """Proportional replay of the live pool: 265 eig-floor + 17 admitted stay,
    the 264 terminal entries go. The agent predicted ~262 of 526 surviving a
    full backfill; the shape of that prediction is what is asserted here."""
    for _ in range(20):
        _pooled(store, "eig 0.4 discounted to 0.1 by calibration, below floor 0.15")
    for _ in range(18):
        _pooled(store, "inconclusive x2: the records do not contain the named observable")
    for _ in range(2):
        _pooled(store, "no falsifier")
    for _ in range(1):
        _pooled(store, "observable absent from records (1/7 terms present, missing dns)")
    for _ in range(2):
        _pooled(store, "admitted", status="gated")

    archive_untestable_pool(store)
    assert len(store.list_hypotheses(status="soup")) == 20  # eig-floor class intact
    assert len(store.list_hypotheses()) == 22  # plus the gated queue
    assert store.count_archived("hypothesis") == 21


def test_sweep_finishes_an_interrupted_archive(store):
    """`archive_hypothesis` writes then moves, so the only state a crash can
    leave is a terminally-stamped file still in soup/. Nothing else would
    ever look at it again — the sweep does."""
    stranded = _pooled(store, "inconclusive x2: nothing answers this", status="untestable")
    stranded.meta["archive_reason"] = "inconclusive x2: nothing answers this"
    store.write(stranded)
    assert len(store.list_hypotheses()) == 1  # still in the scan path

    assert archive_untestable_pool(store)["archived"] == 1
    assert store.list_hypotheses() == []
    moved = store.read_archived("hypothesis", stranded.id)
    assert moved.get("status") == "untestable"
    assert moved.get("archive_reason") == "inconclusive x2: nothing answers this"


def test_sweep_is_bounded_per_pass(store):
    """A sweep is maintenance, not a purge: an unbounded pass would rewrite
    the whole store in one cron run. 3/pass (the hint-retirement bound) would
    be decorative against a pool this size, so the bound is 100."""
    from core.telos.retire import _MAX_FILES_PER_PASS

    assert _MAX_FILES_PER_PASS == 100
    for _ in range(_MAX_FILES_PER_PASS + 5):
        _pooled(store, "no falsifier")

    assert archive_untestable_pool(store)["archived"] == _MAX_FILES_PER_PASS
    assert len(store.list_hypotheses(status="soup")) == 5
    assert archive_untestable_pool(store)["archived"] == 5  # the next pass finishes


# --- the age axis ----------------------------------------------------------


def test_expired_is_archived_not_deleted(store, monkeypatch):
    """The converted pruner. The pool is the calibration review's forensic
    record; the Sept 2026 review reads exactly the cohort a deleting pruner
    would have eaten first."""
    monkeypatch.setattr(settings, "telos_soup_retention_days", 30)
    old = _pooled(store, "eig 0.3 below floor 0.15", created=_ago(45))
    cases = [
        _pooled(store, "eig 0.3 below floor 0.15"),  # young pool row
        _pooled(store, "admitted", status="gated", created=_ago(45)),  # queued work
        _pooled(store, "admitted", status="supported", created=_ago(45)),  # the record
    ]

    assert prune_speculation_pool(store)["archived"] == 1
    assert {h.get("status") for h in store.list_hypotheses()} == {"soup", "gated", "supported"}
    assert {h.id for h in store.list_hypotheses()} == {h.id for h in cases}

    archived = store.read_archived("hypothesis", old.id)
    assert archived is not None, "aged out, but still on disk"
    assert archived.get("status") == "expired"
    # The reason it was pooled survives the reason it aged out.
    assert archived.get("gate_reason") == "eig 0.3 below floor 0.15"


def test_expired_and_untestable_are_different_failure_classes(store, monkeypatch):
    """'untestable' = examined and unresolvable. 'expired' = never examined.
    The review reads them differently, so the statuses stay distinct."""
    monkeypatch.setattr(settings, "telos_soup_retention_days", 30)
    examined = _pooled(store, "inconclusive x2: nothing in the records answers this", created=_ago(45))
    never = _pooled(store, "eig 0.2 below floor 0.15", created=_ago(45))

    archive_untestable_pool(store)
    prune_speculation_pool(store)

    assert store.read_archived("hypothesis", examined.id).get("status") == "untestable"
    assert store.read_archived("hypothesis", never.id).get("status") == "expired"


def test_soup_prune_disabled_by_zero_retention(store, monkeypatch):
    monkeypatch.setattr(settings, "telos_soup_retention_days", 0)
    _pooled(store, "eig 0.3 below floor 0.15", created=_ago(900))
    assert prune_speculation_pool(store)["archived"] == 0
    assert len(store.list_hypotheses()) == 1


# --- the archive's own horizon ---------------------------------------------


def test_archive_hard_delete_respects_its_horizon(store, monkeypatch):
    monkeypatch.setattr(settings, "telos_soup_archive_retention_days", 180)
    stale = _pooled(store, "no falsifier")
    fresh = _pooled(store, "no falsifier")
    store.archive_hypothesis(stale, "untestable", "old")
    store.archive_hypothesis(fresh, "untestable", "recent")
    _age_archived(store, stale.id, 200)

    assert prune_soup_archive(store)["deleted"] == 1
    assert store.read_archived("hypothesis", stale.id) is None
    assert store.read_archived("hypothesis", fresh.id) is not None


def test_archive_kept_forever_at_zero(store, monkeypatch):
    """0 = keep forever, and that is the setting the calibration review needs
    if it slips past the horizon."""
    monkeypatch.setattr(settings, "telos_soup_archive_retention_days", 0)
    h = _pooled(store, "no falsifier")
    store.archive_hypothesis(h, "untestable", "old")
    _age_archived(store, h.id, 5000)
    assert prune_soup_archive(store)["deleted"] == 0
    assert store.count_archived("hypothesis") == 1


# --- the evaluator's dead end ----------------------------------------------


def _verdict(payload) -> ChatResponse:
    return ChatResponse(
        content=json.dumps(payload),
        tool_calls=None,
        usage=TokenUsage(total_tokens=200),
        model="test",
        provider="fake",
        finish_reason="stop",
    )


async def test_evaluator_dead_end_archives_instead_of_repooling(store, mock_llm_client):
    """The cycle this replaces: inconclusive x2 set status back to 'soup', so
    the entry rejoined the pool every scan re-reads, forever."""
    from core.telos.evaluate import _MAX_ATTEMPTS, evaluate_one

    assert _MAX_ATTEMPTS == 2, "raising this only buys more spend on the same records"
    q = store.add_question("Why does the fetch path fail on this host class?")
    h = _pooled(
        store,
        "admitted",
        status="gated",
        question=q.id,
        attempts=0,
        falsifier={"observable": "median TCP connect time", "rule": "reject if under 40ms"},
    )
    mock_llm_client.responses = [
        _verdict({"verdict": "inconclusive", "confidence": 0.3, "note": "no such observable in the records"}),
        _verdict({"verdict": "inconclusive", "confidence": 0.3, "note": "no such observable in the records"}),
    ]

    assert await evaluate_one(store, [h], lambda: False) == "inconclusive"
    mid = store.read("hypothesis", h.id)
    assert mid.get("status") == "gated" and mid.get("attempts") == 1  # one retry, still live

    assert await evaluate_one(store, [mid], lambda: False) == "inconclusive"
    assert store.read("hypothesis", h.id) is None  # gone from every scan
    archived = store.read_archived("hypothesis", h.id)
    assert archived.get("status") == "untestable"
    assert archived.get("gate_reason") == "inconclusive x2: no such observable in the records"
    # Calibration scores this event as the realized-zero outcome; the type
    # must not drift when the destination does.
    pooled = store.trace_events(days=1, types={"hypothesis_pooled"})
    assert pooled and pooled[-1]["archived"] == "untestable"


# --- what the read surfaces say --------------------------------------------


async def test_hypotheses_endpoint_points_at_the_archive(store):
    """A status filter for a terminal status can only ever return nothing.
    Saying where the files went beats implying none were ever produced."""
    from api.routers.telos import telos_hypotheses

    h = _pooled(store, "no falsifier")
    store.archive_hypothesis(h, "untestable", "dead end")

    out = await telos_hypotheses(status="untestable")
    assert out["hypotheses"] == []
    assert "soup/archive/" in out["note"]
    assert "note" not in await telos_hypotheses(status="soup")


def test_status_summary_counts_the_archive_separately(store):
    """The pool count must read as a live queue; the archived total is
    reported beside it so hundreds of entries do not simply vanish."""
    from core.extensions.telos import telos_status

    _pooled(store, "eig 0.3 below floor 0.15")
    h = _pooled(store, "no falsifier")
    store.archive_hypothesis(h, "untestable", "dead end")

    line = next(ln for ln in telos_status().splitlines() if ln.startswith("Hypotheses:"))
    assert "1 in the speculation pool" in line
    assert "1 archived" in line


def test_calibration_still_scores_an_archived_dead_end(store):
    """The eig fallback reads hypothesis objects by id when the generation
    event has aged out of the window. Archived entries are exactly the
    realized-zero half of the sample, so the fallback must find them —
    otherwise the discount is computed against a resolve rate of 1.0."""
    from core.telos.calibration import eig_calibration

    h = _pooled(store, "inconclusive x2: nothing answers this", eig=0.6)
    store.archive_hypothesis(h, "untestable", "dead end")
    store.trace_append("hypothesis_pooled", {"id": h.id})

    metric = eig_calibration(store)
    assert metric["n"] == 1 and metric["mean_eig"] == 0.6 and metric["resolve_rate"] == 0.0
