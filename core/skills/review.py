"""Pernix — the "N skill proposals wait for your decision" rollup (review.pending).

Most skill proposals decide themselves: with auto-apply on (the default)
they apply after the window, so a bell item per proposal would be a receipt,
not a request. What the bell carries is the residue auto-apply refuses —
the items that sit until a human clicks. This module counts exactly those
and keeps ONE coalescing bell row in step with the count.

Counted: pending skill_improvement_proposals auto-apply will not take — ALL
of them when skill_proposal_auto_apply is off, otherwise those that
``_validate_for_auto_apply`` skips (too large, low confidence, written as an
instruction to an editor, a near-duplicate heading, or past the prompt
limit). A disabled skill and the per-skill monthly cap
are not counted — they clear on their own.

Until 3.2 the rollup also counted canary and adaptive proposals; both queues
went with the adaptive layer. Cheap by construction: one bounded list query.
No LLM.
"""

from __future__ import annotations

import logging

from config import settings
from core import notices
from db import models as db

logger = logging.getLogger("pernix.skills")

SUBJECT = "proposals"
_LINK = {"kind": "tab", "tab": "skills"}
# The body last written to the open row. The category coalesces, but a
# coalesced repeat marks the row unread again — re-announcing an unchanged
# count every idle pass would turn one quiet item into a nightly nag.
_LAST_KEY = "review_pending_last_body"


def _skill_needs_human(prop: dict) -> bool:
    from core.skills.proposals import _validate_for_auto_apply

    if not settings.skill_proposal_auto_apply:
        return True
    try:
        reason = _validate_for_auto_apply(prop)
    except Exception:
        return True
    # Transient refusals clear on their own: a disabled skill when it is
    # re-enabled, the per-skill monthly cap when the month rolls over.
    transient = "disabled" in (reason or "") or "in 30 days" in (reason or "")
    return bool(reason) and reason.startswith("skip:") and not transient


def count_review_pending() -> int:
    """Pending skill proposals that wait for a human and never auto-apply."""
    return sum(1 for prop in db.list_skill_proposals(status="pending", limit=500) if _skill_needs_human(prop))


def _body(n: int) -> str:
    why = (
        "auto-apply is off"
        if not settings.skill_proposal_auto_apply
        else "auto-apply's checks refused them; open each to see why"
    )
    return f"None of these apply on their own.\n• {n} skill proposal(s) — {why}"


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
