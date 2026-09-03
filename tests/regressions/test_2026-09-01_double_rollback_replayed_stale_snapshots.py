"""`rollback(batch_id)` had no status guard, and POST /api/adaptive/rollback
exposes it verbatim.

Rolling back an already rolled-back batch walked the same journal events a
second time and replayed their apply-time snapshots over whatever had landed
since: a later batch's create of the same entry id was hard-deleted (the
original create's before_json is absent), a later update was overwritten by
the stale before_json. The fix refuses any batch whose status is not
'applied' or 'suspect' — the two states a batch can actually be in when it
still has live effects to reverse.
"""

import pytest

from core.adaptive.engine import AdaptiveError, apply_batch, queue_edits, rollback
from db import models as db


@pytest.fixture(autouse=True)
def _adaptive_on(monkeypatch, tmp_path):
    monkeypatch.setattr("config.settings.adaptive_enabled", True)
    monkeypatch.setattr("config.settings.adaptive_auto_apply", True)
    import core.adaptive.render as render

    monkeypatch.setattr(render, "MIRROR_PATH", tmp_path / "ADAPTIVE.md")


def _create(title, content):
    edit = {"action": "create", "kind": "routing_hint", "title": title, "content": content, "evidence": ["pm:x"]}
    batch_id = queue_edits([edit], "refine")["batch_id"]
    assert apply_batch(batch_id, actor="user")["applied"] == [title]
    return batch_id


def test_second_rollback_refuses_and_leaves_the_later_entry_alone():
    first = _create("shared-id", "first text")
    rollback(batch_id=first)
    assert db.adaptive_get_entry("shared-id") is None
    assert db.adaptive_get_batch(first)["status"] == "rolled_back"

    # A later batch re-mints the same id with fresh content.
    later = _create("shared-id", "second text")
    assert db.adaptive_get_entry("shared-id")["content"] == "second text"

    with pytest.raises(AdaptiveError, match="rolled_back"):
        rollback(batch_id=first)

    # The later entry survives, and the first batch's journal grew no new
    # rollback events (the refusal happened before any event was reversed).
    assert db.adaptive_get_entry("shared-id")["content"] == "second text"
    assert [e["action"] for e in db.adaptive_events_for_batch(first)].count("rollback") == 1
    assert db.adaptive_get_batch(later)["status"] == "applied"


def test_unknown_and_pending_batches_are_refused():
    with pytest.raises(AdaptiveError, match="unknown batch"):
        rollback(batch_id="ab-nope")
    edit = {"action": "create", "kind": "routing_hint", "title": "queued", "content": "x", "evidence": ["pm:x"]}
    pending = queue_edits([edit], "refine")["batch_id"]
    with pytest.raises(AdaptiveError, match="pending"):
        rollback(batch_id=pending)


def test_suspect_batches_still_roll_back():
    """The tripwire flags a batch 'suspect' before it (or a human) rolls it
    back — that state must stay rollback-able."""
    bid = _create("flagged", "text")
    db.adaptive_update_batch(bid, status="suspect", flagged_reason="canary regression: t1")
    assert rollback(batch_id=bid)["reversed_events"]
    assert db.adaptive_get_entry("flagged") is None
    assert db.adaptive_get_batch(bid)["status"] == "rolled_back"
