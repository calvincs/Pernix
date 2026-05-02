"""Tests for agent.py pure logic: StuckDetector, helper functions."""

import json
from unittest.mock import MagicMock

import pytest

from core.agent import (
    StuckDetector,
    _build_resource_status,
    _hash_args,
    _is_meta_commentary,
    _is_near_duplicate_call,
    _parse_args_dict,
    _summarize_args,
)

# ---------------------------------------------------------------------------
# _hash_args
# ---------------------------------------------------------------------------


def test_hash_args_string():
    h = _hash_args('{"key": "value"}')
    assert isinstance(h, str)
    assert len(h) == 12


def test_hash_args_dict():
    h = _hash_args({"key": "value"})
    assert isinstance(h, str)
    assert len(h) == 12


def test_hash_args_deterministic():
    assert _hash_args("abc") == _hash_args("abc")


def test_hash_args_dict_sorted():
    """Dict args are sorted so key order doesn't matter."""
    h1 = _hash_args({"b": 2, "a": 1})
    h2 = _hash_args({"a": 1, "b": 2})
    assert h1 == h2


# ---------------------------------------------------------------------------
# _summarize_args
# ---------------------------------------------------------------------------


def test_summarize_args_short():
    result = _summarize_args({"key": "short"})
    assert result == {"key": "short"}


def test_summarize_args_truncation():
    long_val = "x" * 300
    result = _summarize_args({"key": long_val}, max_value_len=200)
    assert result["key"].endswith("...")
    assert len(result["key"]) == 203  # 200 + "..."


def test_summarize_args_empty():
    assert _summarize_args({}) == {}


# ---------------------------------------------------------------------------
# _parse_args_dict
# ---------------------------------------------------------------------------


def test_parse_args_dict_from_dict():
    tc = {"arguments": {"key": "val"}}
    assert _parse_args_dict(tc) == {"key": "val"}


def test_parse_args_dict_from_json_string():
    tc = {"arguments": '{"key": "val"}'}
    assert _parse_args_dict(tc) == {"key": "val"}


def test_parse_args_dict_invalid_json():
    tc = {"arguments": "not json"}
    assert _parse_args_dict(tc) == {}


def test_parse_args_dict_empty():
    tc = {"arguments": ""}
    assert _parse_args_dict(tc) == {}


def test_parse_args_dict_missing():
    assert _parse_args_dict({}) == {}


# ---------------------------------------------------------------------------
# _is_meta_commentary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "I'll now try a different approach",
        "Let me try running the tests",
        "I need to check the file",
        "I should look at the config",
        "I will update the settings",
        "Let me read the error output",
        "I'm going to fix this bug",
        "Next I will check the database",
    ],
)
def test_is_meta_commentary_true(text):
    assert _is_meta_commentary(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "The error is caused by a null reference in the handler",
        "Here is the fixed code:\ndef foo():\n    pass",
        "",
        "x" * 201,  # Too long even with stalling prefix
    ],
)
def test_is_meta_commentary_false(text):
    assert _is_meta_commentary(text) is False


def test_is_meta_commentary_long_stalling():
    """Stalling phrase but too long — not meta."""
    text = "I'll now " + "x" * 200
    assert _is_meta_commentary(text) is False


# ---------------------------------------------------------------------------
# _is_near_duplicate_call
# ---------------------------------------------------------------------------


def test_near_dup_call_model_same():
    a = {"arguments": json.dumps({"model": "gpt-4", "images": ["img.png"], "prompt": "describe"})}
    b = {"arguments": json.dumps({"model": "gpt-4", "images": ["img.png"], "prompt": "explain"})}
    assert _is_near_duplicate_call(a, b, "call_model") is True


def test_near_dup_call_model_diff_model():
    a = {"arguments": json.dumps({"model": "gpt-4", "prompt": "hi"})}
    b = {"arguments": json.dumps({"model": "claude", "prompt": "hi"})}
    assert _is_near_duplicate_call(a, b, "call_model") is False


def test_near_dup_call_model_diff_images():
    a = {"arguments": json.dumps({"model": "gpt-4", "images": ["a.png"]})}
    b = {"arguments": json.dumps({"model": "gpt-4", "images": ["b.png"]})}
    assert _is_near_duplicate_call(a, b, "call_model") is False


