"""Pernix — best-effort JSON extraction from background-model output.

Background activities (memory file-split, dream hypothesize, telos soup) ask
the Background model for machine-readable JSON and parse it with ad-hoc
fence-stripping. On the live box the qwen3.8 MTP tag broke all three at once:
its output sometimes arrives wrapped in prose or fences, and sometimes the
engine early-stops mid-generation, leaving `[` or a cut-off object (a known
MTP-variant failure mode — the same model family truncates to 1-72 tokens in
other engines too). The recoverable shapes belong in one extractor instead of
three parsers; the truncated shapes are unrecoverable by parsing and are the
reason call sites retry once.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger("pernix.llm.jsonx")

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
# A fence with no closing ``` (truncated output) still yields its body: the
# trailing ``` is optional, so the balanced scan below gets a look at whatever
# JSON made it out before the cut.
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)(?:```|\Z)", re.DOTALL)


def _balanced_slice(text: str) -> str | None:
    """The first balanced top-level {...} or [...] in mixed prose, or None.

    A real scanner rather than a regex: bracket characters inside JSON
    strings must not count, so string/escape state is tracked.
    """
    start = -1
    for i, ch in enumerate(text):
        if ch in "{[":
            start = i
            break
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None  # never closed — truncated output


def extract_json(text: str | None) -> Any | None:
    """Parse the JSON payload out of LLM output. None when nothing parses.

    Tolerates, in order of attempt: exact JSON; JSON inside markdown fences
    (closed or truncated-open); a JSON object/array embedded in surrounding
    prose or reasoning text. <think> blocks are stripped first. A truncated
    payload stays None — the caller decides whether to retry the call.
    """
    text = (text or "").strip()
    if not text:
        return None
    text = _THINK_RE.sub("", text).strip()

    candidates: list[str] = [text]
    candidates += [m.group(1).strip() for m in _FENCE_RE.finditer(text)]
    sliced = _balanced_slice(text)
    if sliced:
        candidates.append(sliced)

    for candidate in candidates:
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    return None
