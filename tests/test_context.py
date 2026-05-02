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
