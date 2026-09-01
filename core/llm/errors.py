"""Pernix — Typed LLM failover errors with classification."""

from __future__ import annotations

from enum import Enum


class FailoverReason(str, Enum):
    """Closed set of failure reasons for LLM requests."""

    RATE_LIMIT = "rate_limit"
    OVERLOADED = "overloaded"
    TIMEOUT = "timeout"
    CONTEXT_OVERFLOW = "context_overflow"
    AUTH = "auth"
    MODEL_NOT_FOUND = "model_not_found"
    FORMAT_ERROR = "format_error"
    UNKNOWN = "unknown"


# Reasons where falling back to another provider is appropriate.
# Covers transient and unspecified failures. Excludes AUTH, MODEL_NOT_FOUND,
# CONTEXT_OVERFLOW, and FORMAT_ERROR — those are config/logic problems that
# should surface loudly rather than be masked by a silent fallback.
FALLBACK_REASONS = frozenset(
    {
        FailoverReason.RATE_LIMIT,
        FailoverReason.OVERLOADED,
        FailoverReason.TIMEOUT,
        FailoverReason.UNKNOWN,
    }
)

# Phrases in a 400 body that mean the PROMPT exceeded the model's window —
# the one 400 the caller can fix by compacting. Deliberately overflow-
# specific: the old single-word list ("token", "length", "maximum",
# "exceed") also matched "max_tokens is too large" and "image exceeds 20MB",
# and each false positive cost a pointless compaction pass.
_CONTEXT_OVERFLOW_PHRASES = (
    "context length",  # OpenAI / vLLM / OpenRouter: "maximum context length is N tokens"
    "maximum context",
    "context window",
    "context size",  # llama.cpp: "the request exceeds the available context size"
    "prompt is too long",  # Anthropic: "prompt is too long: N tokens > M maximum"
    "input is too long",
    "too many tokens",
    "context_length_exceeded",
)

# Machine-readable error codes that mean the same thing (OpenAI sets
# error.code="context_length_exceeded"; some gateways say window instead).
_CONTEXT_OVERFLOW_CODES = ("context_length_exceeded", "context_window_exceeded")


class FailoverError(Exception):
    """Typed LLM error with a classified reason for recovery decisions."""

    def __init__(
        self,
        reason: FailoverReason,
        message: str,
        original: Exception | None = None,
    ):
        self.reason = reason
        self.message = message
        self.original = original
        super().__init__(message)


def is_context_overflow_code(code: object) -> bool:
    """True when a provider's machine-readable error code names an overflow."""
    if not code:
        return False
    lowered = str(code).lower()
    return any(marker in lowered for marker in _CONTEXT_OVERFLOW_CODES)


def classify_http_error(status_code: int, body: str = "", code: object = None) -> FailoverReason:
    """Classify an HTTP error into a FailoverReason.

    Uses status code first, then inspects body text for 400-class errors
    to distinguish context overflow from generic format errors. `code` is
    the provider's machine-readable error code when the response carried
    one (OpenAI: "context_length_exceeded") — it outranks the body text.
    """
    if is_context_overflow_code(code):
        return FailoverReason.CONTEXT_OVERFLOW
    if status_code in {429, 402}:
        return FailoverReason.RATE_LIMIT
    if status_code in {401, 403}:
        return FailoverReason.AUTH
    if status_code == 404:
        return FailoverReason.MODEL_NOT_FOUND
    if status_code in {502, 503}:
        return FailoverReason.OVERLOADED
    if status_code == 408:
        return FailoverReason.TIMEOUT
    if status_code == 400:
        lower = body.lower()
        if any(phrase in lower for phrase in _CONTEXT_OVERFLOW_PHRASES):
            return FailoverReason.CONTEXT_OVERFLOW
        return FailoverReason.FORMAT_ERROR
    return FailoverReason.UNKNOWN
