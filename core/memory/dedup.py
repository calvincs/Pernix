"""Pernix — Memory dedup primitives: the write-side guard and the recall ledger.

Two halves of the same concern, kept together so they can't drift:

- The write-side token guard (`content_tokens`, `loses_no_unique_token`) that
  every entry-destroying operation must clear before it archives or overwrites
  an entry. The dedup sweep, cross-file consolidation and the store's
  supersede path all perform the same "is dropping this safe?" judgement; it
  used to exist only inside the sweep, and consolidation's copy of the same
  merge silently lacked it.
- The per-session recall ledger, below.

When the model fans out `recall` + `search_web` (or repeats a recall later in
the same turn), the same memory entries get serialised into multiple tool
results — ~8KB of duplicate content per turn in the wild. This module
collapses repeats to a short reference line so the model still knows the
entry exists and can re-pull it explicitly via `include_seen=True`.

Identity is `(file_name, epoch)` — the same composite key the search layer
uses for cross-result dedup. We render it as `file_name@epoch` for human
readability in tool output.

The ledger lives on `AgentSession._seen_memory_keys` (in-memory, reset on
session reload). `_seen_memory_lock` makes check-and-record atomic so
parallel tool calls in the same round (via `asyncio.gather` in the executor)
don't both emit full bodies for the same entries.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.memory.search import SearchResult

logger = logging.getLogger("pernix.memory.dedup")

_WORD_RE = re.compile(r"\w+")


# ---------------------------------------------------------------------------
# Write-side containment guard
# ---------------------------------------------------------------------------


def content_tokens(text: str) -> set[str]:
    """Case-folded word-token set — the unit every write-side guard compares."""
    return set(_WORD_RE.findall(text.lower()))


def loses_no_unique_token(dropped: str, kept: str) -> bool:
    """True when retiring `dropped` in favour of `kept` destroys no information.

    A high similarity ratio is not enough on its own: structured facts that
    differ only in a key value ("prod key X / :8090" vs "dev key Y / :8091")
    score ~0.9, and archiving one loses it forever. Only retire an entry whose
    tokens are all present in the survivor. Pure rephrasings that introduce
    novel words therefore stay — false negatives are wasteful, false positives
    destroy facts, and that asymmetry is the whole point of this check.
    """
    return content_tokens(dropped) <= content_tokens(kept)


def _key(file_name: str, epoch: int) -> str:
    return f"{file_name}@{epoch}"


def _format_footer(seen_keys: list[str]) -> str:
    if not seen_keys:
        return ""
    keys = sorted(set(seen_keys))
    head = "— Already surfaced in this session " "(call recall(..., include_seen=True) to re-pull full text):"
    if len(keys) <= 3:
        return f"{head} {', '.join(keys)}"
    return head + "\n  " + "\n  ".join(keys)


def partition_seen(
    results: list[SearchResult],
    session_id: str,
) -> tuple[list[SearchResult], list[str], str]:
    """Split results into (new, seen_keys, footer_text) against the session ledger.

    New results' keys are recorded in the ledger before return. Calls with
    `session_id=""` or an unknown session id are no-ops (returns input
    unchanged, empty seen, empty footer) so the helper is safe to call from
    contexts that may not have a live session.

    Atomic: the check-and-record loop holds `session._seen_memory_lock`, so
    parallel callers in the same round can't both classify the same key as
    "new".
    """
    if not session_id or not results:
        return list(results), [], ""

    try:
        from sessions.manager import get_manager

        session = get_manager().get(session_id)
    except Exception as e:
        logger.debug("partition_seen: session lookup failed (%s); pass-through", e)
        return list(results), [], ""

    if session is None:
        return list(results), [], ""

    ledger: set = getattr(session, "_seen_memory_keys", None)
    lock = getattr(session, "_seen_memory_lock", None)
    if ledger is None or lock is None:
        # Older session object without ledger fields — pass-through.
        return list(results), [], ""

    new_results: list[SearchResult] = []
    seen_keys: list[str] = []

    with lock:
        for r in results:
            entry = getattr(r, "entry", None)
            if entry is None:
                # Defensive: shouldn't happen, but if a result lacks an entry
                # we can't key it — treat as always-new (pass through).
                new_results.append(r)
                continue
            file_name = getattr(entry, "file_name", "") or ""
            epoch = getattr(entry, "epoch", None)
            if not file_name or epoch is None:
                new_results.append(r)
                continue
            k = _key(file_name, epoch)
            if k in ledger:
                seen_keys.append(k)
            else:
                ledger.add(k)
                new_results.append(r)

    return new_results, seen_keys, _format_footer(seen_keys)
