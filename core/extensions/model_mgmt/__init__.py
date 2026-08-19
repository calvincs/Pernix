"""Pernix — Model management extension: list, call, switch models."""

from __future__ import annotations

import asyncio
import logging

from config import settings

logger = logging.getLogger("pernix.ext.model")

# Error substrings that mean "the requested model id is wrong / unavailable"
# rather than a transient failure. When call_model hits one of these, we append
# the real model list so the agent stops guessing ids (observed loop: 6
# consecutive call_model 404s on hallucinated Qwen ids).
_BAD_MODEL_ERR = ("404", "400", "not found", "no endpoints", "no allowed providers", "not a valid model")

# Error classes for call_model failures. A `fatal` error (bad model id) cannot
# be fixed by retrying — with the same model or a fallback — so the fallback
# is never dispatched for it. Everything else is `transient`: empty body,
# timeout, 5xx, connection failure.
_FATAL = "fatal"
_TRANSIENT = "transient"


def _dispatch_chat(client, loop, model: str, messages: list) -> tuple[str, str | None]:
    """One chat attempt. Returns (text, None) on success or (reason, class)
    on failure, where class is `_FATAL` or `_TRANSIENT`.

    A 200 with an empty body is a failure here, not a success: a provider
    returning no content (observed with kimi-k2.5, 2026-08-19) used to come
    back as "[model via provider]\\n" — success-shaped nothing the agent
    could not distinguish from an answer.
    """
    try:
        future = asyncio.run_coroutine_threadsafe(client.chat(messages, model=model, max_tokens=4096), loop)
        resp = future.result(timeout=120)
    except Exception as e:
        msg = str(e) or e.__class__.__name__
        cls = _FATAL if any(tok in msg.lower() for tok in _BAD_MODEL_ERR) else _TRANSIENT
        return f"calling {model} failed — {msg}", cls
    if not (resp.content or "").strip():
        return f"calling {model} returned an empty response", _TRANSIENT
    return f"[{resp.model} via {resp.provider}]\n{resp.content}", None


def _available_model_ids(client, loop, cap: int = 30) -> str:
    """Best-effort one-line hint of callable model ids for error messages."""
    if loop is None:
        return "Call list_available_models to see valid ids."
    try:
        future = asyncio.run_coroutine_threadsafe(client.list_models(), loop)
        models = future.result(timeout=15)
    except Exception:
        return "Call list_available_models to see valid ids."
    ids = [m.id for m in (models or [])][:cap]
    if not ids:
        return "Call list_available_models to see valid ids."
    return "Available models: " + ", ".join(ids) + " — pass one of these ids exactly, or use list_available_models."


def list_available_models(_context: dict | None = None) -> str:
    """List all available models across providers."""
    from core.llm.client import get_llm_client

    client = get_llm_client()

    try:
        ctx = _context or {}
        loop = ctx.get("_loop") or asyncio.get_running_loop()
        future = asyncio.run_coroutine_threadsafe(client.list_models(), loop)
        models = future.result(timeout=30)
    except Exception as e:
        return f"Error listing models: {e}"

    if not models:
        return "No models available."

    lines = [f"Current: {settings.llm_model}", ""]
    for m in models:
        vision = " [vision]" if m.supports_vision else ""
        lines.append(f"- {m.id} ({m.provider}, ctx={m.context_length:,}{vision})")
    return "\n".join(lines)


