""" "I could not see the evidence" was recorded in the pass column.

Reflect's materiality floor is deliberate and stays: a non-pass verdict the
grader itself scores below `reflect_nonpass_confidence_floor` is ambiguous
evidence, and forcing another attempt over ambiguity is how a worker gets made
to redo finished research. The 2026-08-27 calibration says so, and
tests/test_reflect_schema.py pins it in all four directions.

The defect was that `verdict` was the ONLY channel. A grade that meant "no
retry is warranted, and I could not verify the central claim" came out as
`verdict="pass"`, `failure_cause="none"`, and a `[downgraded from retry: ...]`
marker appended to the END of `reasoning` — where nobody saw it.
`get_worker_result` gated on `verdict == "pass"` and returned the output
clean; `_finalize_worker` stamped `# AUTO-STAMPED (reflect=pass...)`; the
parent's resume manifest listed it as `pass`; and `get_worker_transcript`
clipped reasoning at 200 characters, which is where the marker was not.
Confidence was persisted and rendered nowhere.

Retry disposition and verification state are now two fields. `verdict` still
drives control flow and still downgrades exactly as documented; `verification`
carries verified / partial / unknown, and every consumer of the verdict reads
it. Deterministic checks report their own execution facts on top: a gate that
could not run is an unavailable check, not a confident one, and no amount of
model confidence promotes a missing receipt.
"""

from __future__ import annotations

import asyncio
import json as _json
from pathlib import Path

import pytest

from core.extensions.orchestration import get_worker_result, get_worker_transcript
from core.extensions.orchestration import report as wreport
from core.gates import GateResult
from core.reflect import _result_from_data, apply_verification_receipts
from db import models as db
from sessions.manager import SessionManager

CANNOT_SEE = (
    "The worker reports the migration was applied and the counts match, but the tool "
    "results it cites were elided from my evidence window, so I cannot see the actual "
    "query output. Nothing here proves the numbers are wrong either; I simply have no "
    "way to verify the central claim from what I was given, and forcing another attempt "
    "would not produce evidence I can read."
)


@pytest.fixture
def mgr(monkeypatch):
    fresh = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", fresh)
    return fresh


def _worker(mgr, grade: dict, last="Migration applied; counts verified."):
    parent = mgr.create_session(title="P")
    wid = mgr.create_session(title="W", session_type="worker", parent_session_id=parent)
    wreport.begin_run(wid, workspace_home=None, reason="spawn")
    db.add_message(wid, "user", "migrate and verify")
    db.add_message(wid, "assistant", last)
    db.add_message(wid, "reflect", _json.dumps(grade))
    w = mgr.get(wid)
    w.termination_reason = "complete"
    return parent, wid, w


# ---------------------------------------------------------------------------
# The grade itself
# ---------------------------------------------------------------------------


def test_the_documented_downgrade_still_happens():
    """Not a revert. A low-confidence non-pass still stops driving a retry."""
    for verdict in ("retry", "escalate"):
        r = _result_from_data(
            {"verdict": verdict, "reasoning": CANNOT_SEE, "failure_cause": "verification", "confidence": 0.4},
            "m",
            10,
        )
        assert r.verdict == "pass"
        assert r.failure_cause == "none"
        assert f"downgraded from {verdict}" in r.reasoning


def test_but_it_is_no_longer_recorded_as_verified():
    r = _result_from_data(
        {"verdict": "retry", "reasoning": CANNOT_SEE, "failure_cause": "verification", "confidence": 0.4},
        "m",
        10,
    )
    assert r.verification == "unknown"
    assert "confidence" in r.verification_reason


def test_the_failure_cause_none_coercion_is_also_unverified():
    """Confidence-independent, and equally a 'no retry warranted' — not a
    statement that anything was checked."""
    r = _result_from_data(
        {"verdict": "escalate", "reasoning": CANNOT_SEE, "failure_cause": "none", "confidence": 0.95},
        "m",
        10,
    )
    assert r.verdict == "pass"
    assert r.verification == "unknown"


def test_a_genuine_pass_is_verified():
    r = _result_from_data(
        {"verdict": "pass", "reasoning": "ran the suite, 40/40, output quoted", "confidence": 0.9},
        "m",
        10,
    )
    assert r.verdict == "pass" and r.verification == "verified"


def test_absent_evidence_is_partial_not_verified():
    """A pass that still names something it could not see is not a clean bill."""
    r = _result_from_data(
        {
            "verdict": "pass",
            "reasoning": "looks right",
            "confidence": 0.9,
            "missing": "the migration's row counts were never shown",
        },
        "m",
        10,
    )
    assert r.verdict == "pass"
    assert r.verification == "partial"
    assert "missing" in r.verification_reason or "evidence" in r.verification_reason


def test_an_unmet_deliverable_is_partial_too():
    r = _result_from_data(
        {
            "verdict": "pass",
            "reasoning": "mostly there",
            "confidence": 0.9,
            "deliverables": [
                {"description": "migrate", "status": "met"},
                {"description": "verify", "status": "unknown"},
            ],
        },
        "m",
        10,
    )
    assert r.verification == "partial"


