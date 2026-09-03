"""A 400 that merely mentioned tokens was treated as a context overflow.

`classify_http_error` decided CONTEXT_OVERFLOW on any 400 whose body
contained "token", "length", "maximum" or "exceed". "max_tokens is too large"
and "image exceeds 20MB" both matched, so the agent compacted a context that
was not full and re-sent a request that failed the same way. The fix keys on
overflow-specific phrases and on OpenAI's `code: "context_length_exceeded"`.
"""

import pytest

from core.llm.errors import FailoverReason, classify_http_error


@pytest.mark.parametrize(
    "body",
    [
        "max_tokens is too large: 20000. This model supports at most 16384 completion tokens.",
        "image exceeds 20MB maximum size",
        "Invalid value for 'temperature': must be a number, exceeds nothing",
        "Unknown parameter: 'max_output_length'",
    ],
)
def test_size_and_parameter_400s_are_format_errors(body):
    assert classify_http_error(400, body) == FailoverReason.FORMAT_ERROR


@pytest.mark.parametrize(
    "body",
    [
        "This model's maximum context length is 128000 tokens. However, your messages resulted in 130500 tokens.",
        "This endpoint's maximum context length is 8192 tokens",
        "prompt is too long: 213462 tokens > 200000 maximum",
        "the request exceeds the available context size",
        "input is too long for the context window",
        "too many tokens: 300000 > 262144",
    ],
)
def test_real_overflow_bodies_still_classify(body):
    assert classify_http_error(400, body) == FailoverReason.CONTEXT_OVERFLOW


def test_openai_error_code_outranks_a_vague_body():
    assert (
        classify_http_error(400, "Please reduce the length of the messages.", code="context_length_exceeded")
        == FailoverReason.CONTEXT_OVERFLOW
    )
    # ...and a code that says something else does not turn a size 400 into an overflow.
    assert classify_http_error(400, "max_tokens is too large", code="invalid_value") == FailoverReason.FORMAT_ERROR
