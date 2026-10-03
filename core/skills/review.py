"""Pernix — the "N skill proposals wait for your decision" rollup (review.pending).

Skill proposals are suggestions: none applies on its own, so every pending
proposal waits for a human. A bell item per proposal would be noise; this
module keeps ONE coalescing bell row in step with the pending count.

Until 3.2 skill proposals had a veto window and applied themselves after it,
so the rollup counted only the ones that window would never take.
It also used to count canary and adaptive proposals; both queues
went with the adaptive layer. Cheap by construction: one bounded list query.
No LLM.
"""

from __future__ import annotations

import logging

from core import notices
from db import models as db

logger = logging.getLogger("pernix.skills")

SUBJECT = "proposals"
_LINK = {"kind": "tab", "tab": "skills"}
# The body last written to the open row. The category coalesces, but a
# coalesced repeat marks the row unread again — re-announcing an unchanged
# count every idle pass would turn one quiet item into a nightly nag.
_LAST_KEY = "review_pending_last_body"


def count_review_pending() -> int:
    """Pending skill proposals — every one waits for a human."""
    return len(db.list_skill_proposals(status="pending", limit=500))


def _body(n: int) -> str:
    return f"Proposals are suggestions; nothing applies until you do.\n• {n} skill proposal(s) to review"


def refresh_review_pending() -> int:
    """Bring the one review.pending bell row in line with the count.

    n > 0 opens (or updates) it; n == 0 resolves it. Returns n. Never raises
    — it runs inside the idle pass and is advisory only.
    """
    try:
        n = count_review_pending()
    except Exception as e:
        logger.debug("review.pending count failed: %s", e)
        return 0
    try:
        if n == 0:
            notices.resolve("review.pending", SUBJECT)
            db.set_snooze_state(_LAST_KEY, "")
            return 0
        body = _body(n)
        if db.get_snooze_state(_LAST_KEY) == body:
            return n
        notices.notify(
            "review.pending",
            f"{n} skill proposal(s) wait for your decision",
            body,
            subject=SUBJECT,
            link=_LINK,
        )
        db.set_snooze_state(_LAST_KEY, body)
    except Exception as e:
        logger.debug("review.pending refresh failed: %s", e)
    return n
