"""Post-mortem → tool/skill performance synthesis.

Two test layers:
 1. Pure `attribute()` rules — no DB.
 2. End-to-end `run()` driver — exercises watermark + idempotency.
"""

import json

import pytest

from core import synthesis
from core.signals import from_row
from db import models as db

# ---------------------------------------------------------------------------
# Pure attribution rules
# ---------------------------------------------------------------------------


def _pm(
    verdict="pass",
    failure_cause="none",
    confidence=0.9,
    execution_mode="inline",
    scout_summary=None,
    tool_summary=None,
    scout_viability="verified",
):
    payload = {}
    if scout_summary is not None:
        payload["scout_summary"] = scout_summary
    if tool_summary is not None:
        payload["tool_summary"] = tool_summary
    return {
        "id": "pmX",
        "verdict": verdict,
        "failure_cause": failure_cause,
        "confidence": confidence,
        "execution_mode": execution_mode,
        "scout_viability": scout_viability,
        "payload_json": json.dumps(payload),
    }


def test_pass_credits_recommended_skills():
    row = _pm(scout_summary={"recommended_skills": ["youtube-summary"], "from_fallback": False})
    attrs = synthesis.attribute(row)
    skill_attrs = [a for a in attrs if a.signal_type == "skill"]
    assert len(skill_attrs) == 1
    assert skill_attrs[0].subject == "youtube-summary"
    assert skill_attrs[0].delta_successes == 1
    assert skill_attrs[0].delta_failures == 0


def test_retry_with_skill_cause_blames_the_skill():
    row = _pm(
        verdict="retry",
        failure_cause="skill",
        confidence=0.8,
        scout_summary={"recommended_skills": ["broken-skill"], "from_fallback": False},
    )
    attrs = [a for a in synthesis.attribute(row) if a.signal_type == "skill"]
    assert attrs[0].delta_failures == 1


def test_retry_with_scout_cause_blames_recommended_skills_too():
    # When cause=scout, scout recommended the wrong skill — skill is penalized.
    row = _pm(
        verdict="retry",
        failure_cause="scout",
        confidence=0.9,
        scout_summary={"recommended_skills": ["wrong-skill"], "from_fallback": False},
    )
    attrs = [a for a in synthesis.attribute(row) if a.signal_type == "skill"]
    assert attrs[0].delta_failures == 1


def test_retry_with_agent_cause_does_not_blame_skills():
    # When cause=agent, the skill is fine — agent executed the plan badly.
    row = _pm(
        verdict="retry",
        failure_cause="agent",
        confidence=0.9,
        scout_summary={"recommended_skills": ["innocent-skill"], "from_fallback": False},
    )
    skill_attrs = [a for a in synthesis.attribute(row) if a.signal_type == "skill"]
    assert skill_attrs == []


def test_retry_with_env_cause_does_not_blame_skills():
    row = _pm(
        verdict="retry",
        failure_cause="env",
        confidence=0.9,
        scout_summary={"recommended_skills": ["innocent-skill"], "from_fallback": False},
    )
    assert [a for a in synthesis.attribute(row) if a.signal_type == "skill"] == []


def test_fallback_scout_produces_no_attributions():
    row = _pm(
        verdict="pass",
        scout_summary={"recommended_skills": ["s"], "from_fallback": True},
        tool_summary={"file_write": {"calls": 1, "failures": 0}},
    )
    assert synthesis.attribute(row) == []


def test_low_confidence_non_pass_produces_no_attributions():
    row = _pm(
        verdict="retry",
        failure_cause="scout",
        confidence=0.3,
        scout_summary={"recommended_skills": ["s"], "from_fallback": False},
    )
    assert synthesis.attribute(row) == []


