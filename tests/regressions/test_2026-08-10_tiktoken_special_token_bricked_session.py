"""A tool result that merely *quotes* a special token must not brick the session.

Field incident (box.ventibean, 2026-08-10): a HuggingFace model listing came
back as a 7.8 KB tool result whose JSON contained the literal string
"<|endoftext|>" in tokenizer metadata. tiktoken's encode() defaults to
disallowed_special="all", so counting that message raised

    ValueError: Encountered text corresponding to disallowed special token
                '<|endoftext|>'

TokenEstimator.count() runs over every message on every context compile, so the
failure was not a one-off: the offending text was already persisted in the
transcript, which meant every subsequent turn in that session — and in the
worker it had spawned — died before the agent could start. Two sessions were
permanently unusable (a212dd394eda, a231f8434e1c).

These tests pin the counter's behaviour on such text rather than the exact
counts, which are tokenizer-version dependent.
"""

from __future__ import annotations

import pytest

from core.context.tokens import TokenEstimator

# The strings tiktoken treats as special for cl100k_base, plus the chat-template
# markers that show up constantly in model metadata and prompt discussions.
SPECIAL_STRINGS = [
    "<|endoftext|>",
    "<|fim_prefix|>",
    "<|fim_middle|>",
    "<|fim_suffix|>",
    "<|endofprompt|>",
    "<|im_start|>",
    "<|im_end|>",
]


@pytest.fixture(scope="module")
def estimator():
    est = TokenEstimator()
    if est._enc is None:
        pytest.skip("tiktoken unavailable; the char heuristic cannot hit this bug")
    return est


@pytest.mark.parametrize("special", SPECIAL_STRINGS)
def test_count_does_not_raise_on_special_token_text(estimator, special):
    assert estimator.count(special) > 0
    assert estimator.count(f"prefix {special} suffix") > 0


def test_count_message_survives_the_exact_field_payload(estimator):
    """The shape that actually broke production: a tool result quoting metadata."""
    payload = (
        '{"id":"fishaudio/s2-pro","pipeline_tag":"text-to-speech",'
        '"config":{"tokenizer":{"eos_token":"<|endoftext|>",'
        '"chat_template":"<|im_start|>user<|im_end|>"}}}'
    )
    assert estimator.count_message({"role": "tool", "content": payload}) > 0


def test_special_token_text_counts_as_ordinary_text(estimator):
    """Not silently collapsed to one token.

    Providers escape these sequences rather than honouring them as control
    tokens, so the honest estimate treats them as the several ordinary tokens
    they will actually become. allowed_special="all" would undercount instead.
    """
    assert estimator.count("<|endoftext|>") > 1


def test_a_whole_conversation_containing_one_poisoned_message_still_counts(estimator):
    """The session-bricking path: counting the full history, not one string."""
    messages = [
        {"role": "user", "content": "find audio models"},
        {"role": "tool", "content": '{"eos_token":"<|endoftext|>"}'},
        {"role": "assistant", "content": "Here are the alternatives."},
    ]
    assert sum(estimator.count_message(m) for m in messages) > 0
