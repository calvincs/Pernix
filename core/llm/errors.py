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


# Reasons where falling back to another provider is appropriate
FALLBACK_REASONS = frozenset({FailoverReason.RATE_LIMIT, FailoverReason.OVERLOADED})

# Keywords in error bodies that indicate context/token overflow
_CONTEXT_OVERFLOW_KEYWORDS = ("token", "context", "length", "too long", "maximum", "exceed")


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


def classify_http_error(status_code: int, body: str = "") -> FailoverReason:
    """Classify an HTTP error into a FailoverReason.

    Uses status code first, then inspects body text for 400-class errors
    to distinguish context overflow from generic format errors.
    """
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
        if any(kw in lower for kw in _CONTEXT_OVERFLOW_KEYWORDS):
            return FailoverReason.CONTEXT_OVERFLOW
        return FailoverReason.FORMAT_ERROR
    return FailoverReason.UNKNOWN
