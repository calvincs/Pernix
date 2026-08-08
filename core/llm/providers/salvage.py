"""Pernix — recovery of tool calls that a provider handed back as plain text.

Some served models emit their *native* tool-call markup into the content
stream instead of the structured `tool_calls` field: the chat template's
parser fails to match the special tokens, or the model degrades under loop
pressure and stops using the API shape at all. The call is right there in the
text; only the framing is wrong.

This lives in the provider layer rather than in the agent loop because the
degradation is a property of a *model family's wire format*, not of the
agent's control flow. It is deliberately not folded into a single adapter:
kimi arrives through OpenRouter today and could arrive through Ollama or any
OpenAI-compatible endpoint tomorrow, and DeepSeek's DSML shape leaks from
several hosts — putting the parser inside one adapter would leave the rest
unguarded. `salvage_tool_calls` is the one seam; adding a vendor means adding
a parser to `_PARSERS`, not touching core/agent.py.

Salvage is best-effort and structurally conservative: a candidate is only
promoted when it parses cleanly, and (for the generic form) names a tool the
live registry actually has.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass

# Kimi K2.6 emits its native special-token tool-call format as plain text when
# it degrades under loop pressure instead of using the structured API format.
# These patterns let us recover those calls rather than discarding them.
_KIMI_SECTION_RE = re.compile(
    r"<\|tool_calls_section_begin\|>.*?<\|tool_calls_section_end\|>",
    re.DOTALL,
)
_KIMI_CALL_RE = re.compile(
    r"<\|tool_call_begin\|>\s*(\S+?)(?::(\w+))?\s*<\|tool_call_argument_begin\|>(.*?)<\|tool_call_end\|>",
    re.DOTALL,
)

# Generic XML-style tool-call salvage. Catches model-emitted markup that
# leaks as text when the provider's chat-template parser fails to match the
# real special tokens. Covers DeepSeek's DSML format and any future
# Anthropic-XML-shaped degradation. The captured prefix group (\1) lets the
# closing tag's decoration match the opening tag's. Parameters are matched
# with a re.escape'd prefix at call time to avoid regex-injection.
_GENERIC_INVOKE_RE = re.compile(
    r'<([^\s<>/]*?)invoke\s+name="([^"]+)"\s*>(.*?)</\1invoke>',
    re.DOTALL,
)
_GENERIC_PARAM_RE_TMPL = r'<{prefix}parameter\s+name="([^"]+)"(?:\s+[^>]*)?\s*>(.*?)</{prefix}parameter>'


@dataclass(frozen=True)
class SalvagedCalls:
    """Tool calls recovered from a text response, plus the cleaned content.

    `content` is the original text with the recovered markup removed, so the
    caller can persist it as the assistant message without the leaked tokens.
    `summary` is a ready-to-log description of what was recovered and how.
    """

    tool_calls: list[dict]
    content: str
    format: str
    summary: str


def salvage_tool_calls(content: str, tool_exists: Callable[[str], bool]) -> SalvagedCalls | None:
    """Recover tool calls a model wrote as text. None when there are none.

    `tool_exists` is the live registry predicate — the generic XML parser uses
    it to refuse prose that merely looks like markup. Callers should only
    reach here when the structured `tool_calls` field came back empty; a
    provider that framed the call correctly is always authoritative.
    """
    if not content:
        return None
    for parser in _PARSERS:
        recovered = parser(content, tool_exists)
        if recovered is not None:
            return recovered
    return None


def _salvage_kimi(content: str, _tool_exists: Callable[[str], bool]) -> SalvagedCalls | None:
    if "<|tool_call_begin|>" not in content:
        return None
    recovered: list[dict] = []
    for m in _KIMI_CALL_RE.finditer(content):
        name_raw, call_id, args_raw = m.group(1), m.group(2), m.group(3).strip()
        recovered.append(
            {
                "id": f"kimi_{call_id}" if call_id else f"kimi_{len(recovered)}",
                "name": name_raw.strip(),
                "arguments": args_raw,
            }
        )
    if not recovered:
        return None
    return SalvagedCalls(
        tool_calls=recovered,
        content=_KIMI_SECTION_RE.sub("", content).strip(),
        format="kimi",
        summary=f"recovered {len(recovered)} Kimi native-format tool call(s) from text content",
    )


def _salvage_xml_invoke(content: str, tool_exists: Callable[[str], bool]) -> SalvagedCalls | None:
    if "invoke" not in content:
        return None
    recovered: list[dict] = []
    spans_to_strip: list[tuple[int, int]] = []
    first_prefix: str | None = None
    for inv in _GENERIC_INVOKE_RE.finditer(content):
        prefix, name, body = inv.group(1), inv.group(2).strip(), inv.group(3)
        if not tool_exists(name):
            continue
        param_re = re.compile(
            _GENERIC_PARAM_RE_TMPL.format(prefix=re.escape(prefix)),
            re.DOTALL,
        )
        param_matches = list(param_re.finditer(body))
        if not param_matches:
            # Structural minimum: at least one matched-prefix parameter.
            continue
        params: dict[str, str] = {}
        for p in param_matches:
            params[p.group(1)] = p.group(2).strip()
        recovered.append(
            {
                "id": f"salvage_{len(recovered)}",
                "name": name,
                "arguments": json.dumps(params),
            }
        )
        spans_to_strip.append(inv.span())
        if first_prefix is None:
            first_prefix = prefix
    if not recovered:
        return None
    cleaned = content
    for start, end in reversed(spans_to_strip):
        cleaned = cleaned[:start] + cleaned[end:]
    # Drop a now-empty outer container like <…tool_calls></…tool_calls>.
    cleaned = re.sub(
        r"<[^\s<>/]*?tool_calls>\s*</[^\s<>/]*?tool_calls>",
        "",
        cleaned,
    ).strip()
    return SalvagedCalls(
        tool_calls=recovered,
        content=cleaned,
        format="xml-invoke",
        summary=(
            f"recovered {len(recovered)} native-format tool call(s) from text content "
            f"(markup leaked as text; first prefix={first_prefix!r})"
        ),
    )


# Ordered: the vendor-specific token format is checked before the generic XML
# shape, matching how the agent loop used to try them.
_PARSERS = (_salvage_kimi, _salvage_xml_invoke)
