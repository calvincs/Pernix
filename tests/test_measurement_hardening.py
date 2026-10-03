"""Regression tests for the 2026-09-04 trust-loop hardening, W4: the grader
hold-out, including the assertion that running it leaves post_mortems and
the memory corpus untouched.

The rest of W4 (the drift z-test, receipts, unfounded-proposal holds) went
with the adaptive layer in 3.2, and its tests with it.
"""

import json
from pathlib import Path

import pytest

from db import models as db

# ---------------------------------------------------------------------------
# Grader hold-out
# ---------------------------------------------------------------------------


def test_holdout_fixtures_are_well_formed():
    from core.reflect import FAILURE_CAUSES
    from core.reflect_holdout import load_fixtures

    fixtures = load_fixtures()
    assert 8 <= len(fixtures) <= 10
    assert len({f["id"] for f in fixtures}) == len(fixtures)
    for f in fixtures:
        assert f["expected_verdict"] in ("pass", "retry", "escalate")
        assert f["expected_failure_cause"] in FAILURE_CAUSES
        assert (f["expected_failure_cause"] == "none") == (f["expected_verdict"] == "pass")
        assert f.get("note")
    # The set has to be able to catch over-strictness as well as laxity.
    verdicts = {f["id"]: f["expected_verdict"] for f in fixtures}
    assert sum(1 for v in verdicts.values() if v == "pass") >= 3
    assert "escalate" in verdicts.values()


def test_build_evidence_uses_the_headings_the_rubric_names():
    from core.reflect_holdout import build_evidence, load_fixtures

    blob = build_evidence(load_fixtures()[1])
    assert "TOOL EXECUTION SUMMARY:" in blob
    assert "USER REQUEST:" in blob
    assert "AGENT FINAL RESPONSE:" in blob
    assert "ATTEMPT TRANSCRIPT" in blob


async def test_run_holdout_scores_each_case(monkeypatch, tmp_path):
    """A stubbed grader that gets one case wrong scores 2/3."""
    from core.reflect import ReflectResult
    from core.reflect_holdout import STATE_KEY, run_holdout

    monkeypatch.setattr("config.settings.llm_model", "stub-model")
    _write_fixture(tmp_path, "a", "pass", "none")
    _write_fixture(tmp_path, "b", "retry", "agent")
    _write_fixture(tmp_path, "c", "escalate", "task")

    answers = {"a": ("pass", "none"), "b": ("retry", "scout"), "c": ("escalate", "task")}

    async def _fake(evidence, model):
        case = evidence.split("USER REQUEST:\n")[1].split("\n")[0]
        verdict, cause = answers[case]
        return ReflectResult(verdict=verdict, failure_cause=cause)

    monkeypatch.setattr("core.reflect_holdout._grade_evidence", _fake)

    report = await run_holdout(tmp_path)
    assert report["n"] == 3
    assert report["accuracy"] == pytest.approx(2 / 3, abs=1e-4)
    assert report["model"] == "stub-model"
    assert report["by_case"]["b"] == {"expected": "retry/agent", "got": "retry/scout", "ok": False}
    assert report["by_case"]["a"]["ok"] is True
    # Cause only matters on a non-pass: "pass" is the whole answer.
    assert report["by_case"]["a"]["expected"] == "pass"
    assert json.loads(db.get_snooze_state(STATE_KEY))["accuracy"] == report["accuracy"]


async def test_run_holdout_survives_a_grader_that_throws(monkeypatch, tmp_path):
    from core.reflect_holdout import run_holdout

    monkeypatch.setattr("config.settings.llm_model", "stub-model")
    _write_fixture(tmp_path, "a", "pass", "none")

    async def _boom(evidence, model):
        raise RuntimeError("provider down")

    monkeypatch.setattr("core.reflect_holdout._grade_evidence", _boom)

    report = await run_holdout(tmp_path)
    assert report["n"] == 0 and report["accuracy"] is None
    assert report["by_case"]["a"]["error"] == "RuntimeError"


async def test_run_holdout_writes_nothing_into_the_loop(monkeypatch, tmp_path):
    """The hold-out must stay a hold-out: no post-mortems, no sessions, no
    memory, no workspace files. A fixture the loop can learn from is
    training data with a score attached."""
    from core.reflect import ReflectResult
    from core.reflect_holdout import run_holdout

    monkeypatch.setattr("config.settings.llm_model", "stub-model")

    async def _fake(evidence, model):
        return ReflectResult(verdict="pass", failure_cause="none")

    monkeypatch.setattr("core.reflect_holdout._grade_evidence", _fake)

    from config import settings
    from db.database import connect_sessions

    def _counts():
        with connect_sessions() as conn:
            return (
                conn.execute("SELECT COUNT(*) c FROM post_mortems").fetchone()["c"],
                conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"],
                conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"],
            )

    memory_dir = Path(settings.memory_dir)
    workspace_dir = Path(settings.workspace_dir)
    memory_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir.mkdir(parents=True, exist_ok=True)

    before = _counts()
    memory_before = sorted(p.name for p in memory_dir.rglob("*"))
    workspace_before = sorted(p.name for p in workspace_dir.rglob("*"))

    report = await run_holdout()  # the real fixture set
    assert report["n"] >= 8

    assert _counts() == before
    assert sorted(p.name for p in memory_dir.rglob("*")) == memory_before
    assert sorted(p.name for p in workspace_dir.rglob("*")) == workspace_before


def _write_fixture(directory, case_id, verdict, cause):
    (Path(directory) / f"{case_id}.json").write_text(
        json.dumps(
            {
                "id": case_id,
                "user_request": case_id,
                "transcript_excerpt": "[ASSISTANT]\nx",
                "final_response": "x",
                "expected_verdict": verdict,
                "expected_failure_cause": cause,
                "note": "fixture",
            }
        ),
        encoding="utf-8",
    )


def test_load_fixtures_skips_malformed_files(tmp_path):
    from core.reflect_holdout import load_fixtures

    _write_fixture(tmp_path, "good", "pass", "none")
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "incomplete.json").write_text(json.dumps({"id": "x"}), encoding="utf-8")

    assert [f["id"] for f in load_fixtures(tmp_path)] == ["good"]


def test_holdout_schedule_installs_from_settings(monkeypatch):
    from core.extensions import scheduling

    monkeypatch.setattr("config.settings.grader_holdout_enabled", True)
    monkeypatch.setattr("config.settings.grader_holdout_schedule", "30 3 * * *")

    added = {}

    class _Scheduler:
        def add_job(self, fn, **kwargs):
            added[kwargs.get("id")] = kwargs

    monkeypatch.setattr(scheduling, "_get_scheduler", lambda: _Scheduler())
    scheduling.ensure_grader_holdout_schedule()
    assert "_grader_holdout" in added
    assert added["_grader_holdout"]["kwargs"]["meta"]["kind"] == "grader_holdout"

    added.clear()
    monkeypatch.setattr("config.settings.grader_holdout_enabled", False)
    scheduling.ensure_grader_holdout_schedule()
    assert added == {}
