"""Phase 1c schema groundwork: ReflectResult new fields parse correctly.

These tests pin the on-the-wire shape for failure_cause / confidence /
deliverables so Phase 2 post-mortem writers and Phase 3 snooze consumers
have a stable contract.
"""

from core.reflect import FAILURE_CAUSES, Deliverable, ReflectResult, _result_from_data


def test_new_fields_default_when_absent():
    r = _result_from_data({"verdict": "pass", "reasoning": "ok"}, "m", 10)
    assert r.failure_cause == "none"
    assert r.confidence == 0.0
    assert r.deliverables == []
    assert r.artifact_id == ""


def test_failure_cause_accepts_valid_values():
    for cause in FAILURE_CAUSES:
        r = _result_from_data(
            {"verdict": "retry", "failure_cause": cause},
            "m",
            0,
        )
        assert r.failure_cause == cause


def test_failure_cause_rejects_unknown():
    r = _result_from_data(
        {"verdict": "retry", "failure_cause": "solar-flare"},
        "m",
        0,
    )
    assert r.failure_cause == "none"


def test_confidence_clamped():
    assert _result_from_data({"verdict": "pass", "confidence": 2.0}, "m", 0).confidence == 1.0
    assert _result_from_data({"verdict": "pass", "confidence": -0.5}, "m", 0).confidence == 0.0
    assert _result_from_data({"verdict": "pass", "confidence": 0.7}, "m", 0).confidence == 0.7


def test_confidence_coerces_strings_and_garbage():
    assert _result_from_data({"verdict": "pass", "confidence": "0.4"}, "m", 0).confidence == 0.4
    assert _result_from_data({"verdict": "pass", "confidence": "NaN-text"}, "m", 0).confidence == 0.0


def test_deliverables_parse_and_clamp_status():
    data = {
        "verdict": "pass",
        "deliverables": [
            {"description": "Write report.md", "status": "met", "evidence_ref": "report.md"},
            {"description": "Run tests", "status": "partial", "evidence_ref": "8/10"},
            {"description": "Deploy", "status": "bogus"},
        ],
    }
    r = _result_from_data(data, "m", 0)
    assert len(r.deliverables) == 3
    assert all(isinstance(d, Deliverable) for d in r.deliverables)
    assert r.deliverables[0].status == "met"
    assert r.deliverables[1].status == "partial"
    assert r.deliverables[2].status == "unknown"  # clamped from "bogus"


def test_deliverables_ignores_non_list_and_non_dict_entries():
    assert _result_from_data({"verdict": "pass", "deliverables": "not-a-list"}, "m", 0).deliverables == []
    r = _result_from_data({"verdict": "pass", "deliverables": ["string", 42, None]}, "m", 0)
    assert r.deliverables == []


def test_backward_compat_existing_fields_still_populate():
    data = {
        "verdict": "retry",
        "reasoning": "missed a step",
        "diagnostic": "wrong tool",
        "strategy": "try the other tool",
        "what_worked": "partial search",
        "what_failed": "wrong schema",
    }
    r = _result_from_data(data, "reflect-model", 123)
    assert r.verdict == "retry"
    assert r.reasoning == "missed a step"
    assert r.strategy == "try the other tool"
    assert r.reflect_model == "reflect-model"
    assert r.reflect_latency_ms == 123


# ---------------------------------------------------------------------------
# Verdict coercion: invalid values must NOT silently flip to "pass"
# ---------------------------------------------------------------------------
# Regression for workflow run e8c94b86 (2026-04-27): the reflect LLM
# emitted verdict='fail' (not in the valid {pass, retry, escalate} set)
# while the reasoning correctly said the deliverable was missing. The
# coercion silently flipped 'fail' to 'pass', which then cascaded
# through _finalize_step's pass-but-no-output guard. For callers without
# that guard, a fake "pass" would have shipped.


def test_invalid_verdict_coerces_to_retry_not_pass():
    """An out-of-schema verdict like 'fail' or 'failed' must coerce to
    'retry', not 'pass'. The model tried to say something other than
    pass, so don't pretend it said pass."""
    for bad in ("fail", "failed", "blocked", "in-progress", "", "true", "yes"):
        r = _result_from_data({"verdict": bad, "reasoning": "x"}, "m", 0)
        assert r.verdict == "retry", f"verdict={bad!r} should coerce to 'retry', got {r.verdict!r}"


def test_valid_verdicts_pass_through_unchanged():
    for good in ("pass", "retry", "escalate"):
        r = _result_from_data({"verdict": good, "reasoning": "x"}, "m", 0)
        assert r.verdict == good


def test_missing_verdict_defaults_to_pass():
    """When verdict is absent from the JSON entirely, the historical default
    is 'pass' (assume success when unspecified). This is the correct null
    default — only out-of-schema VALUES coerce to retry."""
    r = _result_from_data({"reasoning": "no verdict in the JSON"}, "m", 0)
    assert r.verdict == "pass"
