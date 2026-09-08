"""Pernix — Provider router with fallback logic."""

from __future__ import annotations

import logging

import httpx

from config import settings
from core.llm.errors import FALLBACK_REASONS, FailoverError, FailoverReason, classify_http_error
from core.llm.providers._shared import describe_exception
from core.llm.providers.ollama import OllamaProvider
from core.llm.providers.openrouter import OpenRouterProvider
from core.llm.registry import ModelRegistry
from core.llm.semaphore import SessionAwareLLMScheduler
from core.llm.types import ChatResponse, HealthStatus, ModelInfo, StreamEvent, StreamEventType, extract_tool_call_fields

logger = logging.getLogger("pernix.llm.router")

# Providers that speak strict OpenAI wire format and need
# normalize_for_openrouter() applied to compiled messages. Ollama is more
# permissive and gets the raw compile output.
OPENAI_FORMAT_PROVIDERS = frozenset({"openrouter", "openai"})


def _carry_parts(content: list) -> list:
    """Re-emit multimodal parts for a DIFFERENT model, minus our own markers.

    `cache_control` is an Anthropic breakpoint attached for the primary and
    means nothing (at best) to the backup; `_kind` / `_filename` / `_b64_len`
    are the compiler's bookkeeping. Only the two wire shapes survive.
    """
    parts: list = []
    for part in content:
        if not isinstance(part, dict):
            parts.append({"type": "text", "text": str(part)})
        elif part.get("type") == "image_url":
            parts.append({"type": "image_url", "image_url": part.get("image_url")})
        else:
            parts.append({"type": "text", "text": part.get("text", "")})
    return parts


def sanitize_for_fallback(
    messages: list[dict],
    *,
    supports_tools: bool = False,
    supports_vision: bool = False,
) -> list[dict]:
    """Reshape a conversation for what the BACKUP model can actually take.

    The defaults are the historical behavior — everything degraded, which is
    right for the small text-only local model this was written for:

    - Tool-role messages → user messages with context
    - Assistant tool_calls → text description appended
    - Multimodal content → flattened to text
    - Mid-conversation system messages → removed

    But the backup is a MODEL, not a place (config.py, the three-role
    scheme): it can be another remote, and degrading a tool-capable or
    vision-capable backup throws away the very capabilities the operator
    configured it for. Stripping tool history from a backup that supports
    tools is how a tool-requiring turn came back as prose that reads like a
    finished answer. Callers pass the registry's answer for the model they
    are actually about to call.

    Mid-conversation system messages are still dropped in both modes: that
    rule is unchanged here on purpose (see the 2026-08-14 trailing-system-tail
    regression for what carrying them properly would mean).
    """
    cleaned = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content") or ""

        if isinstance(content, list):
            if supports_vision:
                content = _carry_parts(content)
            else:
                # Flatten multimodal content
                parts = []
                for part in content:
                    if isinstance(part, dict):
                        if part.get("type") == "text":
                            parts.append(part.get("text", ""))
                        elif part.get("type") == "image_url":
                            kind = part.get("_kind", "image")
                            parts.append(f"[{kind} omitted]")
                    else:
                        parts.append(str(part))
                content = "\n".join(parts)

        if role == "tool" and not supports_tools:
            # Convert tool result to user context
            tool_id = msg.get("tool_call_id", "unknown")
            cleaned.append(
                {
                    "role": "user",
                    "content": f"[Tool result from {tool_id}]: {content}",
                }
            )
        elif role == "assistant" and msg.get("tool_calls") and not supports_tools:
            # Keep content, append tool call descriptions
            tc_desc = []
            for tc in msg["tool_calls"]:
                if not isinstance(tc, dict):
                    continue
                _, name, args = extract_tool_call_fields(tc)
                name = name or "unknown"
                tc_desc.append(f"{name}({args[:100]})")
            text = content or ""
            if tc_desc:
                text += f"\n[Called tools: {', '.join(tc_desc)}]"
            cleaned.append({"role": "assistant", "content": text})
        elif role == "system" and cleaned:
            # Drop mid-conversation system messages
            continue
        elif role == "tool":
            carried = {"role": "tool", "content": content}
            for key in ("tool_call_id", "name"):
                if msg.get(key):
                    carried[key] = msg[key]
            cleaned.append(carried)
        elif role in ("system", "user", "assistant"):
            carried = {"role": role, "content": content}
            if supports_tools and msg.get("tool_calls"):
                carried["tool_calls"] = msg["tool_calls"]
            cleaned.append(carried)
        # else: drop any non-standard internal roles (eval, model_divider, etc.)

    return cleaned


