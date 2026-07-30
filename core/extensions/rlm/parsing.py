"""Extraction of ```repl``` blocks and iteration formatting.

Adapted from the Recursive Language Models reference implementation
(https://github.com/alexzhang13/rlm, MIT License, Copyright (c) 2025 Alex Zhang).
"""

import re

from core.extensions.rlm.types import CellResult

# Per-block cap on REPL output fed back to the root model (upstream value).
MAX_CELL_OUTPUT_CHARS = 20_000

_BLOCK_RE = re.compile(r"```repl\s*\n(.*?)\n```", re.DOTALL)


def find_code_blocks(text: str) -> list[str]:
    return [m.group(1).strip() for m in _BLOCK_RE.finditer(text)]


def format_cell_output(result: CellResult) -> str:
    parts = []
    if result.stdout:
        parts.append(result.stdout.rstrip("\n"))
    if result.stderr:
        parts.append(result.stderr.rstrip("\n"))
    if result.var_names:
        parts.append(f"REPL variables: {result.var_names}")
    return "\n\n".join(parts) if parts else "No output"


def format_iteration(response: str, cells: list[CellResult]) -> list[dict[str, str]]:
    """One iteration -> messages for the next root prompt.

    Always the assistant response; plus, when code ran, a single user message
    concatenating every block's (individually truncated) output — keeping the
    per-turn shape assistant-then-user even for multi-block responses.
    """
    messages = [{"role": "assistant", "content": response}]
    parts = []
    multi = len(cells) > 1
    for i, cell in enumerate(cells):
        out = format_cell_output(cell)
        if len(out) > MAX_CELL_OUTPUT_CHARS:
            out = out[:MAX_CELL_OUTPUT_CHARS] + f"... + [{len(out) - MAX_CELL_OUTPUT_CHARS} chars truncated]"
        header = f"REPL output (block {i + 1}):" if multi else "REPL output:"
        parts.append(f"{header}\n{out}")
    if parts:
        messages.append({"role": "user", "content": "\n\n".join(parts)})
    return messages
