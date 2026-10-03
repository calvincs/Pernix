"""Pernix — User feedback as ground truth (trust-loop hardening, 2026-09-04).

A thumb on an assistant message is the only outcome in the whole loop that
nothing in the system authored. It outranks the next-message reading, which
outranks the reflect verdict — so when it lands it becomes the turn's
`outcome_source` on the post-mortem the thumb belongs to.

Until 3.2 a contradicting thumb also corrected the per-entry usefulness
counters of the adaptive layer. That layer is retired; what remains is the
stamp. `core/synthesis.attribute()` reads the stamped `user_signal` itself
when it attributes the turn, so nothing else needs correcting here.

Nothing here raises: it is called from an HTTP handler whose job is to store
the user's click, and a bookkeeping problem must never turn that into a 500.
"""

from __future__ import annotations

import logging

from db import models as db

logger = logging.getLogger("pernix.feedback")


def apply_user_signal(session_id: str, message_id, signal: str | None) -> dict:
    """Stamp a thumb on the post-mortem of the turn `message_id` belongs to.

    `signal` is "up", "down", or None to withdraw. Returns a small report —
    the post-mortem it found, plus the empty `applied` / `reversed` /
    `entries` fields older callers and logs still read. Never raises.
    """
    report: dict = {"post_mortem_id": None, "applied": {}, "reversed": {}, "entries": []}
    try:
        pm = db.set_post_mortem_user_signal(session_id, message_id, signal)
    except Exception as e:
        logger.warning("Could not stamp the user signal for %s/%s: %s", session_id, message_id, e)
        return report
    if pm:
        # An ungraded turn (or a message that belongs to none) has no
        # post-mortem; the feedback row itself is already stored, and that
        # is not an error.
        report["post_mortem_id"] = pm["id"]
    return report
