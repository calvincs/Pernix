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
from core.llm.types import (
    ChatResponse,
    HealthStatus,
    ModelInfo,
    ProviderConfig,
    StreamEvent,
    StreamEventType,
    TokenUsage,
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


def _parse_usage(usage_data: dict) -> TokenUsage:
    details = usage_data.get("prompt_tokens_details") or {}
    return TokenUsage(
        prompt_tokens=usage_data.get("prompt_tokens", 0),
        completion_tokens=usage_data.get("completion_tokens", 0),
        total_tokens=usage_data.get("total_tokens", 0),
        # OpenAI shape first; Anthropic-style key as a fallback for
        # OpenAI-compatible gateways that use it.
        cache_read_tokens=details.get("cached_tokens", 0) or usage_data.get("cache_read_input_tokens", 0),
        cache_write_tokens=usage_data.get("cache_creation_input_tokens", 0),
    )


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
        return {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
        }

    def _base_url(self) -> str:
        return self._config.base_url.rstrip("/")

    def _model(self, model: str) -> str:
        return model or settings.llm_model

    @property
    def available(self) -> bool:
        return bool(self._config.api_key)

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
        if temperature is not None:
            payload["temperature"] = temperature
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
    ) -> ChatResponse:
        model = self._model(model)
        payload = self._build_payload(messages, model, max_tokens, tools, temperature, stream=False)

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
                messages, tools=tools, model=model, max_tokens=max_tokens, temperature=temperature, _retried=True
            )
        resp.raise_for_status()
        data = resp.json()

        # Guard against 200-with-error bodies (some compatible gateways do this)
        if "choices" not in data:
            error_msg = data.get("error", {}).get("message", str(data)[:500])
            from core.llm.errors import FailoverError, classify_http_error

            reason = classify_http_error(400, error_msg)
            raise FailoverError(reason, f"OpenAI error: {error_msg}")

        return self._parse_response(data, model)

    def _parse_response(self, data: dict, model: str) -> ChatResponse:
        msg = data["choices"][0].get("message", {})
        content = msg.get("content") or ""
        finish = data["choices"][0].get("finish_reason", "stop")

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

        return ChatResponse(
            content=content,
            tool_calls=tool_calls,
            usage=_parse_usage(data.get("usage", {})),
            model=model,
            provider=self.name,
            finish_reason=finish,
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
        last_finish_reason: str | None = None
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
                            yield event
                        done_sent = True
                        return
                    logger.error("OpenAI %d error body: %s", resp.status_code, body[:500])
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    raw = line[6:].strip()
                    if raw == "[DONE]":
                        flushed = _flush_tool_calls()
                        if flushed:
                            yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=flushed)
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
                        from core.llm.errors import FailoverError, FailoverReason, classify_http_error

                        reason = classify_http_error(400, err_msg)
                        if reason == FailoverReason.CONTEXT_OVERFLOW:
                            raise FailoverError(reason, err_msg)
                        yield StreamEvent(type=StreamEventType.ERROR, error=err_msg)
                        break

                    usage_data = chunk.get("usage")
                    if usage_data:
                        yield StreamEvent(type=StreamEventType.USAGE, usage=_parse_usage(usage_data))

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
                            yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=flushed)

                    content = delta.get("content")
                    if content:
                        yield StreamEvent(type=StreamEventType.TOKEN, content=content)
        except GeneratorExit:
            return
        except Exception as e:
            logger.error("OpenAI stream error: %s", e)
            yield StreamEvent(type=StreamEventType.ERROR, error=str(e))
        finally:
            try:
                flushed = _flush_tool_calls()
                if flushed:
                    yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=flushed)
                if not done_sent:
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
        client = self._get_quick_client()
        try:
            resp = await client.get(f"{self._base_url()}/models", headers=self._headers())
            resp.raise_for_status()
            self._models_cache = resp.json().get("data", [])
        except Exception as e:
            logger.warning("Failed to fetch OpenAI models: %s", e)
            self._models_cache = []
        return self._models_cache

    def clear_models_cache(self) -> None:
        """Invalidate the cached model list so next fetch hits the API."""
        self._models_cache = None

    def _model_info(self, model_id: str) -> ModelInfo:
        ctx = 128_000
        for prefix, length in _CONTEXT_HINTS.items():
            if model_id == prefix or model_id.startswith(prefix + "-"):
                ctx = length
                break
        return ModelInfo(
            id=model_id,
            provider=self.name,
            context_length=ctx,
            supports_vision=model_id.startswith(_VISION_PREFIXES),
        )

    async def get_model_info(self, model: str) -> ModelInfo:
        return self._model_info(self._model(model))

    async def list_models(self) -> list[ModelInfo]:
        models_data = await self._fetch_models()
        allowed = set(settings.openai_models) if settings.openai_models else None
        results = []
        for m in models_data:
            mid = m.get("id", "")
            if not mid or (allowed and mid not in allowed):
                continue
            results.append(self._model_info(mid))
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
