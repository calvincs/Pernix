"""Regression: auto-retirement removed exactly the canaries the tripwire
divides by.

Shipped defect (2026-08-07 introspective-stack review, §1/§6):
`maintain._maintain_one` retired a canary after 25 consecutive green runs,
on the reasoning that a long-green canary carries no information. That is
true of a test suite under active development and false of a regression
tripwire, whose entire value is that green stays green.

The invariant has survived two mechanisms since. First fix: cadence
demotion (run every Nth scheduled sweep), protecting the AGGREGATE baseline
denominator the tripwire divided by at the time. The v3.1 redesign replaced
both: the tripwire became per-task (a canary testifies against a batch only
when its trailing runs were all green), and long-green canaries are PARKED
— off the nightly heartbeat, still in the suite. The successor invariant
this file pins:

  A long-green canary is parked, never removed. It stays in
  `scan_canaries`, still runs on full sweeps, coverage triggers and manual
  runs, and a red run revokes the parking. Removing it would erase the
  green history that is now the tripwire's per-task precondition — the same
  starvation as 2026-08-07, one layer down.

Kept as a regression pin because "retire what never fails" is an intuitive
optimisation someone will propose again.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.canary.maintain import run_maintenance
from core.canary.parser import load_canary, scan_canaries
from core.canary.propose import materialize_canary
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
    monkeypatch.setattr("config.settings.canary_park_after_passes", 5)


def _base() -> Path:
    from config import settings

    return Path(settings.canaries_dir)


def _mk(name: str) -> None:
    got, err = materialize_canary(dict(_SPEC, name=name), vetting=False)
    assert got == name, err


def _green(name: str, n: int = 5) -> None:
    for _ in range(n):
        db.add_canary_run(
            task=name, trigger="scheduled", session_id=None, gate_results_json="[]", passed=True, outcome="pass"
        )


def test_long_green_canary_is_parked_not_removed():
    _mk("pin")
    _green("pin")
    run_maintenance()
    # The 2026-08-07 behaviour deleted it from the suite entirely.
    assert [c.name for c in scan_canaries(_base())] == ["pin"]
    assert load_canary("pin", base=_base()).parked is True


async def test_parked_canary_still_runs_on_full_and_named_sweeps(monkeypatch):
    """Parked means off the heartbeat, not out of reach: 'the world changed'
    sweeps and explicit names still fire it — that is what keeps its green
    history alive for the per-task tripwire."""
    from core.canary import runner as runner_mod

    _mk("pin")
    _green("pin")
    run_maintenance()
    parked = load_canary("pin", base=_base())
    assert parked.parked is True

    ran: list[str] = []

    async def _fake_run(c, trigger="manual", batch_id=None):
        ran.append(c.name)
        from core.canary.runner import CanaryRunResult

        return CanaryRunResult(task=c.name, passed=True, trigger=trigger)

    monkeypatch.setattr(runner_mod, "run_canary", _fake_run)
    monkeypatch.setattr(runner_mod, "scan_canaries", lambda *a, **k: [parked])

    await runner_mod.run_sweep(trigger="full")
    assert ran == ["pin"]
    ran.clear()
    await runner_mod.run_sweep(trigger="manual", names=["pin"])
    assert ran == ["pin"]


async def test_parked_canary_is_excluded_from_the_heartbeat(monkeypatch):
    from core.canary import runner as runner_mod
    from core.canary.parser import CanaryDef

    parked = CanaryDef(name="parked-one", prompt="x", gates=[{"name": "g", "command": "true", "watch_paths": []}], parked=True)
    active = CanaryDef(name="active-one", prompt="x", gates=[{"name": "g", "command": "true", "watch_paths": []}])

    ran: list[str] = []

    async def _fake_run(c, trigger="manual", batch_id=None):
        ran.append(c.name)
        from core.canary.runner import CanaryRunResult

        return CanaryRunResult(task=c.name, passed=True, trigger=trigger)

    monkeypatch.setattr(runner_mod, "run_canary", _fake_run)
    monkeypatch.setattr(runner_mod, "scan_canaries", lambda *a, **k: [parked, active])
    monkeypatch.setattr("config.settings.canary_heartbeat_per_night", 2)

    await runner_mod.run_sweep(trigger="scheduled")
    assert ran == ["active-one"]


def test_goodhart_lock_still_wins_over_parking():
    """The invariant parking must not disturb: a canary whose LATEST run
    failed is untouchable — except that a red run may UNPARK, because that
    amplifies the alarm rather than silencing it."""
    _mk("pin")
    _green("pin")
    db.add_canary_run(
        task="pin", trigger="scheduled", session_id=None, gate_results_json="[]", passed=False, outcome="gate_fail"
    )
    stats = run_maintenance()
    assert all(not v for v in stats.values())
    assert load_canary("pin", base=_base()).parked is False
