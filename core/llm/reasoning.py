"""Pernix — reasoning (thinking) tokens: echo-back to the model that wrote them.

A reasoning model served by vLLM (Inkling 2 Mini, 2026-11) returns its
reasoning in a field of its own (`reasoning`, older servers
`reasoning_content`) and expects it back on the assistant turn it belongs to,
tool-call loops included: its chat template re-renders a prior turn's
reasoning only when the client sends it. Sending it anywhere else is a
different matter. api.openai.com and OpenRouter define no such request field,
and a fallback to another model must not choke on one, so the echo is narrow:
the self-hosted OpenAI-compatible provider, and only reasoning produced by the
model being called.

Compiled messages carry two extra keys on assistant entries that have stored
reasoning: REASONING_KEY (the text) and REASONING_MODEL_KEY (the model that
produced it). Every provider passes its messages through `wire_messages()` or
`strip_reasoning()` before they leave the process, so neither key reaches a
server that has not been checked for it.
"""

from __future__ import annotations

import logging

from config import settings

logger = logging.getLogger("pernix.llm.reasoning")

REASONING_KEY = "reasoning"
REASONING_MODEL_KEY = "reasoning_model"


def is_self_hosted_openai(base_url: str | None) -> bool:
    """True for an OpenAI-compatible server that is not api.openai.com."""
    base = (base_url or "").strip()
    return bool(base) and "api.openai.com" not in base


def echo_enabled(model: str) -> bool:
    """Whether stored reasoning should ride along on requests to `model`.

    Asked by the compiler, so that the reasoning it attaches is also the
    reasoning it counts against the budget. The provider applies the same
    rule again at dispatch (wire_messages), which is what actually decides.
    """
    if not model:
        return False
    try:
        from core.llm.client import get_llm_client

        if get_llm_client().resolve_provider(model) != "openai":
            return False
    except Exception:
        return False
    return is_self_hosted_openai(settings.openai_base_url)


def strip_reasoning(messages: list[dict]) -> list[dict]:
    """Messages without either reasoning key, for servers that define neither."""
    if not any(isinstance(m, dict) and (REASONING_KEY in m or REASONING_MODEL_KEY in m) for m in messages):
        return messages
    out = []
    for m in messages:
        if isinstance(m, dict) and (REASONING_KEY in m or REASONING_MODEL_KEY in m):
            m = {k: v for k, v in m.items() if k not in (REASONING_KEY, REASONING_MODEL_KEY)}
        out.append(m)
    return out


def wire_messages(messages: list[dict], model: str) -> list[dict]:
    """Messages for a self-hosted server that renders prior reasoning.

    Keeps `reasoning` on an assistant message only when the same model
    produced it — another model's chain of thought is not this model's
    context, and replaying it as if it were reads as the model's own words —
    and always drops the provenance key, which no server knows.
    """
    if not any(isinstance(m, dict) and (REASONING_KEY in m or REASONING_MODEL_KEY in m) for m in messages):
        return messages
    out = []
    for m in messages:
        if not isinstance(m, dict) or (REASONING_KEY not in m and REASONING_MODEL_KEY not in m):
            out.append(m)
            continue
        text = m.get(REASONING_KEY)
        source = m.get(REASONING_MODEL_KEY)
        clean = {k: v for k, v in m.items() if k not in (REASONING_KEY, REASONING_MODEL_KEY)}
        if m.get("role") == "assistant" and isinstance(text, str) and text and source == model:
            clean[REASONING_KEY] = text
        out.append(clean)
    return out
