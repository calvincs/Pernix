"""Reflect evidence builder tests.

The evidence builder used to drop the mid-conversation transcript by default
(reflect_full_transcript=False). After the turn-digest redesign, reflect
*always* sees the per-attempt transcript with verbatim tool result bodies —
sliced from the most recent ``scout`` role marker so retries don't drown the
verifier in stale messages from prior attempts.

These tests verify:
- the preamble (workspace files, tool summary, deliverables)
- the new ATTEMPT TRANSCRIPT section's content and scoping
- per-tool-result truncation behavior
- backwards compatibility for the deprecated ``reflect_full_transcript`` flag
"""

from unittest.mock import patch

from core.reflect import _build_evidence, _messages_since_attempt_start
from core.scout.report import DeliverableSpec, ScoutReport
from db import models as db


def _make_session_with_transcript() -> str:
    sid = db.create_session(title="Compact Evidence Test")
    db.add_message(sid, "user", "Write a file named report.md with a summary.")
    db.add_message(sid, "assistant", "I'll start by reading the input.")
    db.add_message(sid, "tool", "FILE CONTENT: some middle-of-turn tool output")
    db.add_message(sid, "assistant", "Now I'll write the summary.")
    db.add_message(sid, "tool", "Wrote report.md successfully.")
    db.add_message(sid, "assistant", "Done — report.md has been written.")
    return sid


def test_evidence_includes_attempt_transcript_with_tool_results():
    """Reflect now sees verbatim tool result bodies — the change that lets it
    verify claims against what tools actually returned, instead of grading
    against its training-data priors. Regression target: session
    f7885259462e (war.gov UFO disclosure case)."""
    sid = _make_session_with_transcript()
    user_req, evidence = _build_evidence(sid, attempt=1)
    assert user_req == "Write a file named report.md with a summary."
    assert "Done — report.md has been written." in evidence
    assert "ATTEMPT TRANSCRIPT" in evidence
    # Verbatim tool result bodies must appear — this is the whole point.
    assert "middle-of-turn tool output" in evidence
    assert "Wrote report.md successfully." in evidence


def test_evidence_includes_tool_summary():
    sid = _make_session_with_transcript()
    tool_summary = {
        "file_write": {
            "calls": 1,
            "failures": 0,
            "total_latency_ms": 45,
            "errors": [],
        },
        "file_read": {
            "calls": 2,
            "failures": 1,
            "total_latency_ms": 10,
            "errors": ["file not found: missing.txt"],
        },
    }
    _, evidence = _build_evidence(sid, attempt=1, tool_summary=tool_summary)
    assert "TOOL EXECUTION SUMMARY" in evidence
    assert "file_write" in evidence
    assert "file_read" in evidence
    assert "file not found: missing.txt" in evidence


def test_evidence_includes_deliverables_when_scout_provides():
    sid = _make_session_with_transcript()
    scout = ScoutReport(
        approach_guidance="steps",
        deliverables_plan=[
            DeliverableSpec(description="Write report.md", execution_hint="inline"),
            DeliverableSpec(description="Run linter on it", execution_hint="inline"),
        ],
    )
    _, evidence = _build_evidence(sid, attempt=1, scout_report=scout)
    assert "SCOUT DELIVERABLES PLAN" in evidence
    assert "Write report.md" in evidence
    assert "Run linter on it" in evidence


def test_evidence_emits_turn_scope_for_ask_user_answer():
    """When the user's message is an ask_user answer, evidence must include
    a TURN SCOPE block telling reflect to grade narrowly. Regression target:
    session 2098d1b26d31 — reflect graded a 'yes, approve the skill update'
    turn against the session's broader 'create a LinkedIn post' goal and
    looped on retries the user never asked for."""
    sid = db.create_session(title="Create LinkedIn Post")
    db.add_message(
        sid,
        "user",
        "[User answered your question]\nQ: Approve skill update?\nA: yes",
    )
    db.add_message(sid, "assistant", "Approved. Skill updated.")
    scout = ScoutReport(
        approach_guidance="Draft a LinkedIn post from the source file.",
        deliverables_plan=[
            DeliverableSpec(description="Read source file", execution_hint="file_read"),
            DeliverableSpec(description="Write post to workspace", execution_hint="file_write"),
        ],
    )
    _, evidence = _build_evidence(sid, attempt=1, scout_report=scout)
    assert "TURN SCOPE: ask_user answer." in evidence
    # Scout's stale plan still appears, but TURN SCOPE precedes it in the parts
    # list so the prompt rule can flag it as out of scope.
    assert evidence.index("TURN SCOPE:") < evidence.index("SCOUT DELIVERABLES PLAN")


def test_evidence_omits_turn_scope_for_normal_user_message():
    sid = _make_session_with_transcript()
    _, evidence = _build_evidence(sid, attempt=1)
    assert "TURN SCOPE:" not in evidence


def test_evidence_handles_missing_scout_report():
    sid = _make_session_with_transcript()
    _, evidence = _build_evidence(sid, attempt=1)  # scout_report=None
    assert "SCOUT DELIVERABLES PLAN" not in evidence  # no section when absent


