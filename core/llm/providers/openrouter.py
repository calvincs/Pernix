"""Pernix — OpenRouter LLM provider adapter."""

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

logger = logging.getLogger("pernix.llm.openrouter")

RATE_LIMIT_CODES = {402, 403, 429}


class OpenRouterProvider:
    """OpenRouter adapter implementing ProviderProtocol."""

    name = "openrouter"

    def __init__(self, config: ProviderConfig | None = None):
        self._config = config or ProviderConfig(
            name="openrouter",
            base_url=settings.openrouter_base_url,
            api_key=os.environ.get("OPENROUTER_API_KEY", ""),
        )
        self._client: httpx.AsyncClient | None = None
        self._quick_client: httpx.AsyncClient | None = None
        self._models_cache: list[dict] | None = None

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
            "HTTP-Referer": "https://pernix.local",
            "X-OpenRouter-Title": "pernix agent",
            "Content-Type": "application/json",
        }

    def _base_url(self) -> str:
        return self._config.base_url.rstrip("/")

    def _model(self, model: str) -> str:
        return model or settings.llm_model

    @property
    def available(self) -> bool:
        return bool(self._config.api_key)

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
    ) -> ChatResponse:
        model = self._model(model)
        payload: dict = {
            "model": model,
            "messages": messages,
            "max_tokens": min(max_tokens, 16000),  # OpenRouter cap
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
        if temperature is not None:
            payload["temperature"] = temperature

        client = self._get_client()
        resp = await client.post(
            f"{self._base_url()}/chat/completions",
            json=payload,
            headers=self._headers(),
        )
        resp.raise_for_status()
        data = resp.json()

        # OpenRouter can return 200 with error body
        if "choices" not in data:
            error_msg = data.get("error", {}).get("message", str(data)[:500])
            from core.llm.errors import FailoverError, classify_http_error

            reason = classify_http_error(400, error_msg)
            raise FailoverError(reason, f"OpenRouter error: {error_msg}")

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

        usage_data = data.get("usage", {})
        usage = TokenUsage(
            prompt_tokens=usage_data.get("prompt_tokens", 0),
            completion_tokens=usage_data.get("completion_tokens", 0),
            total_tokens=usage_data.get("total_tokens", 0),
            cache_read_tokens=usage_data.get("cache_read_input_tokens", 0),
            cache_write_tokens=usage_data.get("cache_creation_input_tokens", 0),
        )

        return ChatResponse(
            content=content,
            tool_calls=tool_calls,
            usage=usage,
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
    ) -> AsyncGenerator[StreamEvent, None]:
        model = self._model(model)
        payload: dict = {
            "model": model,
            "messages": messages,
            "max_tokens": min(max_tokens, 16000),
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
        if temperature is not None:
            payload["temperature"] = temperature

        client = self._get_client()
        done_sent = False
        # Track the last finish_reason we observed across chunks so the DONE
        # event can carry it to the agent loop (which uses it to detect
        # max_tokens truncation and trigger an in-turn continuation).
        last_finish_reason: str | None = None
        # Index-based accumulator for streaming tool_call deltas.
        # OpenAI streaming sends tool_calls incrementally: first chunk has id+name,
        # subsequent chunks append argument fragments. We collect by index and
        # only yield complete ToolCalls on finish_reason or [DONE].
        tc_accumulator: dict[int, dict] = {}

        def _flush_tool_calls():
            """Convert accumulated tool call fragments into ToolCall objects."""
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
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    raw = line[6:].strip()
                    if raw == "[DONE]":
                        # Flush any remaining accumulated tool calls
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

                    # OpenRouter can return 200 with error body in streaming
                    chunk_error = chunk.get("error")
                    if chunk_error:
                        err_msg = (
                            chunk_error.get("message", str(chunk_error))
                            if isinstance(chunk_error, dict)
                            else str(chunk_error)
                        )
                        logger.error("OpenRouter stream error in chunk: %s", err_msg)
                        # Context overflow should propagate as FailoverError for retry
                        from core.llm.errors import FailoverError, FailoverReason, classify_http_error

                        reason = classify_http_error(400, err_msg)
                        if reason == FailoverReason.CONTEXT_OVERFLOW:
                            raise FailoverError(reason, err_msg)
                        yield StreamEvent(type=StreamEventType.ERROR, error=err_msg)
                        break

                    # Usage
                    usage_data = chunk.get("usage")
                    if usage_data:
                        yield StreamEvent(
                            type=StreamEventType.USAGE,
                            usage=TokenUsage(
                                prompt_tokens=usage_data.get("prompt_tokens", 0),
                                completion_tokens=usage_data.get("completion_tokens", 0),
                                total_tokens=usage_data.get("total_tokens", 0),
                                cache_read_tokens=usage_data.get("cache_read_input_tokens", 0),
                                cache_write_tokens=usage_data.get("cache_creation_input_tokens", 0),
                            ),
                        )

                    choices = chunk.get("choices", [])
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {})
                    finish_reason = choices[0].get("finish_reason")
                    if finish_reason:
                        last_finish_reason = finish_reason

                    # Accumulate tool call deltas by index
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

                    # Flush accumulated tool calls when the model signals completion
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
            logger.error("OpenRouter stream error: %s", e)
            yield StreamEvent(type=StreamEventType.ERROR, error=str(e))
        finally:
            try:
                # Safety flush for any un-yielded tool calls
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
            logger.warning("Failed to fetch OpenRouter models: %s", e)
            self._models_cache = []
        return self._models_cache

    def clear_models_cache(self) -> None:
        """Invalidate the cached model list so next fetch hits the API."""
        self._models_cache = None

    async def get_model_info(self, model: str) -> ModelInfo:
        model = self._model(model)
        models = await self._fetch_models()
        for m in models:
            if m.get("id") == model:
                arch = m.get("architecture", {})
                ctx = m.get("context_length", 128_000)
                modality = arch.get("modality", "")
                return ModelInfo(
                    id=model,
                    provider=self.name,
                    context_length=ctx,
                    supports_vision="image" in modality,
                    max_output_tokens=m.get("top_provider", {}).get("max_completion_tokens"),
                )
        return ModelInfo(id=model, provider=self.name, context_length=128_000)

    async def list_models(self) -> list[ModelInfo]:
        models_data = await self._fetch_models()
        # Filter to configured models if any
        allowed = set(settings.openrouter_models) if settings.openrouter_models else None
        results = []
        for m in models_data:
            mid = m.get("id", "")
            if allowed and mid not in allowed:
                continue
            arch = m.get("architecture", {})
            results.append(
                ModelInfo(
                    id=mid,
                    provider=self.name,
                    context_length=m.get("context_length", 128_000),
                    supports_vision="image" in arch.get("modality", ""),
                )
            )
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
