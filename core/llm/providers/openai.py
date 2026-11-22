"""Pernix — Native OpenAI (and OpenAI-compatible) LLM provider adapter.

Adaptation plan 1a. base_url is overridable (settings.openai_base_url) so any
OpenAI-compatible server works: vLLM, LM Studio, llama.cpp server. The API
key is env-only (OPENAI_API_KEY) — settings.json is plaintext on disk.

Differences from the OpenRouter adapter it mirrors:
  - Cached-prompt tokens arrive as usage.prompt_tokens_details.cached_tokens
    (OpenAI caches >=1024-token stable prefixes automatically); mapped onto
    TokenUsage.cache_read_tokens so the existing plumbing records them.
  - Streaming requires stream_options={"include_usage": true} or no usage
    chunk is ever sent.
  - Newer OpenAI models (o-series, gpt-5) reject max_tokens in favor of
    max_completion_tokens. First call uses max_tokens; on the specific 400
    the model is remembered in _needs_max_completion_tokens and the call
    retried once. This keeps older OpenAI-compatible servers (which reject
    the NEW param) working without configuration.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import AsyncGenerator

import httpx

from config import settings
from core.llm.errors import FailoverError, FailoverReason, classify_http_error
from core.llm.providers._shared import describe_exception, http_status_failover, parse_usage, stream_failover
from core.llm.types import (
    ChatResponse,
    HealthStatus,
    ModelInfo,
    ProviderConfig,
    StreamEvent,
    StreamEventType,
    ToolCall,
)

logger = logging.getLogger("pernix.llm.openai")

# Context-length hints for common OpenAI models — /models carries no context
# metadata. Unknown models default to 128k; vision-capable prefixes below.
_CONTEXT_HINTS: dict[str, int] = {
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4.1": 1_047_576,
    "gpt-4.1-mini": 1_047_576,
    "gpt-4.1-nano": 1_047_576,
    "o3": 200_000,
    "o3-mini": 200_000,
    "o4-mini": 200_000,
}

_VISION_PREFIXES = ("gpt-4o", "gpt-4.1", "gpt-5", "o3", "o4", "chatgpt-4o")

# A failed /models fetch is retried after this long instead of being cached
# as an empty list for the life of the process.
_MODELS_RETRY_S = 30.0


# Both cache-token dialects now live in providers/_shared so the OpenRouter
# adapter reads the OpenAI shape too; kept as a module-level name because it
# is part of this module's tested surface.
_parse_usage = parse_usage


class OpenAIProvider:
    """OpenAI adapter implementing ProviderProtocol."""

    name = "openai"

    def __init__(self, config: ProviderConfig | None = None):
        self._config = config or ProviderConfig(
            name="openai",
            base_url=settings.openai_base_url,
            api_key=os.environ.get("OPENAI_API_KEY", ""),
        )
        self._client: httpx.AsyncClient | None = None
        self._quick_client: httpx.AsyncClient | None = None
        self._models_cache: list[dict] | None = None
        self._models_retry_at = 0.0
        # Models that rejected max_tokens and require max_completion_tokens.
        self._needs_max_completion_tokens: set[str] = set()

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._config.timeout, connect=self._config.connect_timeout),
                limits=httpx.Limits(
                    max_connections=self._config.max_connections,
                    max_keepalive_connections=self._config.max_keepalive,
                ),
            )
        return self._client

    def _get_quick_client(self) -> httpx.AsyncClient:
        if self._quick_client is None or self._quick_client.is_closed:
            self._quick_client = httpx.AsyncClient(
                timeout=httpx.Timeout(15.0, connect=10.0),
                limits=httpx.Limits(max_connections=5, max_keepalive_connections=3),
            )
        return self._quick_client

    def _headers(self) -> dict:
        # No key, no Authorization header. "Bearer " with nothing after it is
        # an illegal header value to httpx (trailing whitespace), so a keyless
        # self-hosted server failed every request before it left the process
        # — /v1/models included, which is how a vLLM model never reached the
        # registry ("Failed to fetch OpenAI models: Illegal header value").
        headers = {"Content-Type": "application/json"}
        if self._config.api_key:
            headers["Authorization"] = f"Bearer {self._config.api_key}"
        return headers

    def _base_url(self) -> str:
        return self._config.base_url.rstrip("/")

    def _model(self, model: str) -> str:
        return model or settings.llm_model

    @property
    def available(self) -> bool:
        # A key proves intent for api.openai.com; a self-hosted base_url
        # (vLLM, llama.cpp, LiteLLM) proves it on its own — such servers
        # typically run keyless, and gating on OPENAI_API_KEY alone made the
        # provider silently vanish when the key was lost (2026-08-19: a
        # container rebuild dropped the app-written /app/.env and every chat
        # detoured ollama → 404 → paid remote fallback). Without a key no
        # Authorization header is sent at all (see _headers).
        if self._config.api_key:
            return True
        base = (self._config.base_url or "").strip()
        return bool(base) and "api.openai.com" not in base

    def _build_payload(
        self,
        messages: list[dict],
        model: str,
        max_tokens: int,
        tools: list[dict] | None,
        temperature: float | None,
        stream: bool,
    ) -> dict:
        payload: dict = {
            "model": model,
            "messages": messages,
            "stream": stream,
        }
        capped = min(max_tokens, 16000)
        if model in self._needs_max_completion_tokens:
            payload["max_completion_tokens"] = capped
        else:
            payload["max_tokens"] = capped
        if stream:
            payload["stream_options"] = {"include_usage": True}
        if tools:
            payload["tools"] = tools
            # A one-shot call offering one tool wants that tool. Left to choose,
            # self-hosted Qwen skipped scout's submit_report on 25 of 184 calls.
            if len(tools) == 1 and not stream:
                payload["tool_choice"] = {"type": "function", "function": {"name": tools[0]["function"]["name"]}}
        if temperature is not None:
            payload["temperature"] = temperature
        # Self-hosted Qwen with thinking off sometimes ends a finished answer
        # with a stray `</think>` where its stop token belongs, then writes the
        # answer again. Everything past that tag is noise, so end there.
        if "api.openai.com" not in (self._config.base_url or ""):
            payload["stop"] = ["</think>"]
        return payload

    @staticmethod
    def _is_max_tokens_param_error(status: int, body: str) -> bool:
        return status == 400 and "max_tokens" in body and "max_completion_tokens" in body

    # ------------------------------------------------------------------
    # Chat (non-streaming)
    # ------------------------------------------------------------------

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str = "",
        max_tokens: int = 4096,
        temperature: float | None = None,
        _retried: bool = False,
        response_format: dict | None = None,
    ) -> ChatResponse:
        model = self._model(model)
        payload = self._build_payload(messages, model, max_tokens, tools, temperature, stream=False)
        # JSON mode: the server constrains decoding to valid JSON from the
        # first token. Reflect's plain-text replies failed to parse on 7 of 12
        # parallel probes (prose, `{}`, empty); with this set, 11 of 12 parsed.
        if response_format:
            payload["response_format"] = response_format

        client = self._get_client()
        resp = await client.post(
            f"{self._base_url()}/chat/completions",
            json=payload,
            headers=self._headers(),
        )
        if not _retried and self._is_max_tokens_param_error(resp.status_code, resp.text):
            logger.info("Model %s requires max_completion_tokens; retrying", model)
            self._needs_max_completion_tokens.add(model)
            return await self.chat(
                messages,
                tools=tools,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                _retried=True,
                response_format=response_format,
            )
        resp.raise_for_status()
        data = resp.json()

        # Guard against 200-with-error bodies (some compatible gateways do this)
        if "choices" not in data:
            error_msg = data.get("error", {}).get("message", str(data)[:500])
            raise FailoverError(classify_http_error(400, error_msg), f"OpenAI error: {error_msg}")

        return self._parse_response(data, model)

    def _parse_response(self, data: dict, model: str) -> ChatResponse:
        msg = data["choices"][0].get("message", {})
        content = msg.get("content") or ""
        finish = data["choices"][0].get("finish_reason", "stop")
        reasoning = msg.get("reasoning") or msg.get("reasoning_content") or ""

        tool_calls = None
        raw_tcs = msg.get("tool_calls")
        if raw_tcs:
            tool_calls = [
                ToolCall(
                    id=tc.get("id", f"call_{i}"),
                    name=tc.get("function", {}).get("name", ""),
                    arguments=tc.get("function", {}).get("arguments", "{}"),
                )
                for i, tc in enumerate(raw_tcs)
            ]
            finish = "tool_calls"

        usage = parse_usage(data.get("usage"))
        if finish == "length" and not content and not tool_calls:
            _warn_reasoning_exhausted(model, len(reasoning), usage)

        return ChatResponse(
            content=content,
            tool_calls=tool_calls,
            usage=usage,
            model=model,
            provider=self.name,
            finish_reason=finish,
            reasoning=reasoning,
        )

    # ------------------------------------------------------------------
    # Chat (streaming)
    # ------------------------------------------------------------------

    async def chat_stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str = "",
        max_tokens: int = 4096,
        temperature: float | None = None,
        _retried: bool = False,
    ) -> AsyncGenerator[StreamEvent, None]:
        model = self._model(model)
        payload = self._build_payload(messages, model, max_tokens, tools, temperature, stream=True)

        client = self._get_client()
        done_sent = False
        # See the OpenRouter adapter for the full rationale: `emitted_output`
        # is the pre-stream/mid-stream fence for failover, `failing_over`
        # suppresses the finally-block DONE so a raised failure reaches the
        # router as an exception (architecture review, Appendix C §2).
        emitted_output = False
        failing_over = False
        last_finish_reason: str | None = None
        # For the reasoning-ate-the-budget warning at the end of the stream.
        reasoning_chars = 0
        content_seen = False
        tools_seen = False
        last_usage = None
        # Index-based accumulator for streaming tool_call deltas (same
        # contract as the OpenRouter adapter: flush on finish_reason,
        # [DONE], and in finally as a safety net).
        tc_accumulator: dict[int, dict] = {}

        def _flush_tool_calls():
            if not tc_accumulator:
                return None
            tool_calls = [
                ToolCall(
                    id=acc["id"] or f"call_{idx}",
                    name=acc["name"],
                    arguments=acc["arguments"],
                )
                for idx, acc in sorted(tc_accumulator.items())
            ]
            tc_accumulator.clear()
            return tool_calls

        try:
            async with client.stream(
                "POST",
                f"{self._base_url()}/chat/completions",
                json=payload,
                headers=self._headers(),
            ) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", errors="replace")
                    if not _retried and self._is_max_tokens_param_error(resp.status_code, body):
                        logger.info("Model %s requires max_completion_tokens; retrying stream", model)
                        self._needs_max_completion_tokens.add(model)
                        async for event in self.chat_stream(
                            messages,
                            tools=tools,
                            model=model,
                            max_tokens=max_tokens,
                            temperature=temperature,
                            _retried=True,
                        ):
                            if event.type in (StreamEventType.TOKEN, StreamEventType.TOOL_CALL):
                                emitted_output = True
                            yield event
                        done_sent = True
                        return
                    logger.error("OpenAI %d error body: %s", resp.status_code, body[:500])
                    # Typed rather than raise_for_status(): only the body says
                    # whether this is a context overflow, an auth failure, or a
                    # transient the router should fail over on.
                    raise http_status_failover("OpenAI", resp.status_code, body)
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    raw = line[6:].strip()
                    if raw == "[DONE]":
                        flushed = _flush_tool_calls()
                        if flushed:
                            tools_seen = True
                            yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=flushed)
                        if last_finish_reason == "length" and not content_seen and not tools_seen:
                            _warn_reasoning_exhausted(model, reasoning_chars, last_usage)
                        done_sent = True
                        yield StreamEvent(type=StreamEventType.DONE, finish_reason=last_finish_reason)
                        return

                    try:
                        chunk = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    chunk_error = chunk.get("error")
                    if chunk_error:
                        err_msg = (
                            chunk_error.get("message", str(chunk_error))
                            if isinstance(chunk_error, dict)
                            else str(chunk_error)
                        )
                        logger.error("OpenAI stream error in chunk: %s", err_msg)
                        # The provider's own code outranks a guess from the
                        # message: a 200 body carrying {"code": 502} was
                        # classified as HTTP 400 (non-retryable), so the
                        # ladder skipped its backoff entirely, and a 429 was
                        # never recognised as a rate limit. Naming the code in
                        # the text is also what the retry markers key on.
                        err_code = chunk_error.get("code") if isinstance(chunk_error, dict) else None
                        status = err_code if isinstance(err_code, int) else 400
                        if err_code is not None:
                            err_msg = f"OpenAI {err_code}: {err_msg}"
                        reason = classify_http_error(status, err_msg, code=err_code)
                        if reason == FailoverReason.CONTEXT_OVERFLOW or not emitted_output:
                            # Nothing has reached the caller yet, so this is
                            # still a clean failover rather than a truncated
                            # answer mid-flight.
                            raise FailoverError(reason, err_msg)
                        yield StreamEvent(type=StreamEventType.ERROR, error=err_msg)
                        break

                    usage_data = chunk.get("usage")
                    if usage_data:
                        last_usage = parse_usage(usage_data)
                        yield StreamEvent(type=StreamEventType.USAGE, usage=last_usage)

                    choices = chunk.get("choices", [])
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {})
                    finish_reason = choices[0].get("finish_reason")
                    if finish_reason:
                        last_finish_reason = finish_reason

                    raw_tcs = delta.get("tool_calls")
                    if raw_tcs:
                        for tc_delta in raw_tcs:
                            idx = tc_delta.get("index", 0)
                            if idx not in tc_accumulator:
                                tc_accumulator[idx] = {"id": "", "name": "", "arguments": ""}
                            acc = tc_accumulator[idx]
                            if tc_delta.get("id"):
                                acc["id"] = tc_delta["id"]
                            func = tc_delta.get("function") or {}
                            if func.get("name"):
                                acc["name"] = func["name"]
                            if func.get("arguments"):
                                acc["arguments"] += func["arguments"]

                    if finish_reason in ("tool_calls", "stop") and tc_accumulator:
                        flushed = _flush_tool_calls()
                        if flushed:
                            emitted_output = True
                            tools_seen = True
                            yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=flushed)

                    # vLLM streams reasoning as delta.reasoning (older builds:
                    # reasoning_content), and one delta can carry a reasoning
                    # tail AND the first answer token, so both are read from
                    # the same chunk. Reasoning is not output for the failover
                    # fence: a stream that dies while the model is still
                    # thinking has given the caller no answer yet, and the
                    # ladder's reset clears the thinking it showed.
                    thought = delta.get("reasoning") or delta.get("reasoning_content")
                    if thought:
                        reasoning_chars += len(thought)
                        yield StreamEvent(type=StreamEventType.REASONING, reasoning=thought)

                    content = delta.get("content")
                    if content:
                        emitted_output = True
                        content_seen = True
                        yield StreamEvent(type=StreamEventType.TOKEN, content=content)
        except GeneratorExit:
            return
        except FailoverError as fe:
            if not emitted_output:
                failing_over = True
                raise
            logger.error("OpenAI mid-stream failure after partial output (%s); terminating", fe.reason.value)
            yield StreamEvent(type=StreamEventType.ERROR, error=fe.message)
        except (httpx.HTTPStatusError, httpx.TransportError) as e:
            logger.error("OpenAI stream transport error: %s", e)
            if not emitted_output:
                failing_over = True
                raise stream_failover("OpenAI", e) from e
            yield StreamEvent(type=StreamEventType.ERROR, error=describe_exception(e))
        except Exception as e:
            logger.error("OpenAI stream error: %s", e)
            yield StreamEvent(type=StreamEventType.ERROR, error=describe_exception(e))
        finally:
            if not failing_over:
                try:
                    flushed = _flush_tool_calls()
                    if flushed:
                        tools_seen = True
                        yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=flushed)
                    if not done_sent:
                        if last_finish_reason == "length" and not content_seen and not tools_seen:
                            _warn_reasoning_exhausted(model, reasoning_chars, last_usage)
                        done_sent = True
                        yield StreamEvent(type=StreamEventType.DONE, finish_reason=last_finish_reason)
                except GeneratorExit:
                    return

    # ------------------------------------------------------------------
    # Model info
    # ------------------------------------------------------------------

    async def _fetch_models(self) -> list[dict]:
        if self._models_cache is not None:
            return self._models_cache
        # A failure is remembered briefly, not for the life of the process:
        # caching [] made one failed fetch at startup (server still loading,
        # a refused header) permanent, because the registry's lazy
        # repopulate (budget.ensure_model_known) reuses provider caches.
        if time.monotonic() < self._models_retry_at:
            return []
        client = self._get_quick_client()
        try:
            resp = await client.get(f"{self._base_url()}/models", headers=self._headers())
            resp.raise_for_status()
            self._models_cache = resp.json().get("data", [])
        except Exception as e:
            logger.warning("Failed to fetch OpenAI models from %s: %s", self._base_url(), describe_exception(e))
            self._models_retry_at = time.monotonic() + _MODELS_RETRY_S
            return []
        return self._models_cache

    def clear_models_cache(self) -> None:
        """Invalidate the cached model list so next fetch hits the API."""
        self._models_cache = None
        self._models_retry_at = 0.0

    # Self-hosted OpenAI-compatible servers advertise the real context window
    # in their /v1/models entries under various names (vLLM: max_model_len;
    # others: context_length / context_window / max_context_length).
    # api.openai.com sends none of these — the name-hint table below covers it.
    _CTX_FIELDS = ("max_model_len", "context_length", "context_window", "max_context_length")

    def _model_info(self, model_id: str, entry: dict | None = None) -> ModelInfo:
        # Server-advertised window first: it is the serving truth, not a
        # guess. Ignoring it cost half the real window on the llm-host vLLM
        # deploy — the adapter fetched max_model_len=262,144 and defaulted
        # the unrecognized id to 128K anyway (2026-08-19).
        ctx = 0
        for field in self._CTX_FIELDS:
            try:
                ctx = int((entry or {}).get(field) or 0)
            except (TypeError, ValueError):
                ctx = 0
            if ctx > 0:
                break
        if ctx <= 0:
            ctx = 128_000
            for prefix, length in _CONTEXT_HINTS.items():
                if model_id == prefix or model_id.startswith(prefix + "-"):
                    ctx = length
                    break
        return ModelInfo(
            id=model_id,
            provider=self.name,
            context_length=ctx,
            supports_vision=(model_id.startswith(_VISION_PREFIXES) or model_id in settings.vision_model_overrides),
        )

    async def get_model_info(self, model: str) -> ModelInfo:
        model_id = self._model(model)
        entry = None
        try:
            entry = next((m for m in await self._fetch_models() if m.get("id") == model_id), None)
        except Exception:
            pass  # cache miss/network failure — fall back to hints below
        return self._model_info(model_id, entry)

    async def list_models(self) -> list[ModelInfo]:
        models_data = await self._fetch_models()
        allowed = set(settings.openai_models) if settings.openai_models else None
        results = []
        for m in models_data:
            mid = m.get("id", "")
            if not mid or (allowed and mid not in allowed):
                continue
            results.append(self._model_info(mid, m))
        return results

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def check_health(self) -> HealthStatus:
        if not self.available:
            return HealthStatus(healthy=False, error="No API key configured")
        client = self._get_quick_client()
        start = time.monotonic()
        try:
            resp = await client.get(f"{self._base_url()}/models", headers=self._headers())
            latency = int((time.monotonic() - start) * 1000)
            resp.raise_for_status()
            data = resp.json()
            return HealthStatus(
                healthy=True,
                latency_ms=latency,
                models_available=len(data.get("data", [])),
            )
        except Exception as e:
            latency = int((time.monotonic() - start) * 1000)
            return HealthStatus(healthy=False, latency_ms=latency, error=str(e))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
        if self._quick_client and not self._quick_client.is_closed:
            await self._quick_client.aclose()
            self._quick_client = None
        self._models_cache = None


def _warn_reasoning_exhausted(model: str, reasoning_chars: int, usage) -> None:
    """Say so when the output budget ran out before the answer started.

    finish_reason=length with no content and no tool call is not an empty
    answer: the model spent max_tokens thinking. Left silent it reads as a
    model that replied with nothing.
    """
    details = getattr(usage, "extra", None) or {}
    logger.warning(
        "%s hit max_tokens before answering: finish_reason=length, no content, %d reasoning chars "
        "(completion_tokens=%s, reasoning_tokens=%s) — reasoning consumed the output budget",
        model,
        reasoning_chars,
        getattr(usage, "completion_tokens", "?"),
        details.get("reasoning_tokens", "?"),
    )
