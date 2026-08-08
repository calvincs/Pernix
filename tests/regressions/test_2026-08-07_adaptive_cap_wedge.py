"""Regression: a full adaptive entry cap was indistinguishable from a
producer with nothing to say, and only Candor ever released a slot.

Shipped defect (2026-08-07 introspective-stack review, §5.6): with
`adaptive_max_entries_per_kind = 12`, the only producer that retired its own
entries was Candor (`core/snooze.py`). Dream and Telos minted and never
retired, so once `routing_hint` filled, every further insight was rejected in
`_apply_one` with a per-edit error string that was *logged, not notified*.
The observable behaviour of "the shelf is full and everything is being
discarded" was byte-identical to "the loop had nothing to report".

Two fixes, pinned here:
  - the cap rejection raises an operator notification;
  - Dream and Telos have the retirement pass Candor always had.

Kept as a regression pin because the failure mode is *silence*: nothing goes
red, no test fails, the loop just stops producing.
"""

from __future__ import annotations

import json

import pytest

from config import settings
from core.adaptive.engine import CAP_REJECTION_MARKER, apply_batch, queue_edits
from core.telos.store import TelosObject, TelosStore
from db import models as db


@pytest.fixture(autouse=True)
def _adaptive_on(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "adaptive_enabled", True)
    monkeypatch.setattr(settings, "adaptive_auto_apply", True)
    monkeypatch.setattr(settings, "telos_enabled", True)
    import core.adaptive.render as render

    monkeypatch.setattr(render, "MIRROR_PATH", tmp_path / "ADAPTIVE.md")


def _fill_routing_hints(n: int, source: str = "refine") -> None:
    for i in range(n):
        db.adaptive_put_entry(
            {
                "id": f"filler-{i}",
                "kind": "routing_hint",
                "scope": "global",
                "title": f"filler {i}",
                "content": "x",
                "risk": "low",
                "version": 1,
                "status": "active",
                "source": source,
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
            }
        )


# ---------------------------------------------------------------------------
# The cap must be visible
# ---------------------------------------------------------------------------


def test_cap_rejection_notifies_rather_than_only_logging(monkeypatch):
    monkeypatch.setattr(settings, "adaptive_max_entries_per_kind", 3)
    _fill_routing_hints(3)
    before = len(db.get_notifications())

    result = queue_edits(
        [
            {
                "action": "create",
                "kind": "routing_hint",
                "title": "telos insight",
                "content": "something learned",
                "evidence": ["c_0001"],
            }
        ],
        "telos",
    )
    applied = apply_batch(result["batch_id"])

    assert applied["applied"] == []
    assert CAP_REJECTION_MARKER in applied["rejected"][0]["reason"]
    notes = db.get_notifications()
    assert len(notes) == before + 1
    assert "cap" in notes[0]["title"].lower()
    assert "routing_hint" in notes[0]["body"]


def test_a_normal_rejection_does_not_notify(monkeypatch):
    """Only the cap means "this producer is now inert". A version conflict
    is routine and must stay a log line, or the notification is noise."""
    monkeypatch.setattr(settings, "adaptive_max_entries_per_kind", 50)
    db.adaptive_put_entry(
        {
            "id": "moved",
            "kind": "routing_hint",
            "scope": "global",
            "title": "moved",
            "content": "x",
            "risk": "low",
            "version": 7,
            "status": "active",
            "source": "telos",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
    )
    before = len(db.get_notifications())
    result = queue_edits(
        [
            {
                "action": "update",
                "kind": "routing_hint",
                "entry_id": "moved",
                "title": "moved",
                "content": "y",
                "baseline_version": 1,
                "evidence": ["c_0001"],
            }
        ],
        "telos",
    )
    applied = apply_batch(result["batch_id"])
    assert applied["applied"] == []
    assert len(db.get_notifications()) == before


# ---------------------------------------------------------------------------
# Telos must release its own slots
# ---------------------------------------------------------------------------


def _telos_hint(entry_id: str, evidence: list[str], created_at: str) -> None:
    db.adaptive_put_entry(
        {
            "id": entry_id,
            "kind": "routing_hint",
            "scope": "global",
            "title": entry_id,
            "content": "x",
            "risk": "low",
            "version": 1,
            "status": "active",
            "source": "telos",
            "created_at": created_at,
            "updated_at": created_at,
        }
    )
    db.adaptive_add_event(
        entry_id=entry_id,
        action="create",
        before_json=None,
        after_json="{}",
        evidence_json=json.dumps(evidence),
        actor="auto",
        batch_id="ab-seed",
        proposal_id=None,
    )


def _hypothesis(store: TelosStore, hid: str, status: str) -> None:
    store.write(TelosObject(id=hid, kind="hypothesis", meta={"status": status, "band": "near", "question": "q_1"}))


def test_telos_retires_a_hint_whose_hypothesis_lost_support():
    from core.telos.retire import retire_stale_hints

    store = TelosStore.open()
    store.ensure_root()
    _hypothesis(store, "h_0001", "supported")
    _hypothesis(store, "h_0002", "soup")  # returned to the speculation pool
    _telos_hint("telos-still-good", ["c_0001", "h_0001", "q_1"], "2026-08-07T00:00:00+00:00")
    _telos_hint("telos-withdrawn", ["c_0002", "h_0002", "q_1"], "2026-08-07T00:00:00+00:00")

    result = retire_stale_hints(store)
    assert result["retired"] == 1
    assert [r["entry_id"] for r in result["reasons"]] == ["telos-withdrawn"]


def test_telos_retires_past_the_ttl_so_the_cap_cannot_wedge():
    """The honest criterion: a telos verdict is terminal by construction, so
    without a TTL the retirement pass would be as decorative as no pass."""
    from core.telos.retire import retire_stale_hints

    store = TelosStore.open()
    store.ensure_root()
    _hypothesis(store, "h_0001", "supported")
    _telos_hint("telos-ancient", ["c_0001", "h_0001", "q_1"], "2020-01-01T00:00:00+00:00")

    result = retire_stale_hints(store)
    assert result["retired"] == 1
    assert "TTL" in result["reasons"][0]["reason"]


def test_telos_retirement_leaves_other_producers_alone():
    from core.telos.retire import retire_stale_hints

    store = TelosStore.open()
    store.ensure_root()
    _fill_routing_hints(2, source="candor")
    assert retire_stale_hints(store)["retired"] == 0