def test_full_transcript_setting_is_now_a_noop():
    """The legacy reflect_full_transcript flag is deprecated. Reflect always
    sees the per-attempt transcript regardless of the setting; toggling has
    no effect on the resulting evidence text."""
    sid = _make_session_with_transcript()
    with patch("core.reflect.settings.reflect_full_transcript", True):
        _, evidence_on = _build_evidence(sid, attempt=1)
    with patch("core.reflect.settings.reflect_full_transcript", False):
        _, evidence_off = _build_evidence(sid, attempt=1)
    assert evidence_on == evidence_off
    # Both contain the new transcript header (regardless of flag).
    assert "ATTEMPT TRANSCRIPT" in evidence_on


def test_evidence_includes_workspace_files_header():
    sid = _make_session_with_transcript()
    _, evidence = _build_evidence(sid, attempt=1)
    assert "WORKSPACE FILES:" in evidence


def test_evidence_includes_retry_preamble_on_attempt_gt_1():
    sid = _make_session_with_transcript()
    _, evidence = _build_evidence(sid, attempt=2)
    assert "attempt #2" in evidence


def test_messages_since_attempt_start_with_scout_marker():
    """When a scout-role message exists, slice from the latest one forward.
    Earlier attempts (before the latest scout) are excluded — they are
    carried forward to the next scout via the prior turn_digest, not
    re-shown to reflect."""
    msgs = [
        {"id": 1, "role": "user", "content": "ask"},
        {"id": 2, "role": "scout", "content": "scout-1 marker"},
        {"id": 3, "role": "assistant", "content": "attempt-1 work"},
        {"id": 4, "role": "tool", "content": "attempt-1 result"},
        {"id": 5, "role": "scout", "content": "scout-2 marker"},
        {"id": 6, "role": "assistant", "content": "attempt-2 work"},
    ]
    sliced = _messages_since_attempt_start(msgs)
    assert [m["id"] for m in sliced] == [5, 6]


def test_messages_since_attempt_start_falls_back_to_user_msg_id():
    """No scout marker → slice from turn_user_msg_id forward."""
    msgs = [
        {"id": 1, "role": "user", "content": "old ask"},
        {"id": 2, "role": "assistant", "content": "old reply"},
        {"id": 3, "role": "user", "content": "current ask"},
        {"id": 4, "role": "assistant", "content": "current reply"},
    ]
    sliced = _messages_since_attempt_start(msgs, turn_user_msg_id=3)
    assert [m["id"] for m in sliced] == [3, 4]


def test_messages_since_attempt_start_returns_all_when_no_markers():
    msgs = [
        {"id": 1, "role": "user", "content": "ask"},
        {"id": 2, "role": "assistant", "content": "reply"},
    ]
    sliced = _messages_since_attempt_start(msgs)
    assert [m["id"] for m in sliced] == [1, 2]


def test_evidence_scopes_to_current_attempt_via_scout_marker():
    """When the messages table has multiple scout markers (one per attempt),
    reflect's evidence must contain only the latest attempt's transcript.
    Prior attempts' tool results stay out so reflect grades the freshest
    work, not the accumulated history."""
    sid = db.create_session(title="Multi-attempt scoping test")
    db.add_message(sid, "user", "do the thing")
    db.add_message(sid, "scout", '{"type":"scout.done","attempt":1}')
    db.add_message(sid, "assistant", "ATTEMPT_ONE_WORK_MARKER")
    db.add_message(sid, "tool", "ATTEMPT_ONE_TOOL_OUTPUT")
    db.add_message(sid, "assistant", "ATTEMPT_ONE_FINAL")
    # Reflect-1 verdict=retry would land here (real flow), then scout-2 runs.
    db.add_message(sid, "scout", '{"type":"scout.done","attempt":2}')
    db.add_message(sid, "assistant", "ATTEMPT_TWO_WORK_MARKER")
    db.add_message(sid, "tool", "ATTEMPT_TWO_TOOL_OUTPUT")
    db.add_message(sid, "assistant", "ATTEMPT_TWO_FINAL")

    _, evidence = _build_evidence(sid, attempt=2)

    # Attempt-2 content present, attempt-1 content elided from transcript.
    assert "ATTEMPT_TWO_WORK_MARKER" in evidence
    assert "ATTEMPT_TWO_TOOL_OUTPUT" in evidence
    assert "ATTEMPT_TWO_FINAL" in evidence
    assert "ATTEMPT_ONE_WORK_MARKER" not in evidence
    assert "ATTEMPT_ONE_TOOL_OUTPUT" not in evidence


def test_evidence_truncates_long_tool_results_with_marker():
    """A single multi-megabyte tool result can't be allowed to dominate the
    transcript budget. Per-tool char cap with a [+N chars truncated] marker."""
    sid = db.create_session(title="Long-result Trunc Test")
    db.add_message(sid, "user", "fetch the giant file")
    big_body = "X" * 50_000  # well above _PER_TOOL_RESULT_CHAR_CAP (5000)
    db.add_message(sid, "tool", big_body)
    db.add_message(sid, "assistant", "Got it")

    _, evidence = _build_evidence(sid, attempt=1)
    assert "ATTEMPT TRANSCRIPT" in evidence
    assert "[+" in evidence and "chars truncated]" in evidence
    # The full 50k body must not be inlined verbatim.
    assert big_body not in evidence
