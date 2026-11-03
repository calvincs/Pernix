"""Pernix — is a user message only an acknowledgement?

"good job", "thanks!", "nice work, thank you". Such a turn carries no fact,
instruction or correction, so background passes that learn from new material
(distill, refine) can skip it. The agent still answers it normally, on the
model the user picked.

Zero tolerance by design: every word must be praise or filler. A false
negative costs one background call; a false positive would drop a fact for
good ("thanks, remember I prefer tabs"). So a typo ("well odne") fails the
check and the turn is distilled as usual.
"""

from __future__ import annotations

import re

_MAX_WORDS = 6

# The first word must be one of these. Approvals that start work ("ok",
# "yes", "sure", "go ahead") are deliberately absent.
_OPENERS = frozenset(
    {
        "thanks",
        "thank",
        "thx",
        "ty",
        "good",
        "great",
        "nice",
        "awesome",
        "perfect",
        "excellent",
        "cool",
        "love",
        "well",
        "appreciate",
        "appreciated",
        "wonderful",
        "brilliant",
        "amazing",
        "superb",
        "fantastic",
    }
)

# Every later word must be an opener or one of these.
_FILLER = frozenset(
    {
        "job",
        "done",
        "work",
        "you",
        "so",
        "much",
        "it",
        "that",
        "this",
        "looks",
        "look",
        "very",
        "really",
        "all",
        "the",
        "a",
        "again",
        "landed",
        "worked",
        "stuff",
        "one",
        "man",
        "buddy",
        "pernix",
        "lot",
        "lots",
    }
)

_WORD_RE = re.compile(r"[a-z']+")


def is_acknowledgement(message: str, *, has_attachments: bool = False) -> bool:
    """True when `message` is only thanks or praise, with nothing to act on or keep."""
    if has_attachments or not message:
        return False
    text = message.strip().lower()
    if not text or "://" in text or "?" in text or any(ch.isdigit() for ch in text):
        return False
    # Anything that is not a word, space or light punctuation (code, paths,
    # quotes, colons) means there is content.
    if re.search(r"[^a-z'\s,.!]", text):
        return False
    words = _WORD_RE.findall(text)
    if not words or len(words) > _MAX_WORDS:
        return False
    if words[0] not in _OPENERS:
        return False
    return all(w in _OPENERS or w in _FILLER for w in words[1:])


_TRIVIAL_REPLY_CHARS = 400


def trivial_delta_reason(fresh: list[dict]) -> str:
    """Why new session rows hold nothing to learn from, or "" if they might.

    Shared by distill and refine, which both work on the rows past their own
    watermark.

    Two shapes, both seen on session 7fdaf04b6918 (four distill calls, zero
    entries): a delta of only grader/notice rows (the deferred reflect row
    lands after the turn's own distill), and a thanks turn with a short
    reply and no tool work.
    """
    talk = [m for m in fresh if m.get("role") in ("user", "assistant")]
    if not talk and not any(m.get("role") == "tool" for m in fresh):
        return "only reflect/notice rows"
    if any(m.get("role") == "tool" for m in fresh):
        return ""
    users = [m for m in talk if m.get("role") == "user"]
    if not users:
        return ""
    if not all(isinstance(m.get("content"), str) and is_acknowledgement(m["content"]) for m in users):
        return ""
    reply_chars = sum(len(m.get("content") or "") for m in talk if m.get("role") == "assistant")
    if reply_chars >= _TRIVIAL_REPLY_CHARS:
        return ""
    return "acknowledgement turn"
