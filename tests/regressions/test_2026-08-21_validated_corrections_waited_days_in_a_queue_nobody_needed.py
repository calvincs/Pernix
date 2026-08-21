"""Regression — 2026-08-21, box.

Memory corrections are additive (the disputed entries stay; recall surfaces
the correction beside them), so the veto window never protected anything —
yet 280 dream hypotheses queued behind a 12-row per-producer share and a
10-a-day drain. Calvin: "bypass this". Pinned here: a validated correction
applies on promotion regardless of the queue caps, is resolved
`auto_applied` with its own provenance label, is narrated to the dream
journal, and produces at most one operator notice per day.
"""

import json

import pytest

from db import models as db


@pytest.fixture(autouse=True)
def _adaptive_on(monkeypatch, tmp_path):
    monkeypatch.setattr("config.settings.adaptive_enabled", True)
    monkeypatch.setattr("config.settings.adaptive_auto_apply", True)
    import core.adaptive.render as render

    monkeypatch.setattr(render, "MIRROR_PATH", tmp_path / "ADAPTIVE.md")


def _validated(statement: str, file: str = "test.corrections") -> str:
    evidence = json.dumps([{"type": "memory", "file": file, "epoch": 1}])
    hid = db.add_dream_hypothesis("memory_stale", statement, evidence)
    db.update_dream_hypothesis(hid, status="validated")
    return hid


async def test_corrections_apply_on_promotion_even_with_the_queue_full(monkeypatch):
    from core.dream.promote import promote_validated

    written, journal = [], []
    import core.memory.ingest as ingest_mod

    monkeypatch.setattr(
        ingest_mod,
        "apply_memory_correction",
        lambda files, statement, source_ref="", kind="", approved_by="human": (
            written.append(approved_by),
            list(files),
        )[1],
    )
    monkeypatch.setattr("core.dream.journal.append_sync", lambda text: journal.append(text))
    # A queue that would refuse dream under the old rules.
    monkeypatch.setattr("config.settings.adaptive_max_pending_proposals", 1)
    monkeypatch.setattr("config.settings.adaptive_max_pending_per_producer", 1)
    db.adaptive_add_proposal("dream", json.dumps([{"x": 1}]), "[]", "already pending")

    _validated("M1 is stale: the job was renamed")
    _validated("M4 contradicts M2 about the output path", file="test.paths")
    assert await promote_validated(limit=10) == 2

    rows = db.adaptive_list_proposals(status="auto_applied")
    assert len(rows) == 2 and written == ["dream", "dream"]
    assert db.adaptive_count_pending_proposals() == 1  # the unrelated pending row is untouched
    assert len(journal) == 2 and all("memory correction applied on validation" in j for j in journal)

    notes = db.get_notifications()
    assert len(notes) == 1
    assert notes[0]["title"] == "Dream: 2 memory correction(s) applied"
    assert "no veto window" in notes[0]["body"] and "test.paths" in notes[0]["body"]

    # Same day, another pass: journal yes, second notification no.
    _validated("M9 is stale too", file="test.more")
    assert await promote_validated(limit=10) == 1
    assert len(journal) == 3 and len(db.get_notifications()) == 1


def test_auto_applied_is_a_documented_status_with_its_own_provenance(monkeypatch):
    from api.routers.adaptive import PROPOSAL_STATUSES
    from core.memory.ingest import correction_preamble

    assert "auto_applied" in PROPOSAL_STATUSES
    assert correction_preamble("memory_stale", "dream", "dream:abc") == (
        "STALE-INFO CORRECTION (auto-applied on validation — dream finding, no veto window; adaptive review, dream:abc)"
    )


def test_raised_defaults_are_the_new_floor():
    """Calvin, 2026-08-21: 'I am unsure why we have such low permitted levels.'"""
    from config import Settings

    d = Settings()
    assert d.adaptive_max_pending_proposals >= 200
    assert d.adaptive_max_pending_per_producer >= 60
    assert d.adaptive_max_auto_approvals_per_day >= 40
    assert d.adaptive_max_auto_applies_per_day >= 24
    assert d.adaptive_max_entries_per_kind >= 24
    assert d.dream_max_pending >= 200
    assert d.dream_hypotheses_per_cycle >= 6
