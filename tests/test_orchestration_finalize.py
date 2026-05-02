"""Tests for _finalize_step in core/extensions/orchestration.

Covers the verdict-resolution contract: how reflect verdict + output-file
existence + worker termination state combine to produce the step's terminal
manifest entry. The default rule is strict (no verdict + no output → failed),
but a missing/unknown verdict with output present should NOT block downstream —
it should mark complete with `unknown-but-complete` so the engine treats the
deliverable as the source of truth.
"""

from __future__ import annotations

import json as _json
from dataclasses import dataclass

import pytest

from core.extensions.orchestration import _finalize_step
from sessions.manager import SessionManager


@dataclass
class _FakeStep:
    """Minimal stand-in for a parsed workflow step. Only the attributes
    `_finalize_step` actually reads."""

    id: str
    output_file: str | None = "out.json"


@pytest.fixture
def mgr(monkeypatch):
    """Fresh manager singleton — same pattern as test_orchestration."""
    fresh = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", fresh)
    return fresh


def _make_worker(
    mgr: SessionManager,
    *,
    reflect_verdict: str | None = None,
    termination_reason: str | None = None,
    reasoning: str = "",
) -> str:
    """Spawn a worker session row with optional reflect + termination state."""
    from db import models as db

    parent_sid = mgr.create_session(title="P")
    wid = mgr.create_session(
        title="W",
        session_type="worker",
        parent_session_id=parent_sid,
    )
    db.add_message(wid, "user", "go")
    db.add_message(wid, "assistant", "did some work")
    if reflect_verdict is not None:
        db.add_message(
            wid,
            "reflect",
            _json.dumps(
                {
                    "verdict": reflect_verdict,
                    "reasoning": reasoning or f"test:{reflect_verdict}",
                }
            ),
        )
    w = mgr.get(wid)
    w.termination_reason = termination_reason
    return wid


def _build_manifest(step_id: str = "step-1") -> dict:
    return {
        "steps": {
            step_id: {
                "id": step_id,
                "status": "running",
                "attempts": 1,
            }
        }
    }


def _touch_output(run_dir, name: str = "out.json"):
    p = run_dir / name
    p.write_text("{}")
    return p


# ---------------------------------------------------------------------------
# Existing contract: pass + output exists → complete
# ---------------------------------------------------------------------------


def test_pass_with_output_marks_complete(mgr, tmp_path):
    wid = _make_worker(mgr, reflect_verdict="pass")
    step = _FakeStep(id="step-1")
    manifest = _build_manifest()
    _touch_output(tmp_path)

    outcome = _finalize_step(step, wid, manifest, tmp_path)

    assert outcome == "complete"
    assert manifest["steps"]["step-1"]["status"] == "complete"
    assert manifest["steps"]["step-1"]["reflect_verdict"] == "pass"


def test_pass_without_output_flips_to_failed(mgr, tmp_path):
    """Confabulated pass — verdict says pass but no file. Strict rule preserved."""
    wid = _make_worker(mgr, reflect_verdict="pass")
    step = _FakeStep(id="step-1")
    manifest = _build_manifest()

    outcome = _finalize_step(step, wid, manifest, tmp_path)

    assert outcome == "failed"
    assert manifest["steps"]["step-1"]["status"] == "failed"
    assert manifest["steps"]["step-1"]["failure_reason"] == "pass-but-no-output"


# ---------------------------------------------------------------------------
# Existing cancelled-but-complete preserved
# ---------------------------------------------------------------------------


def test_cancelled_with_output_preserves_cancelled_but_complete(mgr, tmp_path, monkeypatch):
    """Cancelled worker with output should remain 'cancelled-but-complete'.
    Recovery is gated to disabled here so we test the preserved behavior."""
    wid = _make_worker(mgr, reflect_verdict=None, termination_reason="cancelled")
    step = _FakeStep(id="step-1")
    manifest = _build_manifest()
    _touch_output(tmp_path)

    # Stub recovery to "no result" so we exercise the output-fallback branch.
    monkeypatch.setattr(
        "core.extensions.orchestration._recover_reflect_verdict",
        lambda wid_, ctx_: None,
    )

    outcome = _finalize_step(step, wid, manifest, tmp_path)

    assert outcome == "complete"
    assert manifest["steps"]["step-1"]["reflect_verdict"] == "cancelled-but-complete"