def test_low_confidence_pass_still_counts():
    # Pass verdicts are acted on even at low confidence — they represent
    # completed work, not reflect's guess at a diagnosis.
    row = _pm(
        verdict="pass",
        confidence=0.3,
        scout_summary={"recommended_skills": ["s"], "from_fallback": False},
    )
    skill_attrs = [a for a in synthesis.attribute(row) if a.signal_type == "skill"]
    assert len(skill_attrs) == 1


def test_tool_summary_half_or_more_failures_penalizes():
    row = _pm(
        verdict="pass",
        tool_summary={"flaky_tool": {"calls": 4, "failures": 2}},
    )
    tool_attrs = [a for a in synthesis.attribute(row) if a.signal_type == "tool"]
    assert len(tool_attrs) == 1
    assert tool_attrs[0].subject == "flaky_tool"
    assert tool_attrs[0].delta_failures == 1


def test_tool_summary_clean_run_credits():
    row = _pm(
        verdict="pass",
        tool_summary={"reliable": {"calls": 5, "failures": 0}},
    )
    tool_attrs = [a for a in synthesis.attribute(row) if a.signal_type == "tool"]
    assert tool_attrs[0].delta_successes == 1


def test_tool_summary_mixed_below_threshold_is_skipped():
    # 1 failure in 10 calls — below 50% threshold AND not strictly clean → skipped.
    row = _pm(
        verdict="pass",
        tool_summary={"mixed": {"calls": 10, "failures": 1}},
    )
    tool_attrs = [a for a in synthesis.attribute(row) if a.signal_type == "tool"]
    assert tool_attrs == []


def test_tool_summary_zero_calls_is_skipped():
    row = _pm(tool_summary={"unused": {"calls": 0, "failures": 0}})
    tool_attrs = [a for a in synthesis.attribute(row) if a.signal_type == "tool"]
    assert tool_attrs == []


def test_execution_mode_produces_no_attribution():
    """Execution mode attribution was removed — no mode signals are generated."""
    for verdict, cause, mode in [
        ("pass", "none", "workers"),
        ("retry", "scout", "tasks"),
        ("pass", "none", "inline"),
    ]:
        row = _pm(
            verdict=verdict,
            failure_cause=cause,
            confidence=0.9,
            execution_mode=mode,
            scout_summary={"from_fallback": False, "execution_mode": mode},
        )
        mode_attrs = [a for a in synthesis.attribute(row) if a.signal_type == "execution_mode"]
        assert mode_attrs == [], f"expected no mode attrs for verdict={verdict}, mode={mode}"


def test_malformed_payload_skips_skill_and_tool_attribution():
    """Malformed payload → no skill/tool attrs (no scout_summary / tool_summary).

    The structured execution_mode column is independent of the JSON blob,
    so a valid mode can still credit even if the payload is garbage.
    """
    row = {
        "id": "pm1",
        "verdict": "pass",
        "failure_cause": "none",
        "confidence": 0.9,
        "execution_mode": "inline",
        "scout_viability": "verified",
        "payload_json": "not-json-at-all",
    }
    attrs = synthesis.attribute(row)
    skill_or_tool = [a for a in attrs if a.signal_type in ("skill", "tool")]
    assert skill_or_tool == []


def test_escalate_produces_no_attributions():
    row = _pm(verdict="escalate", scout_summary={"recommended_skills": ["s"], "from_fallback": False})
    assert synthesis.attribute(row) == []


# ---------------------------------------------------------------------------
# End-to-end run(): watermark, idempotency, signal upsert
# ---------------------------------------------------------------------------


def _seed_post_mortem(
    session_id: str,
    verdict="pass",
    failure_cause="none",
    confidence=0.9,
    scout_summary=None,
    tool_summary=None,
    execution_mode="inline",
) -> str:
    payload = {"scout_summary": scout_summary or {"from_fallback": False}}
    if tool_summary is not None:
        payload["tool_summary"] = tool_summary
    return db.add_post_mortem(
        session_id=session_id,
        attempt=1,
        verdict=verdict,
        failure_cause=failure_cause,
        confidence=confidence,
        reflect_model="test",
        reflect_latency_ms=10,
        scout_viability="verified",
        execution_mode=execution_mode,
        payload_json=json.dumps(payload),
    )