def call_model(
    model: str,
    prompt: str,
    system: str = "",
    image_path: str = "",
    fallback_model: str = "",
    _context: dict | None = None,
) -> str:
    """Make a one-shot call to a specific model. Supports vision via image_path.

    `fallback_model` is dispatched once when the primary fails transiently
    (empty body, timeout, connection); a fatal failure (bad model id) returns
    immediately — no fallback could fix it.
    """
    from core.llm.client import get_llm_client

    client = get_llm_client()
    ctx = _context or {}
    try:
        loop = ctx.get("_loop") or asyncio.get_running_loop()
    except RuntimeError:
        loop = None  # no running loop (e.g. sync unit test) — chat call below handles it

    # Validate/canonicalize the model up front so a bad id returns an actionable
    # list instead of an opaque provider error. Mirrors switch_model: an id with
    # no "/" that the registry doesn't know can't be OpenRouter and Ollama
    # doesn't have it either — reject early with the valid options. Ids that
    # look like OpenRouter ("vendor/model") are allowed through and validated by
    # the provider (handled in the except below).
    try:
        resolved = client.router.registry.resolve_model_id(model)
        info = client.router.registry.get_model_info(resolved)
        if info is not None:
            model = resolved
        elif client.router.registry.resolve_provider(resolved) != "openrouter":
            # "Error:" prefix so the executor flags this was_error (stuck
            # detection keys on it); a bare "Model not found" reads as success.
            return f"Error: model '{model}' not found. {_available_model_ids(client, loop)}"
    except Exception:
        pass  # best-effort — fall through and let the provider decide

    messages = []
    if system:
        messages.append({"role": "system", "content": system})

    if image_path:
        import base64
        from pathlib import Path

        # Resolve from workspace first, then try absolute
        img = Path(settings.workspace_dir) / image_path
        if not img.exists():
            img = Path(image_path)
        if not img.exists():
            return f"Error: Image not found: {image_path}"

        img_b64 = base64.b64encode(img.read_bytes()).decode()
        ext = img.suffix.lower().lstrip(".")
        mime = {
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "png": "image/png",
            "gif": "image/gif",
            "webp": "image/webp",
        }.get(ext, "image/jpeg")

        messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_b64}"}},
                ],
            }
        )
    else:
        messages.append({"role": "user", "content": prompt})

    text, err_class = _dispatch_chat(client, loop, model, messages)
    if err_class is None:
        return text
    # "Error:" prefix so the executor records was_error (feeds stuck
    # detection). A model-not-found/invalid error is unrecoverable by
    # retrying with a different guess — surface the real options so the
    # agent self-corrects instead of looping on hallucinated ids.
    if err_class == _FATAL:
        return f"Error: {text} [{_FATAL}]\n{_available_model_ids(client, loop)}"
    if fallback_model and fallback_model != model:
        fb_text, fb_class = _dispatch_chat(client, loop, fallback_model, messages)
        if fb_class is None:
            return f"(primary {text} [{_TRANSIENT}]; answered by fallback {fallback_model})\n{fb_text}"
        return f"Error: {text} [{_TRANSIENT}]; fallback {fb_text} [{fb_class}]"
    return f"Error: {text} [{_TRANSIENT}]"


