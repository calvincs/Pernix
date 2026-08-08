"""Regression: stale pruning could never reach the genuinely old entries.

Shipped defect (architecture review 2026-08-07, §4): `_stale_candidates`
iterated the 30/60/90/180/360-day cohorts in insertion order, accumulated
every qualifying entry into one flat list, then truncated it to `limit=10`.
The 30-day cohort is by far the most populous, so it filled all ten slots and
the 180/360-day cohorts — the entries most likely to actually be stale — were
structurally unreachable on any store with more than ~10 under-average young
entries. Forgetting was aimed at the wrong end of the age distribution.

Fix: qualifying entries are bucketed per cohort and slots are dealt
round-robin, oldest cohort first, so every cohort with candidates is
represented before any cohort gets a second slot.
"""

from __future__ import annotations

from core.memory.sweeps import _stale_candidates

_NOW = 1_800_000_000


def _row(days_old: int, idx: int, hits: int = 0, weight: str = "normal") -> dict:
    return {
        "file_name": f"pernix.f{days_old}",
        "epoch": str(_NOW - int(days_old * 86400)),
        "weight": weight,
        "content": f"Entry {idx} from the {days_old}-day cohort.",
        "hit_count": hits,
    }


def _populous_young_store() -> list[dict]:
    """40 under-average 30-day entries, 4 at 180 days, 4 at 360 days."""
    rows: list[dict] = []
    # Mixed hit counts so an average exists and most entries fall below it.
    for i in range(40):
        rows.append(_row(35, i, hits=0 if i < 36 else 20))
    for i in range(4):
        rows.append(_row(200, i, hits=0 if i < 3 else 9))
    for i in range(4):
        rows.append(_row(400, i, hits=0 if i < 3 else 9))
    return rows


def test_old_cohorts_are_reachable_despite_a_populous_young_cohort():
    candidates = _stale_candidates(_populous_young_store(), _NOW, limit=10)

    assert len(candidates) == 10
    cohorts = {c["cohort"] for c in candidates}
    assert "360d" in cohorts, "the oldest cohort must get slots"
    assert "180d" in cohorts
    assert "30d" in cohorts, "young cohort is still represented, just not exclusively"


def test_oldest_cohort_is_dealt_first():
    candidates = _stale_candidates(_populous_young_store(), _NOW, limit=3)

    # One slot each, oldest first, before any cohort takes a second.
    assert [c["cohort"] for c in candidates] == ["360d", "180d", "30d"]


def test_every_qualifying_entry_returned_when_under_the_limit():
    rows = [_row(35, i, hits=0 if i < 3 else 5) for i in range(4)]
    candidates = _stale_candidates(rows, _NOW, limit=10)
    assert len(candidates) == 3
    assert all(c["cohort"] == "30d" for c in candidates)


def test_thin_cohorts_and_high_weight_still_excluded():
    rows = [_row(35, i) for i in range(2)]  # < 3 entries: no meaningful average
    rows += [_row(400, i, hits=0, weight="high") for i in range(4)]
    assert _stale_candidates(rows, _NOW, limit=10) == []


def test_entries_younger_than_the_smallest_cohort_are_never_candidates():
    rows = [_row(5, i) for i in range(20)]
    assert _stale_candidates(rows, _NOW, limit=10) == []
