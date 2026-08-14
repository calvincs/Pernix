"""Pernix — Token estimation with tiktoken (optional) and char fallback."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from core.llm.types import extract_tool_call_fields

logger = logging.getLogger("pernix.context.tokens")

# tiktoken downloads cl100k_base (1.7MB) from a Microsoft CDN on first use and
# caches it in a temp directory keyed by hash. In a container that temp
# directory is ephemeral, so every rebuild re-downloaded it — 40s on the first
# turn after a deploy, measured, on the request path, and a hard internet
# dependency for an otherwise self-contained box. Point the cache at the
# persistent data directory instead: downloaded once, ever. An operator who
# has already set TIKTOKEN_CACHE_DIR keeps their choice.
_TIKTOKEN_CACHE = Path("data/cache/tiktoken")


def _prepare_tiktoken_cache() -> None:
    if os.environ.get("TIKTOKEN_CACHE_DIR"):
        return
    try:
        _TIKTOKEN_CACHE.mkdir(parents=True, exist_ok=True)
        os.environ["TIKTOKEN_CACHE_DIR"] = str(_TIKTOKEN_CACHE.resolve())
    except Exception as e:  # read-only FS, odd permissions — fall back to the default
        logger.debug("tiktoken cache dir unavailable (%s); using the library default", e)


class TokenEstimator:
    """Estimates token counts for text and messages.

    Primary: tiktoken cl100k_base (~2% accuracy).
    Fallback: content-type-adjusted character heuristic (~15% accuracy).
    """

    def __init__(self):
        self._enc = None
        _prepare_tiktoken_cache()
        try:
            import tiktoken

            self._enc = tiktoken.get_encoding("cl100k_base")
            logger.info("Token estimator: using tiktoken cl100k_base")
        except ImportError:
            logger.info("Token estimator: tiktoken unavailable, using char heuristic")
        except Exception as e:
            # get_encoding also reaches the network on a cold cache. Only
            # ImportError was caught, so an offline box (or a CDN hiccup)
            # raised straight out of the constructor and took the turn with
            # it, when the heuristic was right there.
            logger.warning("Token estimator: tiktoken load failed (%s: %s), using char heuristic", type(e).__name__, e)

    def count(self, text: str) -> int:
        """Count tokens in a text string."""
        if not text:
            return 0
        if self._enc:
            # disallowed_special=() is load-bearing, not a tuning knob. tiktoken
            # defaults to raising on any special-token *string* ("<|endoftext|>",
            # "<|im_start|>", ...), and this counter runs over every message on
            # every compile. A tool result that merely quotes one — a HuggingFace
            # model listing, a tokenizer config, a conversation about prompts —
            # otherwise raises here and kills the turn before it starts, which
            # bricks the session permanently: the text is in the transcript, so
            # every later turn dies on the same message. Counting it as ordinary
            # text is also the more accurate answer, because providers escape
            # these sequences rather than honouring them as control tokens.
            return len(self._enc.encode(text, disallowed_special=()))
        return self._count_heuristic(text)

    def _count_heuristic(self, text: str) -> int:
        """Content-type adjusted character heuristic."""
        if not text:
            return 0
        # Code-like content has shorter tokens on average
        sample = text[:200]
        if any(
            sample.startswith(p)
            for p in (
                "{",
                "[",
                "def ",
                "class ",
                "import ",
                "from ",
                "async ",
                "function ",
                "const ",
                "let ",
                "var ",
                "<",
                "CREATE ",
                "SELECT ",
            )
        ):
            return int(len(text) / 3.3)
        return len(text) // 4

    def count_message(self, msg: dict) -> int:
        """Count tokens for a full chat message including overhead."""
        total = 4  # per-message overhead (role, separators)
        content = msg.get("content") or ""

        if isinstance(content, list):
            # Multipart content (vision)
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        total += self.count(part.get("text", ""))
                    elif part.get("type") == "image_url":
                        total += 85  # flat estimate for images
                else:
                    total += self.count(str(part))
        else:
            total += self.count(content)

        # Tool calls in assistant messages
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            if isinstance(tool_calls, str):
                total += self.count(tool_calls)
            elif isinstance(tool_calls, list):
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    _, name, args = extract_tool_call_fields(tc)
                    total += self.count(name)
                    if not isinstance(args, str):
                        args = json.dumps(args)
                    total += self.count(args)

        return total

    def count_messages(self, messages: list[dict]) -> int:
        """Count total tokens across all messages."""
        return sum(self.count_message(m) for m in messages)


# Module-level singleton
_estimator: TokenEstimator | None = None


def get_estimator() -> TokenEstimator:
    """Get or create the singleton TokenEstimator."""
    global _estimator
    if _estimator is None:
        _estimator = TokenEstimator()
    return _estimator
