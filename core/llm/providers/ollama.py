"""Pernix — Ollama LLM provider adapter."""

from __future__ import annotations

import json
import logging
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

logger = logging.getLogger("pernix.llm.ollama")


class OllamaProvider:
    """Ollama adapter implementing ProviderProtocol."""

    name = "ollama"

    def __init__(self, config: ProviderConfig | None = None):
        self._config = config or ProviderConfig(
            name="ollama",
            base_url=settings.llm_base_url,
        )
        self._client: httpx.AsyncClient | None = None
        self._quick_client: httpx.AsyncClient | None = None
        self._vision_cache: dict[str, bool] = {}

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

    def _base_url(self) -> str:
        return self._config.base_url.rstrip("/")

    def _model(self, model: str) -> str:
        return model or settings.llm_model

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
        # Always use native /api/chat — it supports think=False to suppress
        # reasoning chains from thinking models, and handles all message types.
        return await self._chat_native(
            messages, tools=tools, model=model, max_tokens=max_tokens, temperature=temperature
        )

    async def _chat_native(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str = "",
        max_tokens: int = 4096,
        temperature: float | None = None,
    ) -> ChatResponse:
        """Non-streaming chat via Ollama native /api/chat (supports images)."""
        model = self._model(model)
        base = self._config.base_url.replace("/v1", "")
        native_msgs = _to_native_format(messages)

        payload: dict = {
            "model": model,
            "messages": native_msgs,
            "stream": False,
            "think": False,
            "options": {"num_predict": max_tokens},
        }
        if tools:
            payload["tools"] = tools
        if temperature is not None:
            payload["options"]["temperature"] = temperature

        client = self._get_client()
        resp = await client.post(f"{base}/api/chat", json=payload)
        resp.raise_for_status()
        data = resp.json()

        # Parse native API response format
        msg = data.get("message", {})
        content = msg.get("content") or ""
        tool_calls = None
        raw_tcs = msg.get("tool_calls")
        if raw_tcs:
            tool_calls = [
                ToolCall(
                    id=tc.get("id", f"call_{i}"),
                    name=tc.get("function", {}).get("name", ""),
                    arguments=(
                        json.dumps(tc.get("function", {}).get("arguments", {}))
                        if isinstance(tc.get("function", {}).get("arguments"), dict)
                        else tc.get("function", {}).get("arguments", "{}")
                    ),
                )
                for i, tc in enumerate(raw_tcs)
            ]

        usage = TokenUsage(
            prompt_tokens=data.get("prompt_eval_count", 0),
            completion_tokens=data.get("eval_count", 0),
            total_tokens=data.get("prompt_eval_count", 0) + data.get("eval_count", 0),
        )

        return ChatResponse(
            content=content,
            tool_calls=tool_calls,
            usage=usage,
            model=model,
            provider=self.name,
            finish_reason="tool_calls" if tool_calls else "stop",
        )

    def _parse_chat_response(self, data: dict, model: str) -> ChatResponse:
        choices = data.get("choices", [])
        if not choices:
            raise ValueError(f"No choices in response: {str(data)[:500]}")

        msg = choices[0].get("message", {})
        content = msg.get("content") or ""
        # Qwen3 puts actual response in 'reasoning' field when content is empty
        if not content and msg.get("reasoning"):
            content = msg["reasoning"]
        finish = choices[0].get("finish_reason", "stop")

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
        """Stream using Ollama native API (/api/chat) with think=false.

        The native API properly separates content from reasoning and supports
        disabling thinking mode, which the OpenAI-compat endpoint does not.
        """
        model = self._model(model)
        base = self._config.base_url.replace("/v1", "")

        # Convert to Ollama native format (arguments as dict, no id/type fields)
        native_msgs = _to_native_format(messages)

        payload: dict = {
            "model": model,
            "messages": native_msgs,
            "stream": True,
            "think": False,  # Disable reasoning/thinking mode
            "options": {"num_predict": max_tokens},
        }
        if tools:
            payload["tools"] = tools
        if temperature is not None:
            payload["options"]["temperature"] = temperature

        client = self._get_client()
        collected_tokens = 0
        done_sent = False
        done_reason: str | None = None  # captured from final chunk if present

        try:
            async with client.stream("POST", f"{base}/api/chat", json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue

                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    msg = chunk.get("message", {})

                    # Tool calls
                    raw_tcs = msg.get("tool_calls")
                    if raw_tcs:
                        tool_calls = [
                            ToolCall(
                                id=tc.get("id", f"call_{i}"),
                                name=tc.get("function", {}).get("name", ""),
                                arguments=(
                                    json.dumps(tc.get("function", {}).get("arguments", {}))
                                    if isinstance(tc.get("function", {}).get("arguments"), dict)
                                    else tc.get("function", {}).get("arguments", "{}")
                                ),
                            )
                            for i, tc in enumerate(raw_tcs)
                        ]
                        yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=tool_calls)

                    # Content
                    content = msg.get("content", "")
                    if content:
                        collected_tokens += 1
                        yield StreamEvent(type=StreamEventType.TOKEN, content=content)

                    # Done signal
                    if chunk.get("done"):
                        # Capture done_reason for length-truncation detection.
                        # Ollama emits "stop" | "length" | "load" | "unload";
                        # normalize "length" to match the OpenRouter / OpenAI
                        # vocabulary (the agent loop checks for "length").
                        done_reason = chunk.get("done_reason") or "stop"
                        # Usage from native API
                        prompt_tokens = chunk.get("prompt_eval_count", 0)
                        eval_tokens = chunk.get("eval_count", 0)
                        if prompt_tokens or eval_tokens:
                            yield StreamEvent(
                                type=StreamEventType.USAGE,
                                usage=TokenUsage(
                                    prompt_tokens=prompt_tokens,
                                    completion_tokens=eval_tokens,
                                    total_tokens=prompt_tokens + eval_tokens,
                                ),
                            )
                        else:
                            # Estimate
                            from core.context.tokens import get_estimator

                            estimator = get_estimator()
                            prompt_est = sum(estimator.count_message(m) for m in messages)
                            yield StreamEvent(
                                type=StreamEventType.USAGE,
                                usage=TokenUsage(
                                    prompt_tokens=prompt_est,
                                    completion_tokens=collected_tokens,
                                    total_tokens=prompt_est + collected_tokens,
                                ),
                            )
                        # Yield DONE with the finish reason so the agent loop
                        # can detect length-truncation and trigger a continue.
                        # Previously done_sent was set to True here but no DONE
                        # event was yielded — the finally block then skipped
                        # the yield, so consumers never saw a DONE for normal
                        # Ollama completions.
                        done_sent = True
                        yield StreamEvent(type=StreamEventType.DONE, finish_reason=done_reason)
                        break
        except GeneratorExit:
            # Caller abandoned the stream (e.g. returned/broke out of async for).
            # Cannot yield here — just clean up silently.
            return
        except Exception as e:
            logger.error("Ollama stream error: %s", e)
            yield StreamEvent(type=StreamEventType.ERROR, error=str(e))
        finally:
            if not done_sent:
                done_sent = True
                try:
                    yield StreamEvent(type=StreamEventType.DONE, finish_reason=done_reason)
                except GeneratorExit:
                    return

    # ------------------------------------------------------------------
    # Model info
    # ------------------------------------------------------------------

    async def get_model_info(self, model: str) -> ModelInfo:
        model = self._model(model)
        client = self._get_quick_client()
        # Use Ollama native API for model details
        base = self._config.base_url.replace("/v1", "")
        try:
            resp = await client.post(f"{base}/api/show", json={"name": model})
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            # Fallback: return defaults
            return ModelInfo(
                id=model,
                provider=self.name,
                context_length=128_000,
            )

        model_info = data.get("model_info", {})
        details = data.get("details", {})

        # Context length
        ctx = 128_000
        for key, val in model_info.items():
            if "context_length" in key or "num_ctx" in key:
                ctx = int(val)
                break

        # Vision detection: explicit overrides first, then modelfile key scan.
        from config import settings as _settings

        if model in _settings.vision_model_overrides:
            supports_vision = True
        else:
            supports_vision = any(
                k
                for k in model_info
                if any(v in k.lower() for v in ("clip", "projector", "vision", "mm_", "image", "modalit"))
            )
        self._vision_cache[model] = supports_vision

        return ModelInfo(
            id=model,
            provider=self.name,
            context_length=ctx,
            supports_vision=supports_vision,
        )

    async def list_models(self) -> list[ModelInfo]:
        client = self._get_quick_client()
        base = self._config.base_url.replace("/v1", "")
        try:
            resp = await client.get(f"{base}/api/tags")
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("Failed to list Ollama models: %s", e)
            return []

        models = []
        for m in data.get("models", []):
            name = m.get("name", "")
            models.append(
                ModelInfo(
                    id=name,
                    provider=self.name,
                    context_length=128_000,  # default; actual requires /api/show per model
                )
            )
        return models

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def check_health(self) -> HealthStatus:
        client = self._get_quick_client()
        base = self._config.base_url.replace("/v1", "")
        start = time.monotonic()
        try:
            resp = await client.get(f"{base}/api/tags")
            latency = int((time.monotonic() - start) * 1000)
            resp.raise_for_status()
            data = resp.json()
            return HealthStatus(
                healthy=True,
                latency_ms=latency,
                models_available=len(data.get("models", [])),
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


def _to_native_format(messages: list[dict]) -> list[dict]:
    """Convert OpenAI-format messages to Ollama native API format.

    Key differences:
    - tool_calls: arguments must be dict (not JSON string)
    - tool_calls: no 'id' or 'type' fields, just {function: {name, arguments}}
    - tool responses: no 'tool_call_id' field
    - multimodal: image_url content parts → Ollama native 'images' field (base64 list)
    """
    native = []
    for msg in messages:
        content = msg.get("content") or ""

        # Handle multimodal content (OpenAI format → Ollama native images field)
        images = []
        if isinstance(content, list):
            text_parts = []
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        text_parts.append(part.get("text", ""))
                    elif part.get("type") == "image_url":
                        url = part.get("image_url", {}).get("url", "")
                        if url.startswith("data:") and "," in url:
                            b64 = url.split(",", 1)[1]
                            if b64:
                                images.append(b64)
                else:
                    text_parts.append(str(part))
            content = "\n".join(text_parts)

        entry = {"role": msg.get("role", ""), "content": content}
        if images:
            entry["images"] = images

        # Convert assistant tool_calls to native format
        if msg.get("tool_calls") and msg["role"] == "assistant":
            tcs = msg["tool_calls"]
            if isinstance(tcs, str):
                try:
                    tcs = json.loads(tcs)
                except (json.JSONDecodeError, TypeError):
                    tcs = []

            native_tcs = []
            for tc in (tcs if isinstance(tcs, list) else []):
                func = tc.get("function", {})
                name = func.get("name", tc.get("name", ""))
                args = func.get("arguments", tc.get("arguments", "{}"))
                # Arguments must be dict, not string
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                native_tcs.append({"function": {"name": name, "arguments": args}})

            if native_tcs:
                entry["tool_calls"] = native_tcs

        # Tool responses: no tool_call_id in native format
        # (just role=tool, content=result)

        native.append(entry)
    return native
