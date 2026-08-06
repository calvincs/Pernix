"""Pernix — Canary growth (§12.2): proposal generation + staleness nudge.

Nothing writes data/canaries/ without human approval; approving a proposal
materializes a validated CANARY.md and queues a vetting run; a stale
last_reviewed nudges exactly once per (name, date).
"""

import json
from pathlib import Path

import pytest

from core.canary.propose import materialize_canary, queue_canary_proposals
from db import models as db

_SPEC = {
    "name": "regression-pin",
    "prompt": "Reproduce the fix: create out.txt containing DONE.",
    "gates": [{"name": "out", "command": "grep -qx DONE out.txt", "watch_paths": []}],
    "files": {"seed.txt": "fixture"},
    "rationale": "session X kept mangling file writes",
}


@pytest.fixture(autouse=True)
def _canaries_tmp(monkeypatch, tmp_path):
    monkeypatch.setattr("config.settings.canaries_dir", str(tmp_path / "canaries"))
    monkeypatch.setattr("config.settings.canary_enabled", True)
    monkeypatch.setattr("config.settings.adaptive_enabled", True)
    import core.adaptive.render as render

    monkeypatch.setattr(render, "MIRROR_PATH", tmp_path / "ADAPTIVE.md")


# ---------------------------------------------------------------------------
# Refine contract
# ---------------------------------------------------------------------------


def test_refine_parse_carries_canary_proposals():
    from core.refine import _parse_refine_output

    raw = json.dumps({"proposals": [], "lessons": [], "canary_proposals": [_SPEC]})
    _, _, _, canaries, _ = _parse_refine_output(raw)
    assert canaries and canaries[0]["name"] == "regression-pin"


def test_queue_validates_and_stores():
    assert queue_canary_proposals([_SPEC], "refine", session_id="sess-1") == 1
    bad = dict(_SPEC, name="Not Valid Name!")
    assert queue_canary_proposals([bad], "refine") == 0
    no_gates = dict(_SPEC, gates=[])
    assert queue_canary_proposals([no_gates], "refine") == 0
    traversal = dict(_SPEC, files={"../evil": "x"})
    assert queue_canary_proposals([traversal], "refine") == 0

    props = db.adaptive_list_proposals(status="pending")
    assert len(props) == 1
    assert props[0]["producer"] == "canary_propose"
    assert "session:sess-1" in props[0]["evidence_json"]


# ---------------------------------------------------------------------------
# Approve = materialize + vetting run
# ---------------------------------------------------------------------------


def test_approve_materializes_and_enqueues_vetting(monkeypatch):
    from config import settings
    from core.adaptive import approve_proposal
    from core.canary.parser import load_canary

    vetted = []
    monkeypatch.setattr(
        "core.extensions.scheduling.enqueue_manual_canary",
        lambda name: vetted.append(name) or True,
    )
    queue_canary_proposals([_SPEC], "refine", session_id="s")
    pid = db.adaptive_list_proposals(status="pending")[0]["id"]
    result = approve_proposal(pid)
    assert result["canary_written"] == "regression-pin"
    assert vetted == ["regression-pin"]
    assert db.adaptive_get_proposal(pid)["status"] == "approved"

    # The written file round-trips through the real parser.
    c = load_canary("regression-pin", base=Path(settings.canaries_dir))
    assert c is not None
    assert c.prompt.startswith("Reproduce the fix")
    assert c.gates[0]["command"] == "grep -qx DONE out.txt"
    assert c.files == {"seed.txt": "fixture"}
    assert c.flaky is False and c.last_reviewed  # stamped today


def test_materialize_refuses_duplicates_and_invalid(tmp_path):
    base = tmp_path / "c"
    name, err = materialize_canary(_SPEC, base=base)
    assert name == "regression-pin" and err == ""
    name2, err2 = materialize_canary(_SPEC, base=base)
    assert name2 is None and "already exists" in err2
    name3, err3 = materialize_canary(dict(_SPEC, name="valid-name", prompt=""), base=base)
    assert name3 is None and "prompt" in err3


def test_dict_payload_never_hits_apply_engine(monkeypatch):
    """A canary proposal must not be interpreted as an edit batch."""
    from core.adaptive import AdaptiveError, approve_proposal

    monkeypatch.setattr("core.extensions.scheduling.enqueue_manual_canary", lambda n: True)
    pid = db.adaptive_add_proposal("canary_propose", json.dumps({"canary": dict(_SPEC, prompt="")}), "[]", "r")
    with pytest.raises(AdaptiveError, match="materialization failed|prompt"):
        approve_proposal(pid)
    # Failed materialization leaves the proposal pending for correction.
    assert db.adaptive_get_proposal(pid)["status"] == "pending"
    assert db.adaptive_list_entries(status=None) == []


# ---------------------------------------------------------------------------
# Staleness nudge
# ---------------------------------------------------------------------------


async def test_staleness_nudge_once_per_review_date(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from core.snooze import SnoozeRunner

    stale = SimpleNamespace(name="old-canary", last_reviewed="2025-01-01", flaky=False)
    fresh = SimpleNamespace(name="new-canary", last_reviewed="2099-01-01", flaky=False)
    monkeypatch.setattr("core.canary.scan_canaries", lambda *a, **k: [stale, fresh])
    monkeypatch.setattr("db.models.list_sessions", lambda limit=500: [])

    runner = SnoozeRunner.__new__(SnoozeRunner)
    runner._stats = {}
    await SnoozeRunner._cleanup_canary_runs(runner)
    notes = [n for n in db.get_notifications() if "stale" in (n.get("title") or "")]
    assert len(notes) == 1 and "old-canary" in notes[0]["title"]

    # Second sweep: watermarked, no duplicate.
    await SnoozeRunner._cleanup_canary_runs(runner)
    notes = [n for n in db.get_notifications() if "stale" in (n.get("title") or "")]
    assert len(notes) == 1

    # Human bumps the date past 90d ago -> re-arms.
    stale.last_reviewed = "2025-06-01"
    await SnoozeRunner._cleanup_canary_runs(runner)
    notes = [n for n in db.get_notifications() if "stale" in (n.get("title") or "")]
    assert len(notes) == 2