def switch_model(model: str, reason: str = "", scope: str = "turn", _context: dict | None = None) -> str:
    """Switch the active LLM model.

    scope="turn" (default): temporary — the previous model is restored after
    the current agent turn completes.  scope="session": persists as the
    session's model override until the session ends or the model is switched
    again.  User-initiated switches (via the API) are permanent.
    """
    scope = (scope or "turn").strip().lower()
    if scope not in ("turn", "session"):
        return f"Invalid scope '{scope}': must be 'turn' or 'session'."
    # Resolve the session to apply per-session override instead of mutating global state.
    session_id = (_context or {}).get("session_id", "")
    session = None
    if session_id:
        try:
            from sessions.manager import get_manager

            session = get_manager().get(session_id)
        except Exception:
            pass

    # Refresh registry first so resolve_model_id has current data, then
    # canonicalize the requested model ID (e.g. "claude-haiku-4.5" →
    # "anthropic/claude-haiku-4.5") and validate it actually exists.
    client = None
    loop = None
    try:
        from core.llm.client import get_llm_client

        client = get_llm_client()
        ctx = _context or {}
        loop = ctx.get("_loop") or asyncio.get_running_loop()

        try:
            future = asyncio.run_coroutine_threadsafe(client.refresh_registry(), loop)
            future.result(timeout=30)
        except Exception:
            pass  # Non-critical — registry will fall back to heuristic

        resolved = client.router.registry.resolve_model_id(model)
        model_info = client.router.registry.get_model_info(resolved)
        if model_info is None:
            # Model not in registry. If it has no "/" it can't be OpenRouter and
            # Ollama doesn't know it either — return a helpful error.
            provider_guess = client.router.registry.resolve_provider(resolved)
            if provider_guess != "openrouter":
                return (
                    f"Model '{model}' not found in the model registry. "
                    f"Use list_available_models to see available options."
                )
            # Has "/" — might be a valid OpenRouter model not in the whitelist;
            # allow it through and let OpenRouter reject it if invalid.
        else:
            model = resolved  # use canonical ID
    except Exception:
        pass  # If resolution fails, proceed with raw name (best-effort)

    if session is not None:
        # Per-session switch — no global mutation.
        old_override = session.model_override
        if scope == "session":
            # Persistent switch: cancel any pending turn-end restore so the
            # manager doesn't revert this override when the turn completes.
            session._model_before_agent_switch = None
            session._budget_before_agent_switch = None
        elif session._model_before_agent_switch is None:
            # Save previous override on first switch; "" is a sentinel for "no override was set".
            session._model_before_agent_switch = old_override if old_override is not None else ""
            session._budget_before_agent_switch = (
                session.context_budget_override if session.context_budget_override is not None else -1
            )
        old_display = old_override or settings.llm_model
        session.model_override = model
    else:
        # Fallback for edge cases where no session context is available.
        old_display = settings.llm_model
        settings.llm_model = model

    # Query context length and scope the new budget to the session (not global)
    # so concurrent sessions on different-sized models don't clobber each other.
    new_budget = (
        session.context_budget_override
        if session is not None and session.context_budget_override
        else settings.context_budget
    )
    try:
        if client and loop:
            future = asyncio.run_coroutine_threadsafe(client.get_model_info(model), loop)
            info = future.result(timeout=30)
            new_budget = int(info.context_length * 0.9)
    except Exception:
        pass

    if session is not None:
        session.context_budget_override = new_budget
    # No-session fallback: deliberately no global settings.context_budget
    # mutation — per-session derivation at turn start (core/llm/budget)
    # already picks up the new model's window, and the setting is the
    # user's manual fallback, not a scratch variable.

    if session is not None and scope == "session":
        durability = "persists for the rest of this session"
    else:
        durability = "temporary for this turn"
    return f"Switched from {old_display} to {model} ({durability}, budget: {new_budget:,} tokens)"


def register(reg) -> None:
    common = {"category": "model", "source": "extension"}
    tags = ["model", "llm", "switch", "provider", "ai"]

    reg.register(
        name="list_available_models",
        func=list_available_models,
        description="List all available LLM models across providers with capabilities.",
        parameters={"type": "object", "properties": {}},
        tags=tags + ["list", "available"],
        timeout=30,
        parallel_safe=True,
        **common,
    )
    reg.register(
        name="call_model",
        func=call_model,
        description="Make a one-shot call to a specific model. Supports vision via image_path for multimodal models.",
        parameters={
            "type": "object",
            "properties": {
                "model": {"type": "string", "description": "Model ID"},
                "prompt": {"type": "string", "description": "Prompt text"},
                "system": {"type": "string", "description": "System prompt (optional)"},
                "image_path": {"type": "string", "description": "Path to image file in workspace (for vision models)"},
                "fallback_model": {
                    "type": "string",
                    "description": "Model ID to try once if the primary fails transiently "
                    "(empty response, timeout). Not dispatched on a bad model id.",
                },
            },
            "required": ["model", "prompt"],
        },
        tags=tags + ["call", "query", "ask", "vision", "image"],
        timeout=300,
        parallel_safe=False,
        safety_level="safe",
        **common,
    )
    reg.register(
        name="switch_model",
        func=switch_model,
        description=(
            "Switch the active LLM model. By default the switch is temporary and reverts when the "
            "current turn ends; pass scope='session' to keep it for the rest of the session (e.g. when "
            "the user asks to switch models for the session/conversation). Adjusts context budget to "
            "the new model's capacity."
        ),
        parameters={
            "type": "object",
            "properties": {
                "model": {"type": "string", "description": "Model ID to switch to"},
                "reason": {"type": "string", "description": "Why switching"},
                "scope": {
                    "type": "string",
                    "enum": ["turn", "session"],
                    "description": "'turn' (default): revert to the previous model when this turn ends. "
                    "'session': keep the new model for the rest of the session.",
                },
            },
            "required": ["model"],
        },
        tags=tags + ["switch", "change", "use"],
        timeout=60,
        parallel_safe=False,
        safety_level="safe",
        **common,
    )
