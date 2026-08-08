"""Regression: an auto-injected skill was cut mid-procedure with no marker.

Shipped defect (architecture review 2026-08-07, §6): _validate_report did
`report.injected_skill = instructions[:5000]` under a comment claiming a "~5k
token" cap — it is 5000 CHARACTERS, roughly 1.25k tokens. A longer SKILL.md
was truncated silently, so the agent read a procedure that stopped mid-step
and followed it as if it were complete, with no hint that load_skill() would
return the rest.

Fix: the cut appends a marker naming load_skill('<name>'), and the constant
says what it actually measures.
"""

from core.scout.report import ScoutReport
from core.scout.runner import SKILL_INJECT_MAX_CHARS, _validate_report


class _SkillRegistry:
    def __init__(self, body: str):
        self.body = body

    def exists(self, name):
        return True

    def is_disabled(self, name):
        return False

    def is_valid(self, name):
        return True

    def load_instructions(self, name):
        return self.body


def _validate_with_skill(monkeypatch, body: str) -> ScoutReport:
    monkeypatch.setattr("core.skills.registry.get_skill_registry", lambda: _SkillRegistry(body))
    return _validate_report(ScoutReport(recommended_skills=["deploy"]))


def test_oversized_skill_is_marked_as_truncated(monkeypatch):
    report = _validate_with_skill(monkeypatch, "step\n" * 4000)
    assert len(report.injected_skill) > SKILL_INJECT_MAX_CHARS  # body + marker
    assert "skill truncated" in report.injected_skill
    assert "load_skill('deploy')" in report.injected_skill
    # The marker has to survive into the agent's prompt, not just the field.
    assert "skill truncated" in report.to_system_prompt_section()


def test_skill_that_fits_is_untouched(monkeypatch):
    body = "a short and complete procedure"
    report = _validate_with_skill(monkeypatch, body)
    assert report.injected_skill == body