def test_cancelled_without_output_marks_failed_cancelled(mgr, tmp_path, monkeypatch):
    wid = _make_worker(mgr, reflect_verdict=None, termination_reason="cancelled")
    step = _FakeStep(id="step-1")
    manifest = _build_manifest()
    monkeypatch.setattr(
        "core.extensions.orchestration._recover_reflect_verdict",
        lambda wid_, ctx_: None,
    )

    outcome = _finalize_step(step, wid, manifest, tmp_path)

    assert outcome == "failed"
    assert manifest["steps"]["step-1"]["status"] == "cancelled"
    assert manifest["steps"]["step-1"]["failure_reason"] == "cancelled"


# ---------------------------------------------------------------------------
# NEW: unknown verdict + output exists → unknown-but-complete (downstream unblocks)
# ---------------------------------------------------------------------------


def test_unknown_with_output_marks_unknown_but_complete(mgr, tmp_path, monkeypatch):
    """The bug from run 024c370f: worker shipped its deliverable but post-hook
    reflect was lost. Engine should trust the file rather than fail downstream."""
    wid = _make_worker(mgr, reflect_verdict=None, termination_reason=None)
    step = _FakeStep(id="step-1")
    manifest = _build_manifest()
    _touch_output(tmp_path)
    monkeypatch.setattr(
        "core.extensions.orchestration._recover_reflect_verdict",
        lambda wid_, ctx_: None,
    )

    outcome = _finalize_step(step, wid, manifest, tmp_path)

    assert outcome == "complete"
    assert manifest["steps"]["step-1"]["status"] == "complete"
    assert manifest["steps"]["step-1"]["reflect_verdict"] == "unknown-but-complete"


def test_unknown_without_output_marks_failed_no_verdict_no_output(mgr, tmp_path, monkeypatch):
    """No verdict AND no output IS a real failure — caller should propagate."""
    wid = _make_worker(mgr, reflect_verdict=None, termination_reason=None)
    step = _FakeStep(id="step-1")
    manifest = _build_manifest()
    monkeypatch.setattr(
        "core.extensions.orchestration._recover_reflect_verdict",
        lambda wid_, ctx_: None,
    )

    outcome = _finalize_step(step, wid, manifest, tmp_path)

    assert outcome == "failed"
    assert manifest["steps"]["step-1"]["failure_reason"] == "no-verdict-no-output"


# ---------------------------------------------------------------------------
# NEW: error sentinel (reflect crashed) — same fallback, different label
# ---------------------------------------------------------------------------


def test_error_verdict_with_output_marks_error_but_complete(mgr, tmp_path, monkeypatch):
    """If hooks.py wrote the 'error' sentinel because reflect crashed,
    don't re-run reflect — trust the file and label the manifest accordingly."""
    wid = _make_worker(mgr, reflect_verdict="error", reasoning="reflect crashed: TimeoutError")
    step = _FakeStep(id="step-1")
    manifest = _build_manifest()
    _touch_output(tmp_path)

    # Recovery should NOT be called for 'error' — assert via a sentinel that
    # would force a test failure if invoked.
    def _boom(*a, **kw):
        pytest.fail("recovery should not run for verdict='error'")

    monkeypatch.setattr(
        "core.extensions.orchestration._recover_reflect_verdict",
        _boom,
    )

    outcome = _finalize_step(step, wid, manifest, tmp_path)

    assert outcome == "complete"
    assert manifest["steps"]["step-1"]["reflect_verdict"] == "error-but-complete"


def test_error_verdict_without_output_marks_failed(mgr, tmp_path):
    wid = _make_worker(mgr, reflect_verdict="error", reasoning="reflect crashed: TimeoutError")
    step = _FakeStep(id="step-1")
    manifest = _build_manifest()

    outcome = _finalize_step(step, wid, manifest, tmp_path)

    assert outcome == "failed"
    assert manifest["steps"]["step-1"]["failure_reason"] == "reflect-error-no-output"


