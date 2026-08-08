"""Pernix — helpers shared by the two OpenAI-wire-format provider adapters.

openrouter.py and openai.py speak the same protocol and had already drifted
apart on the details: usage parsing read only the Anthropic-shaped cache keys
in one adapter and (mostly) the OpenAI-shaped ones in the other, so cache
accounting silently reported zero for half the models routed through
OpenRouter. Anything the two adapters must agree on byte-for-byte lives here.
"""

from __future__ import annotations

import httpx

from core.llm.errors import FailoverError, FailoverReason, classify_http_error
from core.llm.types import TokenUsage


def parse_usage(usage_data: dict | None) -> TokenUsage:
    """Normalize a usage block from either cache-token dialect.

    OpenAI reports cached prompt tokens under
    ``prompt_tokens_details.cached_tokens``; Anthropic — and OpenRouter when
    it proxies ``anthropic/*`` — uses ``cache_read_input_tokens`` /
    ``cache_creation_input_tokens``. A given response only ever carries one
    shape, so reading both is safe and is the only way a single adapter can
    account for every model it can route to.
    """
    usage_data = usage_data or {}
    details = usage_data.get("prompt_tokens_details") or {}
    return TokenUsage(
        prompt_tokens=usage_data.get("prompt_tokens", 0),
        completion_tokens=usage_data.get("completion_tokens", 0),
        total_tokens=usage_data.get("total_tokens", 0),
        cache_read_tokens=details.get("cached_tokens", 0) or usage_data.get("cache_read_input_tokens", 0) or 0,
        cache_write_tokens=usage_data.get("cache_creation_input_tokens", 0) or 0,
    )


def http_status_failover(provider: str, status: int, body: str) -> FailoverError:
    """Typed error for an HTTP error status, classified from its body.

    Raised instead of ``resp.raise_for_status()`` on the streaming path: the
    body is the only place the real reason lives (context overflow vs. auth
    vs. rate limit), and by the time httpx's own error reaches the router the
    streamed response may no longer be readable.
    """
    return FailoverError(classify_http_error(status, body), f"{provider} {status}: {body[:500]}")


def stream_failover(provider: str, exc: Exception) -> FailoverError:
    """Classify an httpx failure raised while opening/reading a stream."""
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            body = exc.response.text
        except Exception:
            body = ""
        return http_status_failover(provider, exc.response.status_code, body)
    reason = FailoverReason.TIMEOUT if isinstance(exc, httpx.TimeoutException) else FailoverReason.UNKNOWN
    return FailoverError(reason, f"{provider} stream {type(exc).__name__}: {exc}", original=exc)
