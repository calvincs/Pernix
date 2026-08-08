"""Regression: auto-retirement removed exactly the canaries the tripwire
divides by.

Shipped defect (2026-08-07 introspective-stack review, §1/§6):
`maintain._maintain_one` retired a canary after
`canary_retire_after_passes` (25) consecutive green runs, on the reasoning
that a long-green canary carries no information. That is true of a test
suite under active development and false of a regression tripwire: the
adaptive tripwire's baseline pass rate is computed from SCHEDULED runs of
the tasks in the post-batch sweep (`core/adaptive/tripwire.py`), so
systematically deleting the stable canaries shrank the denominator of the
only signal allowed to auto-roll-back an applied batch. Its entire value is
that green stays green.

The fix: long-green canaries are demoted to a reduced scheduled cadence and
stay in the pool. Post-batch and manual sweeps ignore cadence entirely.

Kept as a regression pin because "retire what never fails" is an intuitive
optimisation someone will propose again.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.canary.maintain import run_maintenance
from core.canary.parser import MAX_CADENCE, load_canary, scan_canaries
from core.canary.propose import materialize_canary
from core.canary.runner import _due_this_sweep
from db import models as db

_SPEC = {
    "name": "pin",
    "prompt": "Create out.txt containing DONE.",
    "gates": [{"name": "out", "command": "grep -qx DONE out.txt", "watch_paths": []}],
    "rationale": "test canary",
}


@pytest.fixture(autouse=True)
def _canaries_tmp(monkeypatch, tmp_path):
    monkeypatch.setattr("config.settings.canaries_dir", str(tmp_path / "canaries"))
    monkeypatch.setattr("config.settings.canary_enabled", True)
    monkeypatch.setattr("config.settings.canary_auto_maintain", True)
    monkeypatch.setattr("config.settings.canary_retire_after_passes", 5)


def _base() -> Path:
    from config import settings

    return Path(settings.canaries_dir)


def _mk(name: str) -> None:
    got, err = materialize_canary(dict(_SPEC, name=name), vetting=False)
    assert got == name, err


def test_long_green_canary_stays_in_the_scheduled_pool():
    _mk("pin")
    for _ in range(5):
        db.add_canary_run(task="pin", trigger="scheduled", session_id=None, gate_results_json="[]", passed=True)
    run_maintenance()
    # The old behaviour deleted it from the suite entirely.
    assert [c.name for c in scan_canaries(_base())] == ["pin"]
    assert load_canary("pin", base=_base()).cadence > 1


def test_demoted_canary_still_runs_on_its_cadence():
    """Reduced, not removed: over one cadence period it contributes a
    scheduled run, which is what the tripwire baseline reads."""
    _mk("pin")
    for _ in range(5):
        db.add_canary_run(task="pin", trigger="scheduled", session_id=None, gate_results_json="[]", passed=True)
    run_maintenance()
    c = load_canary("pin", base=_base())
    hits = [i for i in range(c.cadence * 3) if _due_this_sweep(c, i)]
    assert len(hits) == 3


def test_cadence_one_always_runs():
    _mk("pin")
    c = load_canary("pin", base=_base())
    assert c.cadence == 1
    assert all(_due_this_sweep(c, i) for i in range(10))


def test_demotion_is_bounded():
    """A canary must never back off so far it stops feeding the baseline."""
    _mk("pin")
    md = _base() / "pin" / "CANARY.md"
    md.write_text(md.read_text().replace("flaky: false", f"flaky: false\ncadence: {MAX_CADENCE}", 1))
    for _ in range(5):
        db.add_canary_run(task="pin", trigger="scheduled", session_id=None, gate_results_json="[]", passed=True)
    assert run_maintenance()["demoted"] == []
    assert load_canary("pin", base=_base()).cadence == MAX_CADENCE


def test_goodhart_lock_still_wins_over_demotion():
    """The one invariant demotion must not disturb: a canary whose LATEST run
    failed is untouchable by every mutation."""
    _mk("pin")
    for _ in range(5):
        db.add_canary_run(task="pin", trigger="scheduled", session_id=None, gate_results_json="[]", passed=True)
    db.add_canary_run(task="pin", trigger="scheduled", session_id=None, gate_results_json="[]", passed=False)
    stats = run_maintenance()
    assert all(not v for v in stats.values())
    assert load_canary("pin", base=_base()).cadence == 1