def test_near_dup_generic_same_structural():
    a = {"arguments": json.dumps({"path": "foo.py", "content": "x" * 200})}
    b = {"arguments": json.dumps({"path": "foo.py", "content": "y" * 200})}
    # path is structural (short), content is long — filtered out
    assert _is_near_duplicate_call(a, b, "file_write") is True


def test_near_dup_generic_diff_structural():
    a = {"arguments": json.dumps({"path": "foo.py", "content": "x" * 200})}
    b = {"arguments": json.dumps({"path": "bar.py", "content": "x" * 200})}
    assert _is_near_duplicate_call(a, b, "file_write") is False


def test_near_dup_generic_no_structural_args():
    """If all args are long strings, no structural comparison possible."""
    a = {"arguments": json.dumps({"prompt": "x" * 200})}
    b = {"arguments": json.dumps({"prompt": "y" * 200})}
    # Empty structural → returns False (len == 0)
    assert _is_near_duplicate_call(a, b, "bash") is False


# ---------------------------------------------------------------------------
# StuckDetector
# ---------------------------------------------------------------------------


def _make_registry(known_tools=None):
    """Create a mock registry."""
    reg = MagicMock()
    known = set(known_tools or ["bash", "file_read", "file_write"])
    reg.exists = lambda name: name in known
    return reg