def test_the_model_may_state_its_own_verification():
    r = _result_from_data(
        {"verdict": "pass", "reasoning": "ok", "confidence": 0.9, "verification": "partial"},
        "m",
        10,
    )
    assert r.verification == "partial"


# ---------------------------------------------------------------------------
# Deterministic receipts
# ---------------------------------------------------------------------------


def _gate(name, *, passed=True, broken=False):
    return GateResult(name=name, command=f"run {name}", passed=passed, broken=broken)


def test_a_check_that_could_not_run_is_not_a_check_that_passed():
    r = _result_from_data({"verdict": "pass", "reasoning": "fine", "confidence": 0.95}, "m", 10)
    assert r.verification == "verified"
    apply_verification_receipts(r, [_gate("pytest", passed=False, broken=True)])
    assert r.verification == "partial"
    assert "pytest" in r.verification_reason


def test_a_failing_gate_leaves_verification_unknown():
    r = _result_from_data({"verdict": "pass", "reasoning": "fine", "confidence": 0.95}, "m", 10)
    apply_verification_receipts(r, [_gate("pytest", passed=False)])
    assert r.verification == "unknown"


def test_passing_gates_are_receipts_the_model_cannot_supply(monkeypatch):
    """Model confidence never promotes a missing receipt — but a check that
    actually ran and passed is evidence, and says which one it was."""
    r = _result_from_data(
        {"verdict": "retry", "reasoning": CANNOT_SEE, "failure_cause": "verification", "confidence": 0.4},
        "m",
        10,
    )
    assert r.verification == "unknown"
    apply_verification_receipts(r, [_gate("pytest"), _gate("ruff")])
    assert r.verification == "partial"
    assert "pytest" in r.verification_reason and "ruff" in r.verification_reason


# ---------------------------------------------------------------------------
# Every consumer of the verdict
# ---------------------------------------------------------------------------


def _downgraded_grade() -> dict:
    r = _result_from_data(
        {"verdict": "retry", "reasoning": CANNOT_SEE, "failure_cause": "verification", "confidence": 0.4},
        "m",
        10,
    )
    return {
        "verdict": r.verdict,
        "reasoning": r.reasoning,
        "failure_cause": r.failure_cause,
        "confidence": r.confidence,
        "verification": r.verification,
        "verification_reason": r.verification_reason,
    }


def test_the_parent_is_told_the_result_is_unverified(mgr):
    _parent, wid, _w = _worker(mgr, _downgraded_grade())
    out = get_worker_result(wid)
    assert out.startswith("# PASS BUT UNVERIFIED")
    assert "Migration applied" in out, "the output is still served — this is a label, not a block"


def test_the_finalize_stamp_says_so_too(mgr):
    _parent, wid, w = _worker(mgr, _downgraded_grade())
    asyncio.run(mgr._finalize_worker(w))
    stamp = Path(wreport.load_run(wid)["report_path"]).read_text()
    assert stamp.startswith("# PASS BUT UNVERIFIED")
    assert "AUTO-STAMPED (reflect=pass" not in stamp


def test_the_resume_manifest_says_so_too(mgr):
    parent, wid, _w = _worker(mgr, _downgraded_grade())
    line = [ln for ln in mgr._build_resume_message(mgr.get(parent)).splitlines() if wid in ln][0]
    assert "unverified" in line.lower()


def test_the_transcript_shows_it_where_the_marker_was_clipped(mgr):
    _parent, wid, _w = _worker(mgr, _downgraded_grade())
    line = [ln for ln in get_worker_transcript(wid).splitlines() if "reflect]" in ln][0]
    assert "verification=unknown" in line
    assert "downgraded from" not in line, "the marker is still clipped — that is why the field exists"


def test_a_verified_pass_stays_clean_everywhere(mgr):
    grade = {"verdict": "pass", "reasoning": "suite green, output quoted", "verification": "verified"}
    parent, wid, w = _worker(mgr, grade)
    assert not get_worker_result(wid).startswith("#")
    asyncio.run(mgr._finalize_worker(w))
    assert Path(wreport.load_run(wid)["report_path"]).read_text().startswith("# AUTO-STAMPED")
    line = [ln for ln in mgr._build_resume_message(mgr.get(parent)).splitlines() if wid in ln][0]
    assert line.strip().endswith("pass")


def test_a_grade_with_no_verification_field_is_unchanged(mgr):
    """Rows written before this field existed keep reading exactly as before."""
    parent, wid, _w = _worker(mgr, {"verdict": "pass", "reasoning": "old-style row"})
    assert not get_worker_result(wid).startswith("#")
    line = [ln for ln in mgr._build_resume_message(mgr.get(parent)).splitlines() if wid in ln][0]
    assert line.strip().endswith("pass")
