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


def test_missing_verdict_coerces_to_retry():
    """Contract CHANGED 2026-08-25 (second ARC-3 sweep): a grade with no
    verdict field is a malformed grade, and defaulting it to "pass" made a
    broken grader look like a confident approval (field case: contentless
    confidence-0.0 passes). Missing verdict now coerces to retry, matching
    the invalid-verdict precedent."""
    r = _result_from_data({"reasoning": "did stuff"}, "m", 1)
    assert r.verdict == "retry"


# ---------------------------------------------------------------------------
# Experience read (intangibles) — schema pinned for Candor emission, the
# post-mortem payload, and dream evidence packs.
# ---------------------------------------------------------------------------

from core.reflect import _sanitize_experience  # noqa: E402


def test_experience_absent_defaults_empty():
    r = _result_from_data({"verdict": "pass"}, "m", 0)
    assert r.experience == {}


def test_experience_parses_and_sanitizes():
    data = {
        "verdict": "pass",
        "experience": {
            "user_sentiment": "Frustrated",
            "clarification_loop": True,
            "first_response_sufficient": False,
            "friction": ["Misread Intent!", "misread intent", "tool_noise"],
            "user_observations": ["  Prefers terse answers  ", ""],
            "note": "User had to re-ask twice.",
        },
    }
    r = _result_from_data(data, "m", 0)
    assert r.experience["user_sentiment"] == "frustrated"
    assert r.experience["clarification_loop"] is True
    assert r.experience["first_response_sufficient"] is False
    # Normalized to snake_case and deduped
    assert r.experience["friction"] == ["misread_intent", "tool_noise"]
    assert r.experience["user_observations"] == ["Prefers terse answers"]
    assert r.experience["note"] == "User had to re-ask twice."


def test_experience_unknown_sentiment_and_missing_booleans():
    exp = _sanitize_experience({"user_sentiment": "elated", "clarification_loop": "yes"})
    assert exp["user_sentiment"] == "unknown"
    # Non-boolean answers are dropped, never coerced — an unanswered question
    # must not become a frequency observation.
    assert "clarification_loop" not in exp
    assert exp["friction"] == [] and exp["user_observations"] == []


def test_experience_caps_enforced():
    exp = _sanitize_experience(
        {
            "friction": [f"label_{i}" for i in range(10)],
            "user_observations": ["x" * 900] * 5,
            "note": "n" * 900,
        }
    )
    assert len(exp["friction"]) == 6
    assert len(exp["user_observations"]) == 3
    assert all(len(o) <= 400 for o in exp["user_observations"])
    assert len(exp["note"]) == 500


def test_experience_disabled_by_setting(monkeypatch):
    monkeypatch.setattr("config.settings.reflect_experience", False)
    r = _result_from_data({"verdict": "pass", "experience": {"user_sentiment": "satisfied"}}, "m", 0)
    assert r.experience == {}


def test_cited_policies_parsed_capped_and_sanitized():
    r = _result_from_data(
        {
            "verdict": "pass",
            "cited_policies": ["[verify-on-disk]", "plain-id", "", 42, "a", "b", "c", "d"],
        },
        "m",
        0,
    )
    # Brackets stripped, empties/non-strings dropped, capped at 5.
    assert r.cited_policies[0] == "verify-on-disk"
    assert "plain-id" in r.cited_policies
    assert len(r.cited_policies) == 5
    # Absent → empty default (the honest common case).
    assert _result_from_data({"verdict": "pass"}, "m", 0).cited_policies == []


# ---------------------------------------------------------------------------
# Materiality floor for non-pass verdicts (2026-08-27 verdict audit)
# ---------------------------------------------------------------------------


def test_low_confidence_nonpass_downgrades_to_pass():
    r = _result_from_data(
        {"verdict": "escalate", "reasoning": "cannot see the evidence", "failure_cause": "env", "confidence": 0.45},
        "m",
        0,
    )
    assert r.verdict == "pass"
    assert r.failure_cause == "none"
    assert "downgraded from escalate" in r.reasoning
    r2 = _result_from_data({"verdict": "retry", "failure_cause": "agent", "confidence": 0.3}, "m", 0)
    assert r2.verdict == "pass"


def test_confident_nonpass_survives_floor():
    r = _result_from_data({"verdict": "retry", "failure_cause": "agent", "confidence": 0.6}, "m", 0)
    assert r.verdict == "retry"
    assert r.failure_cause == "agent"


def test_floor_never_flips_coerced_verdicts():
    """A malformed grade carries no meaningful confidence — flipping it to
    pass would undo the deliberate conservative coercion."""
    r = _result_from_data({"reasoning": "no verdict field"}, "m", 0)  # missing verdict
    assert r.verdict == "retry"
    r2 = _result_from_data({"verdict": "fail", "failure_cause": "agent", "confidence": 0.2}, "m", 0)  # invalid verdict
    assert r2.verdict == "retry"


def test_floor_disabled_by_config(monkeypatch):
    monkeypatch.setattr("config.settings.reflect_nonpass_confidence_floor", 0)
    r = _result_from_data({"verdict": "retry", "failure_cause": "agent", "confidence": 0.1}, "m", 0)
    assert r.verdict == "retry"


def test_effective_workspace_falls_back_without_live_session():
    from core.reflect import _effective_workspace

    root, overridden = _effective_workspace("no-such-session")
    assert overridden is False
    assert root  # shared workspace path


def test_effective_workspace_honors_override(monkeypatch):
    from types import SimpleNamespace

    from core.reflect import _effective_workspace

    live = SimpleNamespace(workspace_override="/tmp/sandbox-x")
    monkeypatch.setattr("sessions.manager.get_manager", lambda: SimpleNamespace(get=lambda _s: live))
    root, overridden = _effective_workspace("s1")
    assert (root, overridden) == ("/tmp/sandbox-x", True)


def test_floor_ignores_absent_confidence():
    """A non-pass grade that omits confidence entirely is incomplete output,
    not a self-assessed ambiguity — it stays conservative."""
    r = _result_from_data({"verdict": "retry", "failure_cause": "agent"}, "m", 0)
    assert r.verdict == "retry"
