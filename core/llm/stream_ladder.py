"""Pernix — the one retry/fallback ladder for a streamed LLM turn.

The agent makes two kinds of streamed call per turn: the tool-loop rounds and
the final tools=None answer. Each used to carry its own copy of the same
ladder — backoff on transient errors, then switch to the fallback model once,
then give up — and the copies had drifted:

  * only the tool loop waited on capacity (`session.waiting_llm`);
  * only the tool loop recorded cache_read / cache_write tokens, so every
    final answer's prompt-cache hit was invisible to the usage ledger even
    though the final call attaches cache breakpoints;
  * the final loop re-normalized already-normalized messages when it fell
    back, instead of re-normalizing the compiled originals for the new model.

Those are the divergences you get for free from a second implementation. One
ladder, one behavior.

What stays with the caller: what to DO with a failure. The tool loop
soft-lands an exhausted LLM time budget and hard-errors everything else; the
final-answer path saves whatever partial text it has and stops. The ladder
returns a StreamOutcome and lets each site decide.

Context overflow is the one error the caller can act on mid-ladder — the fix
is to compact and re-compile, not to retry the same oversized request — so
`raise_on_context_overflow` hands that FailoverError straight back.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from config import settings
from core.context.compiler import attach_cache_breakpoints, normalize_for_openrouter
from core.llm.budget import derive_max_output
from core.llm.errors import FailoverError, FailoverReason
from core.llm.router import OPENAI_FORMAT_PROVIDERS
from core.llm.types import StreamEventType
from db import models as db

logger = logging.getLogger("pernix.agent")

STREAM_BACKOFFS = (5, 10, 15)


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float | None:
    """USD estimate from settings.model_prices, or None for unpriced models.

    model_prices: {model_id: {"in": USD per 1M prompt tok, "out": USD per 1M
    completion tok}}. Exact model-id match only — a partial match silently
    pricing the wrong model is worse than a NULL. Display/telemetry only.
    """
    try:
        prices = settings.model_prices.get(model)
        if not isinstance(prices, dict):
            return None
        rate_in = float(prices.get("in") or 0.0)
        rate_out = float(prices.get("out") or 0.0)
        if rate_in <= 0 and rate_out <= 0:
            return None
        return (int(prompt_tokens) * rate_in + int(completion_tokens) * rate_out) / 1_000_000.0
    except Exception:
        return None


# Substrings that mark an error as worth retrying against the same model:
# gateway/5xx codes and transport-level failures. Anything else (auth, bad
# request, model-not-found) is a config problem that retrying only delays.
_RETRYABLE_MARKERS = (
    "500",
    "502",
    "503",
    "504",
    "ConnectError",
    "ReadTimeout",
    "ConnectTimeout",
    "Connection refused",
    # A stream that produced no tokens at all is an upstream flake, not a
    # config problem — the request was well-formed and the provider simply
    # sent nothing back. Field case ae952f40e3d1: one "Provider returned an
    # empty response" from the fallback model killed a 61-round turn dead
    # (classified non-retryable, fallback rung already spent).
    "empty response",
)


def is_stream_retryable(error: str) -> bool:
    return any(k in error for k in _RETRYABLE_MARKERS)


@dataclass
class StreamOutcome:
    """What one full ladder run produced.

    `error` is None on success. `content` / `tool_calls` carry the last
    attempt's output either way — on failure they are the partial the caller
    may still want to persist.
    """

    content: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    error: str | None = None
    finish_reason: str | None = None
    # The model that served the last attempt — the fallback, if the ladder
    # switched. Callers report this, not the model they asked for.
    model: str = ""
    usage: Any | None = None
    # time.monotonic() at the start of the last attempt, so round latency
    # measures the attempt that actually answered rather than the sum of
    # everything that failed first.
    started_at: float = 0.0
    tried_fallback: bool = False
    # Set instead of `error` when the caller asked to handle context overflow
    # itself. Returned rather than raised so the ladder still reports whether
    # it had already burned the fallback before the overflow hit.
    context_overflow: FailoverError | None = None


# --- Quota circuit breaker state -------------------------------------------
# model -> monotonic deadline until which failover TO that model is refused.
# Populated when a stream attempt dies on an exhausted-quota error (OpenRouter
# daily key cap 403, billing hard limits). Field case 2026-08-25/26: the
# fallback 403'd its daily cap every time, so each failover paid one doomed
# request and masked the primary's real error — killing two workers and a
# turn before anyone saw the word "budget".
_quota_block_until: dict[str, float] = {}

_QUOTA_ERR_RE = re.compile(
    r"key limit exceeded|quota exceeded|insufficient[_ ]quota|billing hard limit",
    re.IGNORECASE,
)


def _is_budget_exhaustion(err: str) -> bool:
    """True for the per-session LLM time-limit error (semaphore.py)."""
    return "exceeded the" in err and "LLM time limit" in err


def _is_quota_error(err: str) -> bool:
    return bool(_QUOTA_ERR_RE.search(err))


def _note_quota_block(model: str) -> None:
    cooldown = float(getattr(settings, "provider_quota_cooldown_s", 600) or 0)
    if cooldown > 0:
        _quota_block_until[model] = time.monotonic() + cooldown


def _quota_block_remaining(model: str) -> float:
    until = _quota_block_until.get(model)
    if until is None:
        return 0.0
    remaining = until - time.monotonic()
    if remaining <= 0:
        _quota_block_until.pop(model, None)
        return 0.0
    return remaining


async def stream_with_failover(
    *,
    client,
    session_id: str,
    emit,
    messages: list[dict],
    base_messages: list[dict],
    static_prefix_chars: int,
    tools,
    model: str,
    max_output_cap: int,
    goal_id: int | None,
    sched_created_at: float,
    sched_priority,
    tried_fallback: bool = False,
    surface_context_overflow: bool = False,
    label: str = "LLM stream",
) -> StreamOutcome:
    """Stream one response, retrying and failing over as needed.

    messages       — what to send now (already normalized for the provider).
    base_messages  — the compiled, un-normalized messages. Re-normalized for
                     the fallback model if the ladder switches: a fallback on
                     a different provider needs its own normalization and its
                     own cache breakpoints, and normalizing the already-
                     normalized `messages` a second time is not the same thing.
    max_output_cap — payload.effective_max_output. The compiler shrinks the
                     output reservation when the context is tight; asking the
                     provider for the model's full max_tokens while the
                     compiler has reserved less is how a request overflows a
                     budget the compiler thought it had balanced. 0 disables.

    With surface_context_overflow, an overflow FailoverError comes back on
    StreamOutcome.context_overflow instead of being laddered — retrying the
    same oversized request can only fail the same way; the caller has to
    compact and re-compile.
    """
    retries = 0
    current_model = model
    current_messages = messages
    overflow: FailoverError | None = None
    # Usage persists across attempts: a retry that dies before the provider
    # sends its usage frame should not erase what the previous attempt
    # reported to the caller's stream.done payload.
    usage: Any | None = None

    while True:
        content = ""
        tool_calls: list[dict] = []
        err: str | None = None
        finish_reason: str | None = None
        started_at = time.monotonic()

        if not client.has_capacity(current_model):
            emit({"type": "session.waiting_llm"})

        max_tokens = derive_max_output(current_model)
        if max_output_cap > 0:
            max_tokens = min(max_tokens, max_output_cap)

        try:
            async for event in client.chat_stream(
                current_messages,
                tools=tools,
                model=current_model,
                max_tokens=max_tokens,
                session_id=session_id,
                session_created_at=sched_created_at,
                session_priority=sched_priority,
            ):
                if event.type == StreamEventType.TOKEN and event.content:
                    content += event.content
                    emit({"type": "stream.token", "content": event.content})

                elif event.type == StreamEventType.TOOL_CALL and event.tool_calls:
                    _merge_tool_call_deltas(tool_calls, event.tool_calls)

                elif event.type == StreamEventType.USAGE and event.usage:
                    usage = event.usage
                    await asyncio.to_thread(
                        db.add_token_usage,
                        session_id=session_id,
                        model=current_model,
                        prompt_tokens=event.usage.prompt_tokens,
                        completion_tokens=event.usage.completion_tokens,
                        total_tokens=event.usage.total_tokens,
                        cache_read_tokens=event.usage.cache_read_tokens,
                        cache_write_tokens=event.usage.cache_write_tokens,
                        cost_estimate=estimate_cost(
                            current_model,
                            event.usage.prompt_tokens,
                            event.usage.completion_tokens,
                        ),
                        source="provider",
                        provider=client.resolve_provider(current_model),
                        goal_id=goal_id,
                    )

                elif event.type == StreamEventType.ERROR:
                    # An ERROR event is an error even when the adapter could
                    # not describe it. Testing `event.error` for truth used to
                    # drop the empty-string case (str(httpx.ReadTimeout()) is
                    # ''), and the adapter's finally-DONE then ended the turn
                    # as a clean, empty completion.
                    err = event.error or "provider stream ended with an error and no detail"
                    break

                elif event.type == StreamEventType.DONE:
                    # The provider's finish_reason lets the caller detect
                    # max_tokens truncation and continue in-turn instead of
                    # paying for a full reflect-retry.
                    finish_reason = event.finish_reason

        except FailoverError as fe:
            if surface_context_overflow and fe.reason == FailoverReason.CONTEXT_OVERFLOW:
                overflow = fe
            else:
                err = fe.message

        except Exception as e:
            err = str(e)

        outcome = StreamOutcome(
            content=content,
            tool_calls=tool_calls,
            error=err,
            finish_reason=finish_reason,
            model=current_model,
            usage=usage,
            started_at=started_at,
            tried_fallback=tried_fallback,
            context_overflow=overflow,
        )

        if err is None:
            return outcome

        if _is_quota_error(err):
            _note_quota_block(current_model)

        # Budget exhaustion is not a provider fault. Switching models cannot
        # buy time on a spent per-session clock, and the fallback's own
        # failure then MASKS the budget error so the agent loop's soft-land
        # (_terminate_stream_error -> BUDGET_EXHAUSTED) never runs and the
        # turn hard-errors. Propagate untouched.
        if _is_budget_exhaustion(err):
            return outcome

        if retries < len(STREAM_BACKOFFS) and is_stream_retryable(err):
            wait = STREAM_BACKOFFS[retries]
            retries += 1
            logger.warning(
                "%s error (attempt %d/%d) in session %s, retrying in %ds: %s",
                label,
                retries,
                len(STREAM_BACKOFFS),
                session_id,
                wait,
                err,
            )
            emit({"type": "stream.retry", "attempt": retries, "wait": wait, "error": err})
            await asyncio.sleep(wait)
            continue

        # A different model is a viable fallback even on the same provider
        # (model-specific failures, per-model rate buckets). Requiring a
        # different provider meant an Ollama-primary/Ollama-fallback config
        # silently had no failover at all.
        fallback = settings.fallback_model
        if fallback and not tried_fallback and fallback != current_model:
            _blocked = _quota_block_remaining(fallback)
            if _blocked > 0:
                logger.warning(
                    "%s NOT failing over for session %s: fallback %s is quota-capped "
                    "for another %.0fs — returning the original error",
                    label,
                    session_id,
                    fallback,
                    _blocked,
                )
                return outcome
            tried_fallback = True
            retries = 0
            current_model = fallback
            logger.warning(
                "%s failing over for session %s to fallback model %s (error was: %s)",
                label,
                session_id,
                fallback,
                err,
            )
            emit({"type": "stream.fallback", "model": fallback})
            fb_provider = client.resolve_provider(fallback)
            if fb_provider in OPENAI_FORMAT_PROVIDERS:
                current_messages = normalize_for_openrouter(base_messages)
                # Re-run for the NEW model: flattens stale anthropic cache
                # parts when the fallback isn't anthropic/*.
                current_messages = attach_cache_breakpoints(
                    current_messages, fallback, fb_provider, static_prefix_chars
                )
            else:
                current_messages = base_messages
            continue

        return outcome


def _merge_tool_call_deltas(collected: list[dict], deltas) -> None:
    """Fold streamed tool-call fragments into `collected`, merging by id."""
    for tc in deltas:
        existing = next((c for c in collected if c["id"] == tc.id and tc.id), None)
        if existing:
            if tc.name:
                existing["name"] = tc.name
            existing["arguments"] += tc.arguments
        else:
            collected.append({"id": tc.id, "name": tc.name, "arguments": tc.arguments})
