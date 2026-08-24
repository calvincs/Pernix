"""Scout never pointed the agent at its stateful escape hatches (F15, field
case 17683100ecf8).

The agent drove a live ARC-AGI-3 game environment through ~20 cold bash
heredocs — each a fresh process with a new anonymous API session, re-fetching
25 environments — while the persistent repl kernel (variables survive rounds
and turns) sat unused except for text slicing. repl is a builtin, so it never
appears in recommended_tools; the steering has to live in the scout's
approach_guidance rules, gated on session_kernel_enabled like the RLM rule.
"""

from core.scout.runner import _scout_system_prompt


def test_kernel_rule_present_when_kernel_enabled(monkeypatch):
    monkeypatch.setattr("config.settings.session_kernel_enabled", True)
    text = _scout_system_prompt()
    assert "STATEFUL ENVIRONMENTS" in text
    assert "repl kernel" in text
    # /no_think must stay the last directive (provider quirk).
    assert text.rstrip().endswith("/no_think")
    assert text.index("STATEFUL ENVIRONMENTS") < text.index("- Do NOT use <think>")


def test_kernel_rule_absent_when_kernel_disabled(monkeypatch):
    monkeypatch.setattr("config.settings.session_kernel_enabled", False)
    monkeypatch.setattr("config.settings.rlm_enabled", False)
    monkeypatch.setattr("config.settings.gates_enabled", False)
    text = _scout_system_prompt()
    assert "STATEFUL ENVIRONMENTS" not in text