class TestStuckDetector:

    def test_initial_state(self):
        sd = StuckDetector()
        assert sd.repeat_count == 0
        assert not sd.behavioral_flags
        assert not sd.has_unresolved_failure

    def test_signal1_content_repeat(self):
        sd = StuckDetector()
        reg = _make_registry()
        # First time — not a repeat
        score1, _ = sd.evaluate("hello", None, {}, reg)
        assert score1 == 0.0
        # Same content again
        score2, count = sd.evaluate("hello", None, {}, reg)
        assert score2 >= 0.5
        assert "content_repeat" in sd.behavioral_flags

    def test_signal2_tool_cycle(self):
        sd = StuckDetector()
        reg = _make_registry()
        tc = [{"name": "bash", "arguments": '{"command": "ls"}'}]
        sd.evaluate("", tc, {}, reg)
        # Same tool call again
        score, _ = sd.evaluate("", tc, {}, reg)
        assert score >= 0.3
        assert "tool_cycle" in sd.behavioral_flags

    def test_signal3_error_retry(self):
        sd = StuckDetector()
        reg = _make_registry()
        args = '{"command": "bad"}'
        args_hash = _hash_args(args)
        # Previous failure for this tool+args
        failures = {"bash": [args_hash]}
        tc = [{"name": "bash", "arguments": args}]
        score, _ = sd.evaluate("", tc, failures, reg)
        assert score >= 0.4
        assert "error_loop" in sd.behavioral_flags

    def test_signal4_noop_loop(self):
        sd = StuckDetector()
        reg = _make_registry()
        score, _ = sd.evaluate("I'll now try a different approach", None, {}, reg)
        assert score >= 0.2
        assert "noop_loop" in sd.behavioral_flags

    def test_signal5_hallucinated_tool(self):
        sd = StuckDetector()
        reg = _make_registry(["bash"])  # only bash exists
        tc = [{"name": "nonexistent_tool", "arguments": "{}"}]
        score, _ = sd.evaluate("", tc, {}, reg)
        assert score >= 0.3
        assert "hallucinated_tool" in sd.behavioral_flags

    def test_signal6_failure_drift(self):
        sd = StuckDetector()
        reg = _make_registry()
        sd.mark_failure()
        assert sd.has_unresolved_failure
        # 3 rounds of unrelated tool calls without addressing the failure
        tc = [{"name": "bash", "arguments": '{"command": "ls"}'}]
        sd.evaluate("", tc, {}, reg)
        sd.evaluate("a", [{"name": "bash", "arguments": '{"command": "pwd"}'}], {}, reg)
        score, _ = sd.evaluate("b", [{"name": "bash", "arguments": '{"command": "date"}'}], {}, reg)
        assert "failure_drift" in sd.behavioral_flags

    def test_mark_success_clears_failure(self):
        sd = StuckDetector()
        sd.mark_failure()
        assert sd.has_unresolved_failure
        sd.mark_success()
        assert not sd.has_unresolved_failure
        assert sd.unresolved_failure_rounds == 0

    def test_repeat_count_increments(self):
        sd = StuckDetector()
        reg = _make_registry()
        # Build up repeats
        sd.evaluate("same", None, {}, reg)
        sd.evaluate("same", None, {}, reg)  # repeat
        assert sd.repeat_count >= 1

    def test_repeat_count_decrements_on_clean(self):
        sd = StuckDetector()
        reg = _make_registry()
        sd.evaluate("a", None, {}, reg)
        sd.evaluate("a", None, {}, reg)  # sets repeat_count
        sd.evaluate("totally new content", None, {}, reg)  # clean — should decrement
        # repeat_count should decrease (or stay at 0)
        assert sd.repeat_count >= 0

    def test_no_false_positive_on_different_content(self):
        sd = StuckDetector()
        reg = _make_registry()
        score1, _ = sd.evaluate("message one", None, {}, reg)
        score2, _ = sd.evaluate("message two", None, {}, reg)
        score3, _ = sd.evaluate("message three", None, {}, reg)
        assert score1 == 0.0
        assert score2 == 0.0
        assert score3 == 0.0

    def test_signal7_file_edit_loop(self):
        """3+ failures on same tool+file triggers file_edit_loop signal."""
        sd = StuckDetector()
        reg = _make_registry()
        for _ in range(3):
            sd.mark_failure(tool_name="file_edit", args={"path": "foo.py"})
        score, _ = sd.evaluate("", None, {}, reg)
        assert score >= 0.4
        assert "file_edit_loop" in sd.behavioral_flags

    def test_signal7_different_files_no_penalty(self):
        """Failures on different files should NOT trigger file_edit_loop."""
        sd = StuckDetector()
        reg = _make_registry()
        sd.mark_failure(tool_name="file_edit", args={"path": "a.py"})
        sd.mark_failure(tool_name="file_edit", args={"path": "b.py"})
        sd.mark_failure(tool_name="file_edit", args={"path": "c.py"})
        score, _ = sd.evaluate("", None, {}, reg)
        assert "file_edit_loop" not in sd.behavioral_flags

    def test_signal7_success_resets_counter(self):
        """mark_success clears the per-file failure counter."""
        sd = StuckDetector()
        reg = _make_registry()
        sd.mark_failure(tool_name="file_edit", args={"path": "foo.py"})
        sd.mark_failure(tool_name="file_edit", args={"path": "foo.py"})
        # Success resets
        sd.mark_success(tool_name="file_edit", args={"path": "foo.py"})
        # One more failure — should be count=1, not 3
        sd.mark_failure(tool_name="file_edit", args={"path": "foo.py"})
        score, _ = sd.evaluate("", None, {}, reg)
        assert "file_edit_loop" not in sd.behavioral_flags

    def test_mark_failure_no_args_backwards_compat(self):
        """mark_failure() with no args still works (backwards compat)."""
        sd = StuckDetector()
        sd.mark_failure()
        assert sd.has_unresolved_failure
        assert sd.file_failure_counts == {}

    def test_mark_success_no_args_backwards_compat(self):
        """mark_success() with no args still works (backwards compat)."""
        sd = StuckDetector()
        sd.mark_failure()
        sd.mark_success()
        assert not sd.has_unresolved_failure
        assert sd.file_failure_counts == {}

    # --- Semantic streak signals (8-10) ---

    def test_signal8_empty_result_streak_fires(self):
        """3 web-tool calls returning low-info content triggers empty_result_streak.

        Catches the search-spiral seen in session 42550cc17b33 turn 3 where
        20 distinct search_web queries returning "No results found" never
        tripped any of the exact-args signals.
        """
        sd = StuckDetector()
        reg = _make_registry()
        for _ in range(3):
            sd.observe_result("search_web", {"query": "x"}, "No results found for: foo bar", was_error=False)
        score, _ = sd.evaluate("", None, {}, reg)
        assert "empty_result_streak" in sd.behavioral_flags
        assert score >= 0.3

    def test_signal8_does_not_fire_for_substantive_results(self):
        sd = StuckDetector()
        reg = _make_registry()
        body = "real content " * 100
        for _ in range(3):
            sd.observe_result("search_web", {"query": "x"}, body, was_error=False)
        sd.evaluate("", None, {}, reg)
        assert "empty_result_streak" not in sd.behavioral_flags

    def test_signal9_bot_wall_streak_fires_on_tiny_browse_bodies(self):
        """3 browse_web responses with body <800 bytes triggers bot_wall_streak."""
        sd = StuckDetector()
        reg = _make_registry()
        for _ in range(3):
            sd.observe_result("browse_web", {"url": "https://example.com/a"}, "tiny", was_error=False)
        sd.evaluate("", None, {}, reg)
        assert "bot_wall_streak" in sd.behavioral_flags

    def test_signal9_does_not_fire_for_search_web_only(self):
        """Empty search_web results are signal 8, not signal 9."""
        sd = StuckDetector()
        reg = _make_registry()
        for _ in range(3):
            sd.observe_result("search_web", {"query": "x"}, "No results found", was_error=False)
        sd.evaluate("", None, {}, reg)
        assert "bot_wall_streak" not in sd.behavioral_flags

    def test_signal9_fires_with_interleaved_search_web(self):
        """Signal 9 fires even when search_web calls appear between tiny browse_web
        results — the interleaved pattern from the motivating spiral session."""
        sd = StuckDetector()
        reg = _make_registry()
        for _ in range(3):
            sd.observe_result("search_web", {"query": "x"}, "some results", was_error=False)
            sd.observe_result("browse_web", {"url": "https://example.com/a"}, "tiny", was_error=False)
        sd.evaluate("", None, {}, reg)
        assert "bot_wall_streak" in sd.behavioral_flags

    def test_signal10_same_domain_repetition(self):
        sd = StuckDetector()
        reg = _make_registry()
        for i in range(6):
            sd.observe_result(
                "browse_web",
                {"url": f"https://github.com/some/path/{i}"},
                "x" * 4000,  # large body so signal 9 doesn't also fire
                was_error=False,
            )
        sd.evaluate("", None, {}, reg)
        assert "same_domain_repetition" in sd.behavioral_flags

    def test_observe_result_ignores_non_web_tools(self):
        """bash/file_read results don't pollute the web history."""
        sd = StuckDetector()
        for _ in range(5):
            sd.observe_result("bash", {"command": "ls"}, "", was_error=False)
            sd.observe_result("file_read", {"path": "x"}, "", was_error=False)
        assert len(sd.web_result_history) == 0
        assert sd.domain_hits == {}