def test_run_processes_pending_post_mortems_and_marks_synthesized():
    # Two separate sessions so per-session dedupe doesn't collapse them.
    sid_a = db.create_session(title="run-process-a")
    sid_b = db.create_session(title="run-process-b")
    db.delete_signal("skill", "run-proc-skill-a")
    db.delete_signal("skill", "run-proc-skill-b")
    pm1 = _seed_post_mortem(
        sid_a, verdict="pass", scout_summary={"recommended_skills": ["run-proc-skill-a"], "from_fallback": False}
    )
    pm2 = _seed_post_mortem(
        sid_b, verdict="pass", scout_summary={"recommended_skills": ["run-proc-skill-b"], "from_fallback": False}
    )

    stats = synthesis.run()
    assert stats.processed >= 2

    # Both marked synthesized
    assert db.get_post_mortem(pm1)["synthesized_at"] is not None
    assert db.get_post_mortem(pm2)["synthesized_at"] is not None

    # Signals upserted
    sig_a = db.get_signal("skill", "run-proc-skill-a")
    assert sig_a is not None
    assert sig_a["successes"] == 1


def test_run_is_idempotent_second_run_is_noop():
    sid = db.create_session(title="idempotent")
    db.delete_signal("skill", "idem-skill")
    _seed_post_mortem(sid, verdict="pass", scout_summary={"recommended_skills": ["idem-skill"], "from_fallback": False})
    synthesis.run()
    first = db.get_signal("skill", "idem-skill")
    assert first["successes"] == 1
    # Second run: no unsynthesized rows → no change
    stats = synthesis.run()
    assert stats.processed == 0
    second = db.get_signal("skill", "idem-skill")
    assert second["successes"] == 1  # not double-counted


def test_run_handles_empty_queue():
    # Mark everything synthesized, then run — should be a cheap no-op.
    synthesis.run()  # drain
    stats = synthesis.run()
    assert stats.processed == 0
    assert stats.attributions == 0


def test_run_continues_after_single_row_failure(monkeypatch):
    """A broken attribution on one row must not block the rest of the batch.

    Use separate sessions so per-session dedupe doesn't collapse the two
    into a single latest-attribute call.
    """
    sid_good = db.create_session(title="resilient-good")
    sid_bad = db.create_session(title="resilient-bad")
    db.delete_signal("skill", "good-skill")
    good_pm = _seed_post_mortem(
        sid_good, verdict="pass", scout_summary={"recommended_skills": ["good-skill"], "from_fallback": False}
    )
    bad_pm = _seed_post_mortem(
        sid_bad, verdict="pass", scout_summary={"recommended_skills": ["bad-skill"], "from_fallback": False}
    )

    original_attribute = synthesis.attribute

    def selective_boom(row):
        if row["id"] == bad_pm:
            raise RuntimeError("simulated attribution failure")
        return original_attribute(row)

    monkeypatch.setattr(synthesis, "attribute", selective_boom)
    stats = synthesis.run()

    # Good row processed; bad row left unsynthesized for retry
    assert db.get_post_mortem(good_pm)["synthesized_at"] is not None
    assert db.get_post_mortem(bad_pm)["synthesized_at"] is None
    # Good skill signal present
    assert db.get_signal("skill", "good-skill") is not None
    # stats.processed counts the good row
    assert stats.processed >= 1


def test_attributions_payload_json_is_parseable_via_from_row():
    sid = db.create_session(title="payload-parse")
    db.delete_signal("skill", "payload-check")
    _seed_post_mortem(
        sid, verdict="pass", scout_summary={"recommended_skills": ["payload-check"], "from_fallback": False}
    )
    synthesis.run()
    row = db.get_signal("skill", "payload-check")
    sig = from_row(row)
    assert sig.subject == "payload-check"
    assert sig.successes == 1


