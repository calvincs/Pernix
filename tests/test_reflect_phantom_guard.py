"""Phantom-evidence guard (2026-09-15): a non-pass verdict built on evidence the
grader was never shown must not retry the turn or page the user."""

from core.reflect import ReflectResult, phantom_evidence_reason

_EVIDENCE = (
    "TERMINATION HISTORY (newest first): complete, complete\n\nUSER REQUEST:\nwhat is the TLDR here?\n\n"
    "ATTEMPT TRANSCRIPT (current attempt only)\n[ASSISTANT]\nTLRD, grounded in the files I just read: ...\n\n"
    "AGENT FINAL RESPONSE:\nTLRD, grounded in the files I just read: ..."
)


def test_invented_402_compaction_failure_on_complete_turn_is_phantom():
    r = ReflectResult(
        verdict="escalate",
        failure_cause="env",
        confidence=0.9,
        reasoning=(
            "The agent could not produce any response for this turn because the session's compaction "
            "failed with a hard error ('Compaction failed: 402 payment_required')."
        ),
        missing="Replenish the API quota.",
    )
    why = phantom_evidence_reason(r, _EVIDENCE, "complete")
    assert why and "environment failure" in why


def test_real_compaction_failure_in_evidence_is_not_phantom():
    r = ReflectResult(verdict="escalate", failure_cause="env", reasoning="compaction failed with 402 payment_required")
    ev = _EVIDENCE + "\n[SYSTEM]\nCompaction failed: 402 payment_required"
    assert phantom_evidence_reason(r, ev, "complete") is None


def test_env_claim_on_non_complete_turn_is_left_alone():
    r = ReflectResult(verdict="escalate", failure_cause="env", reasoning="compaction failed, quota exhausted")
    assert phantom_evidence_reason(r, _EVIDENCE, "compaction_failed") is None


def test_no_output_claim_with_final_response_is_phantom():
    r = ReflectResult(verdict="escalate", failure_cause="env", reasoning="The transcript has no agent output.")
    assert phantom_evidence_reason(r, _EVIDENCE, "complete")


def test_no_output_claim_is_honoured_when_final_is_missing():
    r = ReflectResult(verdict="escalate", failure_cause="env", reasoning="The transcript has no agent output.")
    ev = _EVIDENCE.replace(
        "AGENT FINAL RESPONSE:\nTLRD, grounded in the files I just read: ...",
        "AGENT FINAL RESPONSE:\n(no final assistant message)",
    )
    assert phantom_evidence_reason(r, ev, "complete") is None


def test_quoted_next_user_message_without_section_is_phantom():
    r = ReflectResult(
        verdict="retry",
        failure_cause="agent",
        reasoning="However, the user's own next message reports that scratch was not updated ('weren't updated').",
    )
    assert phantom_evidence_reason(r, _EVIDENCE, "complete")


def test_next_user_message_claim_with_section_is_honoured():
    r = ReflectResult(
        verdict="retry", failure_cause="agent", reasoning="The user's next message says 'no, do it again'."
    )
    ev = _EVIDENCE + "\n\nUSER'S NEXT MESSAGE (arrived after this turn):\nno, do it again"
    assert phantom_evidence_reason(r, ev, "complete") is None


def test_ordinary_env_retry_is_not_touched():
    # A tool-level env failure that the evidence actually shows (http 403) is a legitimate env verdict.
    r = ReflectResult(verdict="retry", failure_cause="env", reasoning="http_get returned 403 so the report is missing")
    ev = _EVIDENCE + "\n[TOOL http_get]\nHTTP 403 Forbidden"
    assert phantom_evidence_reason(r, ev, "complete") is None


def test_pass_verdict_is_never_flagged():
    r = ReflectResult(verdict="pass", reasoning="compaction 402 quota — none of this matters on a pass")
    assert phantom_evidence_reason(r, _EVIDENCE, "complete") is None
