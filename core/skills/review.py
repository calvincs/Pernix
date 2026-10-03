"""Pernix — the "N skill proposals wait for your decision" rollup (review.pending).

Most skill proposals decide themselves: they have a veto window and apply on
their own after it, so a bell item per proposal was a receipt, not a
request. What the bell should carry is the residue the clock will NEVER take
— the items that sit until a human clicks. This module counts exactly those
and keeps ONE coalescing bell row in step with the count.

Counted: pending skill_improvement_proposals the veto window will never
apply — ALL of them when skill_proposal_auto_apply_after_hours <= 0,
otherwise those failing a STATIC machine check (change over
AUTO_APPLY_MAX_CHANGE_CHARS, confidence below 0.6 or unparseable). Transient
skips (a disabled skill) are not counted — they may clear on their own.

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
    from core.skills.proposals import AUTO_APPLY_MAX_CHANGE_CHARS

    if settings.skill_proposal_auto_apply_after_hours <= 0:
        return True
    if len((prop.get("proposed_change") or "").strip()) > AUTO_APPLY_MAX_CHANGE_CHARS:
        return True
    try:
        return float(prop.get("confidence") or 0.0) < 0.6
    except (TypeError, ValueError):
        return True


def count_review_pending() -> int:
    """Pending skill proposals that wait for a human and never auto-apply."""
    return sum(1 for prop in db.list_skill_proposals(status="pending", limit=500) if _skill_needs_human(prop))


def _body(n: int) -> str:
    why = (
        "the veto window is off"
        if settings.skill_proposal_auto_apply_after_hours <= 0
        else "too large or too low-confidence to apply unattended"
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
