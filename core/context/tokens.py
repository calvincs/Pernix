"""Pernix — Token estimation with tiktoken (optional) and char fallback."""

from __future__ import annotations

import json
import logging

from core.llm.types import extract_tool_call_fields

logger = logging.getLogger("pernix.context.tokens")


class TokenEstimator:
    """Estimates token counts for text and messages.

    Primary: tiktoken cl100k_base (~2% accuracy).
    Fallback: content-type-adjusted character heuristic (~15% accuracy).
    """

    def __init__(self):
        self._enc = None
        try:
            import tiktoken

            self._enc = tiktoken.get_encoding("cl100k_base")
            logger.info("Token estimator: using tiktoken cl100k_base")
        except ImportError:
            logger.info("Token estimator: tiktoken unavailable, using char heuristic")

    def count(self, text: str) -> int:
        """Count tokens in a text string."""
        if not text:
            return 0
        if self._enc:
            return len(self._enc.encode(text))
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

    def count_tool_schemas(self, tools: list[dict]) -> int:
        """Count tokens for tool schema definitions."""
        if not tools:
            return 0
        return self.count(json.dumps(tools))


# Module-level singleton
_estimator: TokenEstimator | None = None


def get_estimator() -> TokenEstimator:
    """Get or create the singleton TokenEstimator."""
    global _estimator
    if _estimator is None:
        _estimator = TokenEstimator()
    return _estimator


def estimate_tokens(text: str) -> int:
    """Convenience: estimate tokens for a string."""
    return get_estimator().count(text)
