"""The planning extension and the feature-eval loop were retired together.

`evaluate` and the eval_auto post-task hook only ever read
data/registry.json, which only the planning tools (add_feature,
mark_feature_passed, list_features) wrote. Both sides went in the 2026-10
prune; the deterministic-gate tools stay in the evaluation extension.
"""

from __future__ import annotations

from core.extensions import BUNDLED_EXTENSIONS, load_extensions
from core.tools.registry import ToolRegistry


def test_planning_and_evaluate_tools_are_gone_gate_tools_stay(monkeypatch):
    monkeypatch.setattr("config.settings.gates_enabled", True)
    assert "core.extensions.planning" not in BUNDLED_EXTENSIONS
    reg = ToolRegistry()
    load_extensions(reg)
    for name in ("add_feature", "mark_feature_passed", "list_features", "evaluate"):
        assert reg.get(name) is None, name
    for name in ("add_gate", "list_gates", "remove_gate"):
        assert reg.get(name) is not None, name


def test_eval_settings_and_hook_are_gone():
    from config import Settings
    from sessions import hooks

    for key in ("eval_auto", "eval_threshold", "eval_max_retries", "eval_browser_verify", "plan_review_timeout"):
        assert not hasattr(Settings(), key), key
    assert not hasattr(hooks, "_maybe_evaluate")


def test_base_prompt_never_mentions_add_feature():
    from core.context.compiler import _build_base_system_prompt

    assert "add_feature" not in _build_base_system_prompt()