# ---------------------------------------------------------------------------
# NEW: recovery succeeds → uses the recovered verdict (treated as authoritative)
# ---------------------------------------------------------------------------


def test_recovery_succeeds_with_pass_treats_as_complete(mgr, tmp_path, monkeypatch):
    """When _latest_reflect returns nothing but recovery yields a real verdict,
    use that verdict and run the normal path. Output file must exist for pass."""
    wid = _make_worker(mgr, reflect_verdict=None, termination_reason=None)
    step = _FakeStep(id="step-1")
    manifest = _build_manifest()
    _touch_output(tmp_path)

    # Recovery returns a valid pass verdict. The helper writes the row to DB
    # in production; for the test we just hand back the dict.
    monkeypatch.setattr(
        "core.extensions.orchestration._recover_reflect_verdict",
        lambda wid_, ctx_: {
            "verdict": "pass",
            "reasoning": "recovered: looks fine",
            "_recovered": True,
        },
    )

    outcome = _finalize_step(step, wid, manifest, tmp_path)

    assert outcome == "complete"
    assert manifest["steps"]["step-1"]["reflect_verdict"] == "pass"
    assert manifest["steps"]["step-1"]["reflect_recovered"] is True


def test_recovery_succeeds_with_retry_marks_failed_for_caller(mgr, tmp_path, monkeypatch):
    """If recovery yields verdict=retry, _finalize_step returns 'failed' so
    the caller's retry loop kicks in (it checks reflect_verdict=='retry')."""
    wid = _make_worker(mgr, reflect_verdict=None)
    step = _FakeStep(id="step-1")
    manifest = _build_manifest()
    monkeypatch.setattr(
        "core.extensions.orchestration._recover_reflect_verdict",
        lambda wid_, ctx_: {"verdict": "retry", "reasoning": "missed step 2"},
    )

    outcome = _finalize_step(step, wid, manifest, tmp_path)

    assert outcome == "failed"
    assert manifest["steps"]["step-1"]["reflect_verdict"] == "retry"
    assert manifest["steps"]["step-1"]["failure_reason"] == "retry"


def test_recovery_succeeds_with_escalate_marks_escalated(mgr, tmp_path, monkeypatch):
    wid = _make_worker(mgr, reflect_verdict=None)
    step = _FakeStep(id="step-1")
    manifest = _build_manifest()
    monkeypatch.setattr(
        "core.extensions.orchestration._recover_reflect_verdict",
        lambda wid_, ctx_: {"verdict": "escalate", "reasoning": "blocked on auth"},
    )

    outcome = _finalize_step(step, wid, manifest, tmp_path)

    assert outcome == "escalated"
    assert manifest["steps"]["step-1"]["status"] == "escalated"


# ---------------------------------------------------------------------------
# Retry verdict + output-file evidence override (#3 from 2026-04-27 audit)
# ---------------------------------------------------------------------------


def test_retry_verdict_overridden_by_substantial_output(mgr, tmp_path):
    """When reflect says retry but the worker shipped a non-trivial output
    file, trust the file. This was the web-news failure mode in workflow run
    1ec11d2b — reflect read the agent's "still fetching" prose and asked for
    a retry that re-did 3 minutes of work for no benefit."""
    wid = _make_worker(
        mgr,
        reflect_verdict="retry",
        reasoning="agent's final message says still fetching",
    )
    step = _FakeStep(id="step-1")
    manifest = _build_manifest()
    # Substantive output (>100 bytes) — what a real worker would produce
    out = tmp_path / "out.json"
    out.write_text(
        '[{"title": "Real result with content", "url": "https://example.com/a"},'
        ' {"title": "Second result here", "url": "https://example.com/b"}]'
    )
    assert out.stat().st_size > 100, "test fixture should be over threshold"

    outcome = _finalize_step(step, wid, manifest, tmp_path)

    assert outcome == "complete", (
        "verdict=retry with substantial output should be honored as complete — " "retrying discards real work"
    )
    assert manifest["steps"]["step-1"]["status"] == "complete"
    assert manifest["steps"]["step-1"]["reflect_verdict"] == "retry-overridden-by-file-evidence"
    assert manifest["steps"]["step-1"]["reflect_verdict_original"] == "retry"


