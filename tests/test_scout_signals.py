"""Tool/skill performance counter DB tests (scout_signals table)."""

from core.signals import Signal, from_row
from db import models as db


def test_upsert_signal_inserts_then_increments():
    db.delete_signal("skill", "test-upsert")
    db.upsert_signal("skill", "test-upsert", delta_successes=1)
    row = db.get_signal("skill", "test-upsert")
    assert row["reinforcements"] == 1
    assert row["successes"] == 1
    assert row["failures"] == 0
    assert row["first_seen_at"] == row["last_reinforced_at"]

    db.upsert_signal("skill", "test-upsert", delta_failures=2)
    row = db.get_signal("skill", "test-upsert")
    assert row["reinforcements"] == 2
    assert row["successes"] == 1
    assert row["failures"] == 2


def test_upsert_preserves_first_seen_updates_last_reinforced():
    db.delete_signal("tool", "test-first-seen")
    db.upsert_signal("tool", "test-first-seen", delta_successes=1)
    first = db.get_signal("tool", "test-first-seen")
    db.upsert_signal("tool", "test-first-seen", delta_successes=1)
    second = db.get_signal("tool", "test-first-seen")
    assert second["first_seen_at"] == first["first_seen_at"]
    assert second["last_reinforced_at"] >= first["last_reinforced_at"]


def test_get_signals_by_subjects_hits_natural_key():
    db.delete_signal("skill", "q-a")
    db.delete_signal("tool", "q-b")
    db.delete_signal("skill", "q-unrelated")
    db.upsert_signal("skill", "q-a", delta_successes=1)
    db.upsert_signal("tool", "q-b", delta_failures=1)
    db.upsert_signal("skill", "q-unrelated", delta_successes=5)

    rows = db.get_signals_by_subjects([("skill", "q-a"), ("tool", "q-b")])
    subs = {(r["signal_type"], r["subject"]) for r in rows}
    assert subs == {("skill", "q-a"), ("tool", "q-b")}


def test_get_signals_by_subjects_empty_input():
    assert db.get_signals_by_subjects([]) == []


def test_get_top_signals_excludes_execution_mode():
    db.delete_signal("execution_mode", "inline")
    db.upsert_signal("execution_mode", "inline", delta_successes=1)
    db.delete_signal("skill", "top-skill")
    db.upsert_signal("skill", "top-skill", delta_successes=3)

    rows = db.get_top_signals(limit=100)
    types = {r["signal_type"] for r in rows}
    assert "execution_mode" not in types
    assert "skill" in types


def test_from_row_deserializes():
    db.delete_signal("tool", "deser-tool")
    db.upsert_signal("tool", "deser-tool", delta_successes=5, delta_failures=1)
    row = db.get_signal("tool", "deser-tool")
    sig = from_row(row)
    assert isinstance(sig, Signal)
    assert sig.signal_type == "tool"
    assert sig.subject == "deser-tool"
    assert sig.successes == 5
    assert sig.failures == 1
    assert sig.reinforcements == 1


def test_is_poor_performer():
    sig = Signal(signal_type="tool", subject="x", reinforcements=5, successes=3, failures=2)
    # 2/5 = 40% >= 20% threshold
    assert sig.is_poor_performer is True

    sig2 = Signal(signal_type="tool", subject="y", reinforcements=10, successes=9, failures=1)
    # 1/10 = 10% < 20% threshold
    assert sig2.is_poor_performer is False


def test_to_display():
    sig = Signal(signal_type="skill", subject="z", reinforcements=11, successes=10, failures=1)
    d = sig.to_display()
    assert d["uses"] == 11
    assert d["failures"] == 1
    assert d["is_poor_performer"] is False  # 1/11 ≈ 9%