# ---------------------------------------------------------------------------
# Adaptive-entry usefulness attribution (v3.1)
# ---------------------------------------------------------------------------


def test_used_hints_pass_credits_without_recounting_usage():
    """Usage was counted at scout submit-time — synthesis adds the OUTCOME
    only (delta_reinforcements=0), or the denominator double-counts."""
    row = _pm(scout_summary={"used_hints": ["yt-dlp-403-captions-fallback"], "from_fallback": False})
    attrs = [a for a in synthesis.attribute(row) if a.signal_type == "adaptive_entry"]
    assert len(attrs) == 1
    a = attrs[0]
    assert a.subject == "yt-dlp-403-captions-fallback"
    assert a.delta_successes == 1 and a.delta_failures == 0 and a.delta_reinforcements == 0


def test_used_hints_retry_blamed_on_scout_penalizes():
    row = _pm(
        verdict="retry",
        failure_cause="scout",
        scout_summary={"used_hints": ["bad-hint"], "from_fallback": False},
    )
    attrs = [a for a in synthesis.attribute(row) if a.signal_type == "adaptive_entry"]
    assert len(attrs) == 1 and attrs[0].delta_failures == 1 and attrs[0].delta_reinforcements == 0


def test_used_hints_retry_blamed_elsewhere_is_skipped():
    """`env`/`task`/`skill` causes are not the hint's doing — no failure.

    (`agent` and `scout` ARE charged now; see test_attribution_hardening.py.)
    """
    row = _pm(
        verdict="retry",
        failure_cause="env",
        scout_summary={"used_hints": ["innocent-hint"], "from_fallback": False},
    )
    assert [a for a in synthesis.attribute(row) if a.signal_type == "adaptive_entry"] == []


def test_cited_policies_count_usage_and_outcome():
    """A reflect citation is usage+outcome in one observation: the use is
    always booked, and the verdict lands as a success or — when the failure
    is one the policy could have caused — as a failure (2026-09-04, W1)."""
    payload = {"scout_summary": {"from_fallback": False}, "cited_policies": ["verify-on-disk-before-completion"]}
    row = {
        "id": "pmY",
        "verdict": "pass",
        "failure_cause": "none",
        "confidence": 0.9,
        "execution_mode": "inline",
        "scout_viability": "verified",
        "payload_json": json.dumps(payload),
    }
    attrs = [a for a in synthesis.attribute(row) if a.signal_type == "adaptive_entry"]
    assert len(attrs) == 1
    assert attrs[0].delta_successes == 1 and attrs[0].delta_reinforcements == 1

    # A cause the policy could have caused: charged, and the use still books.
    row["verdict"] = "retry"
    row["failure_cause"] = "agent"
    attrs = [a for a in synthesis.attribute(row) if a.signal_type == "adaptive_entry"]
    assert len(attrs) == 1
    assert attrs[0].delta_successes == 0 and attrs[0].delta_failures == 1 and attrs[0].delta_reinforcements == 1

    # A cause it could not have caused: use booked, no verdict either way.
    row["failure_cause"] = "env"
    attrs = [a for a in synthesis.attribute(row) if a.signal_type == "adaptive_entry"]
    assert len(attrs) == 1
    assert attrs[0].delta_successes == 0 and attrs[0].delta_failures == 0 and attrs[0].delta_reinforcements == 1


def test_adaptive_entry_signal_upserts_with_zero_reinforcement_delta():
    db.delete_signal("adaptive_entry", "zero-delta-check")
    db.upsert_signal("adaptive_entry", "zero-delta-check")  # submit-time usage
    db.upsert_signal("adaptive_entry", "zero-delta-check", delta_successes=1, delta_reinforcements=0)
    row = db.get_signal("adaptive_entry", "zero-delta-check")
    assert row["reinforcements"] == 1 and row["successes"] == 1
