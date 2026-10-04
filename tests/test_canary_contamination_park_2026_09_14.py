"""L07 — a canary that was contaminated by design, alerting every night.

`data/canaries/workspace-organizer-evidence-gate`, written by a generator,
told the agent to operate on `./data/workspace` — the live agent workspace,
outside the canary sandbox. Every run was correctly recorded
`outcome='contaminated'` (3 of 3), and the suite then raised a nightly
high-urgency alert plus a "chronically failing task(s)" notification about a
task that had never measured anything. `gen-grep-count` and
`gen-json-transform` were contaminated once each for naming sibling canaries
in their transcripts.

Pinned here: a spec never becomes a file when its task points outside the
sandbox (the same patterns the runtime contamination scan flags are refused
at creation time). The other half — a task contaminated three runs running
parked itself — left with suite auto-maintenance in 3.2; contamination is a
record on the run row now, never an alarm.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.canary.parser import load_canary
from core.canary.propose import isolation_violation, materialize_canary
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


def _base() -> Path:
    from config import settings

    return Path(settings.canaries_dir)


def _mk(name: str) -> None:
    got, err = materialize_canary(dict(_SPEC, name=name))
    assert got == name, err


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
