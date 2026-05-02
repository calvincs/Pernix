"""Phase 2b compact reflect evidence: default path drops transcript.

Verifies reflect_full_transcript=False (default) produces an evidence blob
with user ask + final assistant message + tool summary + workspace files,
and that it does NOT include mid-conversation tool results.
"""

from unittest.mock import patch

from core.reflect import _build_evidence
from core.scout.report import DeliverableSpec, ScoutReport
from db import models as db


def _make_session_with_transcript() -> str:
    sid = db.create_session(title="Compact Evidence Test")
    db.add_message(sid, "user", "Write a file named report.md with a summary.")
    db.add_message(sid, "assistant", "I'll start by reading the input.")
    db.add_message(sid, "tool", "FILE CONTENT: some middle-of-turn tool output that should NOT appear")
    db.add_message(sid, "assistant", "Now I'll write the summary.")
    db.add_message(sid, "tool", "Wrote report.md successfully.")
    db.add_message(sid, "assistant", "Done — report.md has been written.")
    return sid


def test_compact_evidence_default_omits_transcript():
    sid = _make_session_with_transcript()
    user_req, evidence = _build_evidence(sid, attempt=1)
    assert user_req == "Write a file named report.md with a summary."
    # Final assistant message present.
    assert "Done — report.md has been written." in evidence
    # Mid-transcript tool output should NOT leak into compact evidence.
    assert "middle-of-turn tool output" not in evidence
    # No transcript header.
    assert "FULL CONVERSATION TRANSCRIPT" not in evidence


def test_compact_evidence_includes_tool_summary():
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


def test_compact_evidence_includes_deliverables_when_scout_provides():
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


def test_compact_evidence_handles_missing_scout_report():
    sid = _make_session_with_transcript()
    _, evidence = _build_evidence(sid, attempt=1)  # scout_report=None
    assert "SCOUT DELIVERABLES PLAN" not in evidence  # no section when absent


def test_full_transcript_path_opt_in():
    sid = _make_session_with_transcript()
    with patch("core.reflect.settings.reflect_full_transcript", True):
        _, evidence = _build_evidence(sid, attempt=1)
    assert "FULL CONVERSATION TRANSCRIPT" in evidence
    assert "middle-of-turn tool output" in evidence


def test_compact_evidence_includes_workspace_files_header():
    sid = _make_session_with_transcript()
    _, evidence = _build_evidence(sid, attempt=1)
    assert "WORKSPACE FILES:" in evidence


def test_compact_evidence_includes_retry_preamble_on_attempt_gt_1():
    sid = _make_session_with_transcript()
    _, evidence = _build_evidence(sid, attempt=2)
    assert "attempt #2" in evidence


def test_compact_evidence_includes_workflow_runs_for_session():
    """Regression for session 7b97cf7ef84a: when the agent invokes
    run_workflow, reflect's evidence must include the workflow_runs row's
    authoritative status so the LLM doesn't conflate intermediate scratch
    files with a 'pass'.
    """
    sid = db.create_session(title="Workflow reflect test")
    db.add_message(sid, "user", "execute scheduled job ai-tech-daily-brief")
    # The run_workflow tool's response embeds the run_id in its summary.
    db.add_message(sid, "tool", "Workflow 'ai-tech-daily-brief' run abc12345: running. " "1/4 complete, 3 pending.")
    db.add_message(sid, "assistant", "The workflow is running, I'll wait for it.")

    # Persist a workflow_runs row that's still 'running' — emulates an
    # orphaned/in-flight run during the reflect window.
    db.create_workflow_run(run_id="abc12345", workflow_name="ai-tech-daily-brief", run_dir="x/abc12345", step_count=4)

    _, evidence = _build_evidence(sid, attempt=1)
    assert "WORKFLOW RUNS:" in evidence, evidence
    assert "abc12345" in evidence
    assert "status=running" in evidence, "reflect must see the authoritative workflow status, not just " "scratch files"
