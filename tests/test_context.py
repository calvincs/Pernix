"""Tests for context management: tokens, compaction, compiler."""

from core.context.compaction import apply_view_pruning, exclude_orphans
from core.context.tokens import TokenEstimator


def test_token_estimator_basic():
    est = TokenEstimator()
    t = est.count("Hello world")
    assert t > 0
    assert t < 10  # should be ~2-3 tokens


def test_token_estimator_code_detection():
    est = TokenEstimator()
    code = 'def hello():\n    print("hi")\n'
    prose = "The quick brown fox jumps over the lazy dog."
    # Code should use different ratio if tiktoken unavailable
    t_code = est.count(code)
    t_prose = est.count(prose)
    assert t_code > 0
    assert t_prose > 0


def test_token_count_message():
    est = TokenEstimator()
    msg = {"role": "user", "content": "Hello world, how are you?"}
    tokens = est.count_message(msg)
    assert tokens > 0
    assert tokens > est.count("Hello world, how are you?")  # includes overhead


def test_view_pruning():
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "tool", "content": "x" * 500},  # old, should be pruned
        {"role": "tool", "content": "y" * 100},  # old but short, kept
    ] + [
        {"role": "user", "content": f"msg {i}"} for i in range(10)
    ]  # 10 recent messages

    pruned = apply_view_pruning(messages, keep_recent=10)
    # First tool message should be pruned (>300 chars, outside recent 10)
    assert "[pruned" in pruned[1]["content"]
    # Second tool message kept (< 300 chars)
    assert pruned[2]["content"] == "y" * 100
    # Original messages unchanged
    assert len(messages[1]["content"]) == 500


def test_orphan_exclusion():
    messages = [
        {
            "role": "assistant",
            "content": "Let me help",
            "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "test", "arguments": "{}"}}],
        },
        {"role": "tool", "content": "result", "tool_call_id": "call_1"},
        {"role": "tool", "content": "orphan", "tool_call_id": "call_MISSING"},
    ]
    filtered = exclude_orphans(messages)
    assert len(filtered) == 2  # orphan removed
    assert filtered[1]["content"] == "result"


def test_view_pruning_preserves_originals():
    """Ensure view pruning doesn't modify original messages."""
    original = {"role": "tool", "content": "x" * 500}
    messages = [original] + [{"role": "user", "content": "a"}] * 10
    pruned = apply_view_pruning(messages, keep_recent=10)
    # Original should be untouched
    assert len(original["content"]) == 500
    # Pruned view should have stub
    assert "[pruned" in pruned[0]["content"]


# ===========================================================================
# Known facts must not be discounted by unfilled config
# ===========================================================================


def test_instructions_block_is_framed_as_config_not_knowledge(tmp_path, monkeypatch):
    """Regression: SESSIONS.md ships with placeholder lines ("Timezone: not
    set"). Injected bare as [INSTRUCTIONS], the model read them as ground
    truth and refused a weather request while the user's city sat in memory.
    Production session becce7a77bcb, 2026-07-24. The block now comes from the
    compiler's directives builder — the framing must have moved with it."""
    from core.context.compiler import _build_agent_directives_block

    monkeypatch.chdir(tmp_path)
    agent_dir = tmp_path / "data" / "agent"
    agent_dir.mkdir(parents=True)
    (agent_dir / "SESSIONS.md").write_text("- Timezone: not set\n- Key facts: not set")

    out = _build_agent_directives_block()

    assert "[INSTRUCTIONS]" in out
    lowered = out.lower()
    assert "not pinned in config" in lowered, "unset config must not read as unknown fact"
    assert "defer to [relevant memory]" in lowered, "framing must point at memory as the fallback"
    # Framing sits inside the INSTRUCTIONS block, above the file content.
    assert out.index("not pinned in config") > out.index("[INSTRUCTIONS]")
    assert out.index("not pinned in config") < out.index("Timezone: not set")


def test_instructions_framing_absent_when_no_instructions(tmp_path, monkeypatch):
    """No SESSIONS.md file → no [INSTRUCTIONS] block and no stray framing."""
    from core.context.compiler import _build_agent_directives_block

    monkeypatch.chdir(tmp_path)
    out = _build_agent_directives_block()
    assert "[INSTRUCTIONS]" not in out
    assert "not pinned in config" not in out


def test_scout_section_no_longer_renders_directives():
    """identity/rules/instructions moved to the compiler's fixed prefix; the
    scout section rendering them again would duplicate content and re-break
    the prompt-prefix cache at the scout boundary."""
    from core.scout.report import ScoutReport

    r = ScoutReport(
        identity="Be helpful",
        rules="Test everything",
        instructions="- Timezone: not set",
        memory_context="Something recalled.",
        approach_guidance="1. Do the thing.",
    )
    out = r.to_system_prompt_section()
    assert "[IDENTITY]" not in out
    assert "[RULES]" not in out
    assert "[INSTRUCTIONS]" not in out
    # Per-task curation still renders.
    assert "[RELEVANT MEMORY]" in out
    assert "[APPROACH]" in out


def test_base_prompt_states_memory_beats_empty_config():
    from core.context.compiler import BASE_SYSTEM_PROMPT

    p = BASE_SYSTEM_PROMPT.lower()
    assert "use what you know" in p
    assert "never overrides a recalled fact" in p


def test_scout_prompt_forbids_reporting_absence_as_memory():
    """Scout authored the actual refusal: it wrote 'no location is configured
    for this session — SESSIONS.md shows timezone: not set' INTO memory_context,
    in the same paragraph where it had just quoted Rockford."""
    from core.scout.runner import SCOUT_SYSTEM_PROMPT

    p = SCOUT_SYSTEM_PROMPT.lower()
    assert "never conclusions about what is missing" in p
    assert "known facts beat empty config" in p


def test_shipped_sessions_template_asserts_no_negative_facts():
    """The stock template must not claim facts are unset — that phrasing is
    what the model quoted back as justification for refusing."""
    from pathlib import Path

    tpl = Path("data/agent/SESSIONS.md")
    if not tpl.exists():
        import pytest

        pytest.skip("no shipped template in this checkout")
    text = tpl.read_text()
    user_ctx = text.split("## User Context")[1].split("##")[0]
    assert "not set" not in user_ctx.lower(), "User Context must use blank placeholders, not 'not set'"
    assert "does NOT" in text or "not pinned in config" in text.lower(), "template must state the precedence rule"
