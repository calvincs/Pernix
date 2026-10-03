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
from db import models as db


@pytest.fixture(autouse=True)
def _adaptive_on(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "adaptive_enabled", True)
    monkeypatch.setattr(settings, "adaptive_auto_apply", True)
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
    # v42: adaptive.cap_reached is a log-tier category, so it lives in the activity log, not the bell.
    before = len(db.list_notifications("log"))

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
    notes = db.list_notifications("log")
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
    before = len(db.list_notifications("log"))
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
    assert len(db.list_notifications("log")) == before
