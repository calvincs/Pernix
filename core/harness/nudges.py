"""Pernix — Mid-turn skill nudges.

Scans tool results for known failure signatures (bot-detection HTML, 4xx/5xx
from public sites, SSRF blocks on what looks like a public domain) and emits
a targeted hint pointing the agent at the skill that solves it. Each hint
fires at most once per turn per pattern so the agent doesn't get spammed.

Why not let the model figure it out? The crawl4ai-fetch skill description
literally enumerates these failure signatures, but the agent only reads the
skill description once it has *already* loaded the skill — and it won't
load the skill unless something tells it to. The harness is the natural
place to close that loop: it's already inspecting tool results for the
event stream, so a regex pass costs nothing.

A nudge is appended to the tool result text the agent sees, prefixed with
`[harness hint]:`. Provider normalization keeps tool-role messages
verbatim, so the hint survives the round trip; a synthetic role=system
message would be stripped (one-system rule + Ollama collapse).
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class NudgeRule:
    """One pattern → suggestion mapping."""

    name: str  # short id used for per-turn dedup
    pattern: re.Pattern[str]  # regex applied to the tool-result text
    suggestion: str  # appended to the tool result the agent sees
    applies_to: frozenset[str]  # tool names this rule scans (empty = any)


# Bot-detection / anti-bot wall signatures.
# Sources: crawl4ai-fetch SKILL.md "When to invoke" section.
_BOT_DETECTION_RE = re.compile(
    r"\b(?:Just a moment|Checking your browser|Please verify you are human|"
    r"Access denied|Cloudflare|enable JavaScript|please enable javascript|"
    r"DataDome|PerimeterX|Akamai)\b",
    re.IGNORECASE,
)

# HTTP error families that public web tools emit when their target IP is rate-
# limited or blocked. We deliberately don't match 4xx in general — a 404 is
# usually "you typed the wrong URL", not "you got blocked".
_HTTP_BLOCK_RE = re.compile(
    r"\b(?:HTTP[/\s]*1\.[01]?\s+)?(?:403|429|503)\b",
)

# SSRF block emitted by core/extensions/web/__init__.py:446. The user's prompt
# in turn 3 of session 42550cc17b33 hit this twice on a public-looking domain
# (developers.roku.com) because the user's split-horizon DNS resolves it to a
# private IP — and the agent had no escape hatch suggested.
#
# Loopback hostnames are excluded via negative lookahead: crawl4ai-fetch
# routes through a remote egress IP and cannot reach the agent's loopback,
# so suggesting it for `localhost`/`127.x`/`::1` blocks would mislead the
# agent into a dead-end fallback (session 444e33b3968e).
_SSRF_BLOCK_RE = re.compile(
    r"Blocked:\s+"
    r"(?!(?:localhost(?:\.localdomain)?|ip6-loopback|ip6-localhost|"
    r"127\.\d+\.\d+\.\d+|::1)\s+resolves)"
    r"\S+\s+resolves to a private/internal address",
)


CRAWL4AI_HINT = (
    "[harness hint] This page looks like it was blocked or returned bot-detection "
    "content. The crawl4ai-fetch skill routes through a headless Chromium on a "
    "different egress IP and is the standard fallback. Call "
    "load_skill('crawl4ai-fetch') and follow its instructions."
)


_RULES: tuple[NudgeRule, ...] = (
    NudgeRule(
        name="bot_detection_wall",
        pattern=_BOT_DETECTION_RE,
        suggestion=CRAWL4AI_HINT,
        applies_to=frozenset({"http_get", "browse_web"}),
    ),
    NudgeRule(
        name="http_403_429_503",
        pattern=_HTTP_BLOCK_RE,
        suggestion=CRAWL4AI_HINT,
        applies_to=frozenset({"http_get", "browse_web"}),
    ),
    NudgeRule(
        name="ssrf_private_block",
        pattern=_SSRF_BLOCK_RE,
        suggestion=CRAWL4AI_HINT,
        # SSRF block can come from any web tool path.
        applies_to=frozenset({"http_get", "browse_web", "search_web"}),
    ),
)


def evaluate(tool_name: str, content: str, fired: set[str]) -> str | None:
    """Return a hint string to append to this tool result, or None.

    Mutates `fired` so the same nudge doesn't fire twice in one turn. The
    caller owns the per-turn `fired` set — this function is otherwise
    stateless and safe to call on every tool result.
    """
    if not content:
        return None
    text = content[:8000]  # bound the regex cost on huge results
    for rule in _RULES:
        if rule.name in fired:
            continue
        if rule.applies_to and tool_name not in rule.applies_to:
            continue
        if rule.pattern.search(text):
            fired.add(rule.name)
            return rule.suggestion
    return None