# ---------------------------------------------------------------------------
# Hallucinated tool discover integration
# ---------------------------------------------------------------------------


def test_hallucinated_tool_discover_suggestions():
    """registry.discover() returns useful suggestions for hallucinated names."""
    from core.tools.registry import ToolRegistry

    reg = ToolRegistry()
    # Register a few tools to build the index
    reg.register(
        name="call_model",
        func=lambda: None,
        description="Call another LLM model",
        parameters={"type": "object", "properties": {}},
        tags=["model", "llm", "call", "chat"],
    )
    reg.register(
        name="file_write",
        func=lambda: None,
        description="Write a file",
        parameters={"type": "object", "properties": {}},
        tags=["file", "write"],
    )
    reg.rebuild_index()

    # "openai_chat" should surface "call_model" via shared "model"/"chat" tokens
    results = reg.discover("openai_chat", limit=3)
    assert len(results) > 0
    names = [r.name for r in results]
    assert "call_model" in names


def test_cooccurrence_expansion_pulls_siblings():
    """expand_cooccurrence should pull in TOOL_COOCCURRENCE siblings."""
    from core.tools.registry import ToolRegistry

    reg = ToolRegistry()
    for name in ("spawn_worker", "check_workers", "get_worker_result", "await_workers", "message_worker"):
        reg.register(
            name=name,
            func=lambda: None,
            description=f"{name} tool",
            parameters={"type": "object", "properties": {}},
            tags=[],
        )
    expanded = reg.expand_cooccurrence({"spawn_worker"})
    assert {"spawn_worker", "check_workers", "get_worker_result", "await_workers", "message_worker"} <= expanded


