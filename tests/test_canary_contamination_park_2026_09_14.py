"""L07 — a canary that was contaminated by design, alerting every night.

`data/canaries/workspace-organizer-evidence-gate`, written by a generator,
told the agent to operate on `./data/workspace` — the live agent workspace,
outside the canary sandbox. Every run was correctly recorded
`outcome='contaminated'` (3 of 3), and the suite then raised a nightly
high-urgency alert plus a "chronically failing task(s)" notification about a
task that had never measured anything. `gen-grep-count` and
`gen-json-transform` were contaminated once each for naming sibling canaries
in their transcripts.

Two halves are pinned here: the proposal never becomes a file (the same
patterns the runtime contamination scan flags are refused at generation
time, with the reason filed where a human will see it), and a task that is
already in the suite and contaminated three runs running parks itself, with
one notification and no further nightly alarm.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.canary.contamination import contamination_record
from core.canary.maintain import _CONTAMINATION_WINDOW, check_suite_health, run_maintenance
from core.canary.parser import load_canary, scan_canaries
from core.canary.propose import isolation_violation, materialize_canary, queue_canary_proposals
from db import models as db

_SPEC = {
    "name": "clean-task",
    "prompt": "Create out.txt containing DONE.",
    "gates": [{"name": "out", "command": "grep -qx DONE out.txt", "watch_paths": []}],
    "rationale": "test canary",
}


@pytest.fixture(autouse=True)
def _canaries_tmp(monkeypatch, tmp_path):
    monkeypatch.setattr("config.settings.canaries_dir", str(tmp_path / "canaries"))
    monkeypatch.setattr("config.settings.skills_dir", str(tmp_path / "skills"))
    monkeypatch.setattr("config.settings.canary_enabled", True)
    monkeypatch.setattr("config.settings.canary_auto_maintain", True)
    monkeypatch.setattr("config.settings.canary_auto_admit", False)
    monkeypatch.setattr("config.settings.canary_vetting_runs", 3)
    monkeypatch.setattr("config.settings.canary_park_after_passes", 5)


def _base() -> Path:
    from config import settings

    return Path(settings.canaries_dir)


def _mk(name: str, vetting: bool = False) -> None:
    got, err = materialize_canary(dict(_SPEC, name=name), vetting=vetting)
    assert got == name, err


def _contaminated_run(name: str, finding: str = "read outside the workspace: /app/data/workspace") -> None:
    db.add_canary_run(
        task=name,
        trigger="scheduled",
        session_id=None,
        gate_results_json=json.dumps([contamination_record([finding])]),
        passed=False,
        outcome="contaminated",
    )


def _park_notes() -> list[dict]:
    return [n for n in db.get_notifications() if "contaminated on" in (n.get("title") or "")]


# ---------------------------------------------------------------------------
# (a) generation time
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prompt",
    [
        # The live offender, verbatim in shape.
        "The workspace root at ./data/workspace contains unsorted evidence files; organise them.",
        "Read /app/data/settings.json and summarise it.",
        "Copy /home/calvin/notes.md into out.txt.",
        "Compare your gates with the ones in data/canaries and report.",
    ],
)
def test_the_validator_refuses_a_task_written_outside_the_sandbox(prompt):
    assert isolation_violation(dict(_SPEC, prompt=prompt)) is not None


def test_the_validator_refuses_a_prompt_naming_another_canary():
    _mk("grep-count")
    spec = dict(_SPEC, name="new-task", prompt="Do what grep-count does, but for JSON.")
    assert "names other canaries" in (isolation_violation(spec) or "")


def test_the_validator_refuses_a_seed_file_that_steers_out_of_the_workspace():
    spec = dict(_SPEC, files={"README.md": "All inputs live under /app/data/workspace."})
    assert "files[README.md]" in (isolation_violation(spec) or "")


def test_a_workspace_relative_task_is_accepted():
    assert isolation_violation(_SPEC) is None
    # Toolchain paths are not knowledge — the runtime scan ignores them too.
    assert isolation_violation(dict(_SPEC, prompt="Run /usr/bin/python3 build.py, then check out.txt.")) is None


def test_a_contaminated_proposal_never_becomes_a_file():
    bad = dict(_SPEC, name="workspace-organizer-evidence-gate", prompt="Organise ./data/workspace by evidence type.")
    got, err = materialize_canary(bad)
    assert got is None
    assert "breaks canary isolation" in err
    assert load_canary("workspace-organizer-evidence-gate", base=_base()) is None


def test_the_rejection_reason_is_recorded_on_a_proposal():
    bad = dict(_SPEC, name="workspace-organizer", prompt="Organise ./data/workspace by evidence type.")
    assert queue_canary_proposals([bad], "skill-change", session_id="s-1") == 0

    rejected = db.adaptive_list_proposals(status="rejected", limit=20)
    mine = [r for r in rejected if "workspace-organizer" in (r.get("rationale") or "")]
    assert len(mine) == 1
    assert "breaks canary isolation" in mine[0]["rationale"]
    assert json.loads(mine[0]["payload_json"])["rejected_reason"]
    # It must not occupy review-queue budget, and re-deriving it every cycle
    # must not stack copies.
    assert mine[0]["status"] == "rejected"
    queue_canary_proposals([bad], "skill-change", session_id="s-1")
    assert (
        len(
            [
                r
                for r in db.adaptive_list_proposals(status="rejected", limit=20)
                if "workspace-organizer" in (r.get("rationale") or "")
            ]
        )
        == 1
    )


# ---------------------------------------------------------------------------
# (b) maintenance
# ---------------------------------------------------------------------------


def test_three_consecutive_contaminated_runs_park_the_task_once():
    _mk("evidence-gate")

    for _ in range(_CONTAMINATION_WINDOW - 1):
        _contaminated_run("evidence-gate")
    run_maintenance()
    assert load_canary("evidence-gate", base=_base()).parked is False
    assert _park_notes() == []

    _contaminated_run("evidence-gate")
    stats = run_maintenance()
    assert [i["name"] for i in stats["parked_contaminated"]] == ["evidence-gate"]
    assert load_canary("evidence-gate", base=_base()).parked is True
    notes = _park_notes()
    assert len(notes) == 1
    assert "/app/data/workspace" in notes[0]["body"]  # the reason, not just the verdict

    # A fourth contaminated run says nothing new.
    _contaminated_run("evidence-gate")
    stats = run_maintenance()
    assert stats["parked_contaminated"] == []
    assert len(_park_notes()) == 1
    assert load_canary("evidence-gate", base=_base()).parked is True


def test_a_contaminated_run_never_unparks_a_parked_task():
    """The red-run unpark exists because a parked canary promised green.

    A contaminated run promises nothing — letting it unpark would ping-pong
    the task between parked and unparked forever, one rewrite per sweep.
    """
    _mk("parked-task")
    for _ in range(_CONTAMINATION_WINDOW):
        _contaminated_run("parked-task")
    run_maintenance()
    assert load_canary("parked-task", base=_base()).parked is True

    _contaminated_run("parked-task")
    stats = run_maintenance()
    assert stats["unparked"] == []
    assert load_canary("parked-task", base=_base()).parked is True


def test_an_honest_failure_still_unparks_and_is_never_parked():
    """The Goodhart lock is untouched: a failing canary keeps its alarm."""
    _mk("real-failure")
    for _ in range(4):
        db.add_canary_run(
            task="real-failure",
            trigger="scheduled",
            session_id=None,
            gate_results_json="[]",
            passed=False,
            outcome="gate_fail",
        )
    stats = run_maintenance()
    assert stats["parked_contaminated"] == []
    assert load_canary("real-failure", base=_base()).parked is False
    assert stats["unhealthy"] == ["real-failure"]


def test_a_parked_task_stops_feeding_the_nightly_health_alert():
    _mk("gate-that-cannot-run")
    for _ in range(_CONTAMINATION_WINDOW):
        _contaminated_run("gate-that-cannot-run")

    # Before parking, its rows read as a chronic failure.
    assert check_suite_health(list(scan_canaries(_base())))["chronic"] == ["gate-that-cannot-run"]

    stats = run_maintenance()
    assert stats["unhealthy"] == []
    assert check_suite_health(list(scan_canaries(_base())))["chronic"] == []
    assert not [n for n in db.get_notifications() if "chronically failing" in (n.get("title") or "")]
