"""Pernix — reasoning (thinking) tokens: effort, output headroom, echo-back.

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

# The values Inkling (and vLLM's top-level `reasoning_effort`) accept. ""
# means "omit the field", which leaves the model's own default in force
# (Inkling: high).
REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

# Output tokens added on top of what a caller asked for, because max_tokens is
# shared between reasoning and the answer. Taken from the model card's
# minimum-output guidance (>= 8K for none..medium, >= 16K for high, >= 32K
# for xhigh/max); measured reasoning use is 0.3-3K typically, ~6K on code at
# xhigh and ~17K on hard planning at xhigh. Adding it, rather than flooring
# the request at it, keeps a caller's answer budget intact: a scout call that
# asks for 1,500 tokens of JSON still gets 1,500 tokens after the model has
# thought.
_HEADROOM_BY_EFFORT = {
    "none": 8_192,
    "minimal": 8_192,
    "low": 8_192,
    "medium": 8_192,
    "high": 16_384,
    "xhigh": 32_768,
    "max": 32_768,
}
_DEFAULT_EFFORT = "high"  # what the model runs at when the field is omitted

_warned_invalid: set[str] = set()


def is_self_hosted_openai(base_url: str | None) -> bool:
    """True for an OpenAI-compatible server that is not api.openai.com."""
    base = (base_url or "").strip()
    return bool(base) and "api.openai.com" not in base


def _valid(value: str, setting: str) -> str:
    value = (value or "").strip().lower()
    if not value or value in REASONING_EFFORTS:
        return value
    # settings.json is hand-editable; a typo here would 400 every request.
    if setting not in _warned_invalid:
        _warned_invalid.add(setting)
        logger.warning(
            "Setting %s=%r is not one of %s — omitting reasoning_effort", setting, value, ", ".join(REASONING_EFFORTS)
        )
    return ""


def effort_for(model: str) -> str:
    """The reasoning_effort to send for `model`, or "" to omit the field.

    Roles are told apart the way the Ollama think switch does it: the
    Background setting applies when the model is settings.background_model
    and not also the Primary. Backup follows Primary.
    """
    background = (settings.background_model or "").strip()
    primary = (settings.llm_model or "").strip()
    if model and model == background and model != primary:
        return _valid(getattr(settings, "reasoning_effort_background", ""), "reasoning_effort_background")
    return _valid(getattr(settings, "reasoning_effort", ""), "reasoning_effort")


def output_headroom(effort: str) -> int:
    """Extra output tokens to request so reasoning cannot eat the answer."""
    return _HEADROOM_BY_EFFORT.get(effort or _DEFAULT_EFFORT, _HEADROOM_BY_EFFORT[_DEFAULT_EFFORT])


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