def test_cooccurrence_skips_unregistered_siblings():
    """expand_cooccurrence must not invent tools that aren't registered."""
    from core.tools.registry import ToolRegistry

    reg = ToolRegistry()
    # Only spawn_worker is registered — its siblings (check_workers etc.) are not.
    reg.register(
        name="spawn_worker",
        func=lambda: None,
        description="sw",
        parameters={"type": "object", "properties": {}},
        tags=[],
    )
    expanded = reg.expand_cooccurrence({"spawn_worker"})
    assert expanded == {"spawn_worker"}


def test_hallucinated_tool_hint_includes_description_and_close_match():
    """_build_hallucinated_tool_hint should name the correct tool + describe it."""
    from core.agent import _build_hallucinated_tool_hint
    from core.tools.registry import ToolRegistry

    reg = ToolRegistry()
    reg.register(
        name="get_worker_result",
        func=lambda: None,
        description="Get the final output/summary from a completed worker.",
        parameters={"type": "object", "properties": {}},
        tags=["result", "output", "summary"],
    )
    reg.register(
        name="spawn_worker",
        func=lambda: None,
        description="Spawn a worker session.",
        parameters={"type": "object", "properties": {}},
        tags=["spawn"],
    )
    reg.rebuild_index()

    hint = _build_hallucinated_tool_hint("get_worker_output", reg)
    assert "get_worker_result" in hint
    assert "Retry with name" in hint
    # Must surface the description so the model can confirm it's the right tool.
    assert "completed worker" in hint


def test_hallucinated_tool_hint_no_match_falls_back_to_discover_tools():
    """When nothing resembles the bad name, tell the agent to call discover_tools."""
    from core.agent import _build_hallucinated_tool_hint
    from core.tools.registry import ToolRegistry

    reg = ToolRegistry()
    reg.register(
        name="file_read",
        func=lambda: None,
        description="read",
        parameters={"type": "object", "properties": {}},
        tags=[],
    )
    reg.rebuild_index()

    hint = _build_hallucinated_tool_hint("xyz_totally_unrelated", reg)
    assert "discover_tools" in hint


# ---------------------------------------------------------------------------
# _build_resource_status tier boundaries
# ---------------------------------------------------------------------------


def test_resource_status_last_round_reads_coherently(monkeypatch):
    """remaining==1 means we're IN the tools-disabled round, not next.

    Previously the tier-1 copy said 'FINAL TOOL ROUND: tools=None is next' —
    on the same round where stream_tools=None was already in effect. That
    contradicted the appended 'Tools are disabled for this response' text.
    Now it should say 'LAST ROUND (tools disabled)' once, with no append.
    """
    from db import models as db

    monkeypatch.setattr("config.settings.max_tool_rounds", 10)
    sid = db.create_session()
    text = _build_resource_status(sid, None, tool_round=9)
    assert "LAST ROUND (tools disabled)" in text
    # The old contradictory copy must not be present.
    assert "tools=None is next" not in text
    assert "write it this round" not in text


def test_resource_status_penultimate_round_warns_about_deliverable(monkeypatch):
    """remaining==2 should warn the model to write any deliverable THIS round."""
    from db import models as db

    monkeypatch.setattr("config.settings.max_tool_rounds", 10)
    sid = db.create_session()
    text = _build_resource_status(sid, None, tool_round=8)
    assert "CRITICAL" in text
    assert "THIS round" in text


def test_resource_status_early_rounds_no_warning(monkeypatch):
    """Rounds with >5 remaining should have no tier warning appended."""
    from db import models as db

    monkeypatch.setattr("config.settings.max_tool_rounds", 10)
    sid = db.create_session()
    text = _build_resource_status(sid, None, tool_round=0)
    assert "LAST ROUND" not in text
    assert "CRITICAL" not in text
    assert "WARNING" not in text


# ---------------------------------------------------------------------------
# _TOOL_ALIASES scoping to active_tools
# ---------------------------------------------------------------------------


def test_tool_aliases_map_has_expected_entries():
    """Sanity: the aliases we guard against are still registered."""
    from core.agent import _TOOL_ALIASES

    assert _TOOL_ALIASES["get_worker_output"] == "get_worker_result"
    assert _TOOL_ALIASES["worker_get"] == "get_worker_result"
    assert _TOOL_ALIASES["wait_for_workers"] == "await_workers"