class ProviderRouter:
    """Routes LLM requests to the appropriate provider with fallback."""

    def __init__(self):
        from core.llm.providers.openai import OpenAIProvider

        self._ollama = OllamaProvider()
        self._openrouter = OpenRouterProvider()
        self._openai = OpenAIProvider()
        self.registry = ModelRegistry()
        _timeout = float(settings.llm_session_timeout) if settings.llm_session_timeout > 0 else float("inf")
        self._ollama_semaphore = SessionAwareLLMScheduler(
            max_concurrent=settings.llm_max_concurrent,
            session_timeout=_timeout,
        )
        self._openrouter_semaphore = SessionAwareLLMScheduler(
            max_concurrent=settings.openrouter_max_concurrent,
            session_timeout=_timeout,
        )
        self._openai_semaphore = SessionAwareLLMScheduler(
            max_concurrent=settings.openai_max_concurrent,
            session_timeout=_timeout,
        )
        # Name-keyed maps are the canonical structure; the attributes above
        # remain as aliases for tests/diagnostics that reach in directly.
        self._providers = {
            "ollama": self._ollama,
            "openrouter": self._openrouter,
            "openai": self._openai,
        }
        self._semaphores = {
            "ollama": self._ollama_semaphore,
            "openrouter": self._openrouter_semaphore,
            "openai": self._openai_semaphore,
        }
        self._warned_downgrades: set[str] = set()

    def _fallback_eligible(self, provider) -> bool:
        """Only a remote primary has somewhere to fail over from."""
        return getattr(provider, "name", "ollama") != "ollama"

    def _backup_capabilities(self, model: str) -> tuple[bool | None, bool]:
        """(supports_tools, supports_vision) for the backup, per the registry.

        supports_tools is None when the registry has never heard of the model.
        The two unknowns resolve in opposite directions on purpose: tools are
        KEPT (the stream ladder hands the fallback its tools unconditionally,
        and an operator who configured this backup asked for it), while images
        are dropped, because an image part sent to a text-only model is a hard
        400 rather than a soft degrade.
        """
        try:
            info = self.registry.get_model_info(self.registry.resolve_model_id(model))
        except Exception:  # a stubbed or half-built registry must not break failover
            info = None
        if info is None:
            return None, False
        return bool(getattr(info, "supports_tools", True)), bool(getattr(info, "supports_vision", False))

    def _backup_max_output(self, model: str, asked):
        """Output-token request for the BACKUP model.

        `asked` was derived for the PRIMARY (LLMClient.chat calls
        derive_max_output before the router ever sees the request), and
        handing a 32k reservation to a backup that tops out lower is how a
        failover 400s on a number the caller never chose. Same rule as
        derive_max_output — the registry's cap under settings.max_tokens —
        read off THIS router's registry rather than the process singleton's,
        and never above what the caller asked for.
        """
        try:
            derived = 0
            if settings.context_auto:
                info = self.registry.get_model_info(self.registry.resolve_model_id(model))
                derived = int(getattr(info, "max_output_tokens", 0) or 0) if info else 0
            limit = min(derived, settings.max_tokens) if derived > 0 else settings.max_tokens
            return min(asked, limit) if asked else limit
        except Exception:
            return asked

    def _can_fall_back(self, provider, model: str = "", tools=None) -> bool:
        """Router-level failover needs a remote primary AND a usable backup.

        Without a configured fallback model there is nowhere to go, and the
        right answer is the provider's own classified error — the one the
        ladder's backoff keys on — not a synthetic "no fallback" message that
        matches no retryable marker and turns a 503 into a hard stop.

        A backup equal to the primary is a retry of the failing model wearing
        a failover's clothes; the stream ladder refuses that case and so does
        this one. A backup the registry says cannot take tools is refused for
        a tool-carrying request: answering it with text the caller cannot tell
        from a finished answer is worse than surfacing the primary's error.
        """
        backup = settings.fallback_model
        if not backup or not self._fallback_eligible(provider):
            return False
        if model and backup == model:
            return False
        if tools and self._backup_capabilities(backup)[0] is False:
            logger.warning(
                "Not failing over to backup model %s: it cannot use tools and this request needs them — "
                "returning the primary's error instead of a text-only answer",
                backup,
            )
            return False
        return True

    @staticmethod
    def _http_failover(provider, e: httpx.HTTPStatusError) -> tuple[FailoverReason, FailoverError]:
        """Classify an httpx status error and build the typed error whose text
        names the status ("openrouter 503: ...") so the ladder can retry it."""
        body = e.response.text if hasattr(e.response, "text") else ""
        status = e.response.status_code
        reason = classify_http_error(status, body)
        return reason, FailoverError(reason, f"{provider.name} {status}: {body[:500]}", original=e)

    @staticmethod
    def _connect_failover(provider, e: httpx.ConnectError) -> FailoverError:
        # describe_exception keeps the class name in the text: str(ConnectError)
        # can be empty, and "ConnectError" is the ladder's retry marker.
        return FailoverError(FailoverReason.UNKNOWN, f"{provider.name} {describe_exception(e)}", original=e)

    def get_provider(self, model: str = ""):
        """Select provider using the model registry."""
        model = model or settings.llm_model
        provider_name = self.registry.resolve_provider(model)
        provider = self._providers.get(provider_name)
        if provider is not None and provider_name != "ollama":
            if provider.available:
                return provider
            # This downgrade used to be silent, and it hid a whole outage:
            # every call for the model detoured to Ollama, 404'd there, and
            # failed over to the paid remote — with nothing in the log tying
            # cause to effect (2026-08-19, lost OPENAI_API_KEY). Warn once
            # per provider+model; availability is static within a process.
            key = f"{provider_name}:{model}"
            if key not in self._warned_downgrades:
                self._warned_downgrades.add(key)
                logger.warning(
                    "Provider '%s' resolved for model '%s' but reports unavailable "
                    "(missing API key?) — downgrading to Ollama, which likely cannot serve it",
                    provider_name,
                    model,
                )
        return self._ollama

    def get_semaphore(self, provider=None) -> SessionAwareLLMScheduler:
        """Return the semaphore for a provider instance."""
        return self._semaphores.get(getattr(provider, "name", "ollama"), self._ollama_semaphore)

    @property
    def semaphore_stats(self) -> dict:
        """Combined semaphore stats for diagnostics."""
        stats: dict = {
            "available": sum(s.available for s in self._semaphores.values()),
            "waiting": sum(s.waiting for s in self._semaphores.values()),
            "capacity": sum(s.capacity for s in self._semaphores.values()),
        }
        for name, sem in self._semaphores.items():
            stats[name] = sem.stats
        return stats

    def resolve_provider(self, model: str = "") -> str:
        """Return provider name ('ollama', 'openrouter', 'openai') for a model."""
        model = model or settings.llm_model
        return self.registry.resolve_provider(model)

    def _remote_providers(self) -> list:
        return [p for name, p in self._providers.items() if name != "ollama"]

    async def populate_registry(self) -> None:
        """Populate the model registry from provider APIs."""
        await self.registry.populate(self._ollama, *self._remote_providers())

    async def refresh_registry(self) -> None:
        """Re-populate the model registry (e.g. after model switch)."""
        await self.registry.refresh(self._ollama, *self._remote_providers())

    def _pop_session_kwargs(self, kwargs: dict) -> tuple[str, float, int]:
        """Extract and remove scheduling kwargs; returns (session_id, created_at, priority)."""
        from core.llm.semaphore import PRIORITY_BACKGROUND

        session_id = kwargs.pop("_session_id", "")
        session_created_at = kwargs.pop("_session_created_at", float("inf"))
        session_priority = kwargs.pop("_session_priority", PRIORITY_BACKGROUND)
        return session_id, session_created_at, session_priority

    async def chat(self, messages: list[dict], **kwargs) -> ChatResponse:
        """Route chat with per-provider semaphore and fallback on transient errors."""
        sid, s_at, s_pri = self._pop_session_kwargs(kwargs)
        model = kwargs.get("model", "") or settings.llm_model
        provider = self.get_provider(model)
        sem = self.get_semaphore(provider)

        await sem.acquire(session_id=sid, session_created_at=s_at, priority=s_pri)
        released = False
        try:
            return await provider.chat(messages, **kwargs)
        except FailoverError as fe:
            if fe.reason in FALLBACK_REASONS and self._can_fall_back(provider, model, kwargs.get("tools")):
                sem.release()
                released = True
                return await self._fallback_chat(messages, sid, s_at, s_pri, **kwargs)
            raise
        except httpx.HTTPStatusError as e:
            reason, typed = self._http_failover(provider, e)
            if reason in FALLBACK_REASONS and self._can_fall_back(provider, model, kwargs.get("tools")):
                sem.release()
                released = True
                return await self._fallback_chat(messages, sid, s_at, s_pri, **kwargs)
            raise typed from e
        except httpx.ConnectError as e:
            if self._can_fall_back(provider, model, kwargs.get("tools")):
                sem.release()
                released = True
                return await self._fallback_chat(messages, sid, s_at, s_pri, **kwargs)
            raise self._connect_failover(provider, e) from e
        finally:
            if not released:
                sem.release()

    async def chat_stream(self, messages: list[dict], **kwargs):
        """Route streaming chat with a per-provider semaphore.

        Streaming failover belongs to ONE layer, and it is not this one: every
        caller of this generator reaches it through stream_with_failover, which
        knows the model it is switching to — it re-derives the output cap,
        re-normalizes and re-breakpoints the compiled messages for the backup's
        provider, keeps the tools, tells the UI (stream.reset / stream.fallback)
        and reports the model that actually answered. The router used to
        intercept the same errors one level lower and hand them to Ollama with
        the tools removed, so the ladder never learned a substitution had
        happened and booked the round against the primary. Typed errors now
        propagate; the ladder decides.
        """
        sid, s_at, s_pri = self._pop_session_kwargs(kwargs)
        model = kwargs.get("model", "") or settings.llm_model
        provider = self.get_provider(model)
        sem = self.get_semaphore(provider)

        await sem.acquire(session_id=sid, session_created_at=s_at, priority=s_pri)
        # Restarting is only safe before the first token reaches the caller,
        # and only the layer that can tell the UI to discard the partial may
        # do it. Past that point this generator reports the failure as an
        # ERROR event on the stream it already started rather than raising
        # into a caller that would treat it as a request that never ran.
        emitted_output = False
        try:
            async for event in provider.chat_stream(messages, **kwargs):
                if event.type in (StreamEventType.TOKEN, StreamEventType.TOOL_CALL):
                    emitted_output = True
                yield event
        except FailoverError as fe:
            if emitted_output and fe.reason in FALLBACK_REASONS:
                logger.error(
                    "%s %s after partial stream — reporting on the open stream", provider.name, fe.reason.value
                )
                yield StreamEvent(type=StreamEventType.ERROR, error=fe.message)
            else:
                raise  # let the ladder handle the typed error
        except httpx.HTTPStatusError as e:
            reason, typed = self._http_failover(provider, e)
            if emitted_output:
                logger.error("%s %s after partial stream — reporting on the open stream", provider.name, reason.value)
                yield StreamEvent(type=StreamEventType.ERROR, error=typed.message)
            else:
                raise typed from e
        except httpx.ConnectError as e:
            typed = self._connect_failover(provider, e)
            if emitted_output:
                yield StreamEvent(type=StreamEventType.ERROR, error=typed.message)
            else:
                raise typed from e
        finally:
            sem.release()

    async def _fallback_chat(
        self,
        messages: list[dict],
        session_id: str,
        session_created_at: float,
        session_priority: int,
        **kwargs,
    ) -> ChatResponse:
        """Re-run a failed one-shot request on the configured BACKUP model.

        Streaming has the ladder; `chat()` mostly does not — twenty call sites
        reach LLMClient.chat with nothing above them — so the router keeps
        owning this half. It just has to do it honestly: the backup is a model
        name that resolves through the same registry as any other, so it goes
        to ITS provider, waits on ITS semaphore, gets an output cap derived for
        ITSELF, and keeps the tools and images it is capable of. This used to
        call Ollama unconditionally with `tools` popped, which sent a remote
        backup to a local daemon that had never heard of it and turned a
        tool-requiring request into prose the caller read as a finished answer.
        """
        fallback_model = settings.fallback_model
        if not fallback_model:
            raise RuntimeError("No fallback model configured")

        provider = self.get_provider(fallback_model)
        sem = self.get_semaphore(provider)
        supports_tools, supports_vision = self._backup_capabilities(fallback_model)

        logger.warning(
            "Falling back from %s to backup model %s on provider %s",
            kwargs.get("model") or settings.llm_model,
            fallback_model,
            getattr(provider, "name", "?"),
        )
        clean = sanitize_for_fallback(
            messages,
            supports_tools=supports_tools is not False,
            supports_vision=supports_vision,
        )
        kwargs["model"] = fallback_model
        kwargs["max_tokens"] = self._backup_max_output(fallback_model, kwargs.get("max_tokens"))
        if supports_tools is False:
            kwargs.pop("tools", None)

        await sem.acquire(
            session_id=session_id,
            session_created_at=session_created_at,
            priority=session_priority,
        )
        try:
            response = await provider.chat(clean, **kwargs)
        except Exception as e:
            # chat_with_backup retries the backup itself when a call raises.
            # Mark the exception so the backup is not billed for the same
            # failure twice — the router has already spent that attempt.
            try:
                e._pernix_backup_attempted = True
            except Exception:
                pass
            raise
        finally:
            sem.release()

        # Accounting reads these. A response that came back nameless would be
        # booked against whatever the caller asked for — the primary.
        if not getattr(response, "model", ""):
            response.model = fallback_model
        if not getattr(response, "provider", ""):
            response.provider = getattr(provider, "name", "")
        return response

    async def get_model_info(self, model: str = "") -> ModelInfo:
        return await self.get_provider(model).get_model_info(model or settings.llm_model)

    async def list_all_models(self) -> list[ModelInfo]:
        """List models from all available providers.

        Ollama is always queried live (local, fast) so newly-pulled models
        appear in the dropdown without restarting. OpenRouter uses the cached
        registry to avoid a remote API call on every settings open.
        """
        try:
            ollama_live = await self._ollama.list_models()
        except Exception:
            ollama_live = []

        if self.registry.populated:
            cached_or = {m.id: m for m in self.registry.all_models() if m.provider != "ollama"}
        elif self._openrouter.available:
            cached_or = {m.id: m for m in await self._openrouter.list_models()}
        else:
            cached_or = {}

        whitelist = set(settings.openrouter_models or []) | set(settings.openai_models or [])
        result: dict[str, ModelInfo] = dict(cached_or)
        for m in ollama_live:
            if m.id in result and m.id in whitelist:
                continue  # user explicitly wants this model from OpenRouter
            result[m.id] = m  # Ollama wins on collision
        return list(result.values())

    async def check_all_health(self) -> dict[str, HealthStatus]:
        """Check health of all providers."""
        results = {}
        results["ollama"] = await self._ollama.check_health()
        for name, provider in self._providers.items():
            if name != "ollama" and provider.available:
                results[name] = await provider.check_health()
        return results

    async def close(self) -> None:
        for provider in self._providers.values():
            await provider.close()