def test_retry_verdict_kept_when_output_missing(mgr, tmp_path):
    """When reflect says retry AND there's no output file, trust the retry —
    the override only applies when there's evidence the work was actually
    shipped. Missing file means the retry verdict is correct."""
    wid = _make_worker(mgr, reflect_verdict="retry")
    step = _FakeStep(id="step-1")
    manifest = _build_manifest()
    # No output file written

    outcome = _finalize_step(step, wid, manifest, tmp_path)

    assert outcome == "failed"
    assert manifest["steps"]["step-1"]["reflect_verdict"] == "retry"


def test_pass_with_missing_output_recovers_from_worker_write_at_other_path(mgr, tmp_path, monkeypatch):
    """Regression for workflow run 25920bcb (2026-04-27 ai-tech-daily-brief
    run #4): the synthesize worker wrote ai_tech_brief.md to the archive
    path (workspace/ai_tech_brief/{date}/...) but not to the run-dir gate
    location. Reflect verdict=pass but output_file missing → wave failed
    even though the brief was actually produced.

    Recovery: scan the worker's tool messages for file_write calls; if any
    wrote a file with the same basename as the expected output_file, copy
    it to the gate location and accept the step.
    """
    from db import models as db

    wid = _make_worker(mgr, reflect_verdict="pass")
    # Simulate a tool message recording the file_write to a different path.
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    real_path = archive_dir / "out.json"
    real_path.write_text(
        '{"summary": "real deliverable with substantive content here, well over the 100-byte threshold",'
        ' "padding": "more content to ensure we are over the 100 byte size floor for recovery"}'
    )
    assert real_path.stat().st_size > 100, "fixture must be over recovery threshold"
    db.add_message(wid, "tool", f"file_write completed: wrote 200 bytes to {real_path}")

    step = _FakeStep(id="step-1", output_file="out.json")
    manifest = _build_manifest()
    # Note: we do NOT _touch_output(tmp_path) — the gate file is missing.

    outcome = _finalize_step(step, wid, manifest, tmp_path)

    assert outcome == "complete", (
        "synthesize-style worker that wrote to archive but not to gate "
        "should still be honored as complete via recovery copy"
    )
    # The file should now exist at the gate location.
    assert (tmp_path / "out.json").exists()
    assert manifest["steps"]["step-1"]["status"] == "complete"
    assert manifest["steps"]["step-1"]["reflect_verdict"] == "pass-after-recovery-copy"


def test_pass_with_missing_output_no_recovery_when_no_alt_path(mgr, tmp_path):
    """If the worker truly didn't write the file anywhere, the recovery
    can't find it and we must still fail the step (preserves the
    confabulation guard)."""
    wid = _make_worker(mgr, reflect_verdict="pass")
    # No file_write tool message recorded.
    step = _FakeStep(id="step-1", output_file="out.json")
    manifest = _build_manifest()

    outcome = _finalize_step(step, wid, manifest, tmp_path)

    assert outcome == "failed"
    assert manifest["steps"]["step-1"]["failure_reason"] == "pass-but-no-output"


def test_retry_verdict_kept_when_output_is_trivial(mgr, tmp_path):
    """A 2-byte `[]` JSON or stub doesn't count as evidence the work shipped.
    Threshold is 100 bytes — anything below is treated as the agent
    confabulating completion when the verdict says otherwise."""
    wid = _make_worker(mgr, reflect_verdict="retry")
    step = _FakeStep(id="step-1")
    manifest = _build_manifest()
    out = tmp_path / "out.json"
    out.write_text("[]")  # 2 bytes — definitely trivial

    outcome = _finalize_step(step, wid, manifest, tmp_path)

    assert outcome == "failed", (
        "verdict=retry with stub output should still fail — empty/stub "
        "output isn't evidence the work was actually done"
    )
    assert manifest["steps"]["step-1"]["reflect_verdict"] == "retry"
