"""Tests for core/adaptive/lint.py — the actionability floor under every
machine producer (v3.1). The accept/reject matrix pins the exact content
classes the 2026-08-27 audit found saturating the store."""

from __future__ import annotations

from core.adaptive.lint import lint_edit


def _edit(content: str, kind: str = "policy", action: str = "create") -> dict:
    return {"action": action, "kind": kind, "title": "t", "content": content}


class TestRejects:
    def test_narrative_complaint_openers(self):
        for c in (
            "Despite high-confidence verifications, the agent repeatedly fails to adhere to constraints.",
            "Even though lessons exist, delivery is unchanged.",
            "Although M7 warns about stalling, workers still stall.",
        ):
            assert lint_edit(_edit(c)) is not None, c

    def test_narrative_complaint_phrases(self):
        for c in (
            "Stored lessons regarding worker stalling appear ineffective or insufficiently robust.",
            "The agent repeatedly fails to produce the TL;DR section.",
            "The stored pattern continues to fail in new sessions.",
        ):
            assert lint_edit(_edit(c)) is not None, c

    def test_negative_tool_claim_without_fix(self):
        assert lint_edit(_edit("The browser tools do not work.", kind="routing_hint")) is not None
        assert lint_edit(_edit("rlm_process is broken and unreliable.", kind="routing_hint")) is not None

    def test_diagnostic_prose_with_no_directive(self):
        c = "Supported hypothesis (c_0003, confidence 0.65): The update_memory tool acts like a restricted item on a cargo manifest."
        assert lint_edit(_edit(c, kind="routing_hint")) is not None


class TestAccepts:
    def test_candor_template_is_the_model_citizen(self):
        c = (
            "Calibrated reliability for forget is 7% over 26 observations — prefer an "
            "alternative or verify its output; see why_reliability('tool_ok', 'forget')."
        )
        assert lint_edit(_edit(c, kind="routing_hint")) is None

    def test_imperative_rules_pass(self):
        for c in (
            "Before asserting a deliverable is complete: read the target file on disk.",
            "When a fetch returns a bot-block page: switch to browse_web; do not re-fetch more than twice.",
            "Never consume an attempt on an exploratory guess in attempt-limited harnesses.",
        ):
            assert lint_edit(_edit(c)) is None, c

    def test_negative_claim_with_fix_passes(self):
        c = "yt-dlp hits 403 on both boxes: stop retrying downloads and fall back to caption endpoints instead."
        assert lint_edit(_edit(c, kind="routing_hint")) is None


class TestScope:
    def test_delete_edits_skip(self):
        assert lint_edit({"action": "delete", "kind": "policy", "entry_id": "x"}) is None

    def test_unlinted_kind_skips(self):
        assert lint_edit(_edit("Despite everything, this is a spec.", kind="worker_spec")) is None

    def test_empty_content_skips(self):
        assert lint_edit(_edit("")) is None

    def test_non_dict_is_tolerated(self):
        assert lint_edit(None) is None  # type: ignore[arg-type]


def test_queue_producer_edits_applies_the_lint(monkeypatch):
    """The floor sits under all four machine producers — a narrative edit is
    rejected with a lint: reason and never reaches the engine."""
    monkeypatch.setattr("config.settings.adaptive_enabled", True)
    from core.adaptive.contract import queue_producer_edits

    result = queue_producer_edits(
        [
            {
                "action": "create",
                "kind": "policy",
                "scope": "global",
                "title": "noise",
                "content": "Despite lessons, the agent repeatedly fails to comply.",
                "evidence": ["session:x"],
            },
            {
                "action": "create",
                "kind": "prompt_note",
                "scope": "global",
                "title": "keeper",
                "content": "Before asserting completion: verify the file on disk.",
                "evidence": ["session:x"],
            },
        ],
        "refine",
        session_id="s1",
    )
    lint_rejects = [r for r in result["rejected"] if str(r["reason"]).startswith("lint:")]
    assert len(lint_rejects) == 1 and lint_rejects[0]["edit"]["title"] == "noise"
    # The keeper made it through to the engine (queued or gated, not linted).
    assert result["queued"] + result["gated"] >= 1 or result["proposal_id"] is not None
