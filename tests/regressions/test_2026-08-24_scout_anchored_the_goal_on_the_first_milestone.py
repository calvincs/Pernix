"""Scout framed a 6-level game as "fix Level 1" and the agent stopped there
(field case ae952f40e3d1).

The retry plan's mission statement was "FIX the known model flaw... build on
saved state" — so the agent cleared Level 1 of 6, declared the deliverable
complete, and stopped with ~60 of 100 rounds unused, asking the user whether
to continue. Reflect graded it "retry" for exactly this. The scout prompt now
carries a COMPLETION TARGET rule: when the user asks to complete a named
unit, deliverables_plan names the FULL unit; milestones are steps, never the
deliverable.
"""

from core.scout.runner import SCOUT_SYSTEM_PROMPT, _scout_system_prompt


def test_completion_target_rule_is_in_the_static_prompt():
    assert "COMPLETION TARGET" in SCOUT_SYSTEM_PROMPT
    assert "FULL unit" in SCOUT_SYSTEM_PROMPT
    # Milestones are steps, not deliverables — the anchoring failure mode.
    assert "never as the deliverable itself" in SCOUT_SYSTEM_PROMPT


def test_rule_survives_conditional_rule_injection(monkeypatch):
    monkeypatch.setattr("config.settings.rlm_enabled", True)
    monkeypatch.setattr("config.settings.session_kernel_enabled", True)
    text = _scout_system_prompt()
    assert "COMPLETION TARGET" in text
    assert text.rstrip().endswith("/no_think")
