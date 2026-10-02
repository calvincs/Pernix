"""Pernix — the "N proposals wait for your decision" rollup (review.pending).

Most proposals decide themselves: adaptive and skill proposals have a veto
window and apply on their own after it, so a bell item per proposal was a
receipt, not a request. What the bell should carry is the residue that the
clock will NEVER take — the items that sit until a human clicks. This module
counts exactly those and keeps ONE coalescing bell row in step with the count.

Counted (and nothing else):

  canary     pending adaptive_proposals with a canary dict payload. The
             veto-window drain never takes them (invariant I6); a spec the
             allowlist could prove safe was already auto-admitted, so what
             is left here needs a human approve/reject.
  adaptive   pending non-canary adaptive_proposals that the clock will not
             take: ALL of them when the veto window is off
             (adaptive_auto_approve_after_hours <= 0), otherwise the ones
             auto-approve already HELD for citing no recorded receipt (the
             `adaptive_unfounded_notified:<id>` marker _hold_unfounded sets).
             Delete-only proposals are exempt from that hold, so a stale
             marker on one does not count. Unfounded proposals still inside
             their window are not counted yet — grading every pending row's
             evidence each pass is the cost this rollup avoids, and they are
             counted the pass after the clock holds them.
  skills     pending skill_improvement_proposals the veto window will never
             apply: ALL of them when skill_proposal_auto_apply_after_hours
             <= 0, otherwise those failing a STATIC machine check (change
             over AUTO_APPLY_MAX_CHANGE_CHARS, confidence below 0.6 or
             unparseable). Transient skips (a disabled skill) are not
             counted — they may clear on their own.

Cheap by construction: two bounded list queries plus one snooze_state point
read per pending adaptive proposal (the queue is capped at
adaptive_max_pending_proposals). No LLM.
"""

from __future__ import annotations

import logging

from config import settings
from core import notices
from db import models as db

logger = logging.getLogger("pernix.adaptive")

SUBJECT = "proposals"
_LINK = {"kind": "tab", "tab": "learning"}
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


def count_review_pending() -> dict[str, int]:
    """Per-kind counts of items that wait for a human and never auto-apply."""
    from core.adaptive.engine import _is_delete_only, _payload_of, is_canary_proposal

    counts = {"canary": 0, "adaptive": 0, "skills": 0}
    clock_off = settings.adaptive_auto_approve_after_hours <= 0
    for prop in db.adaptive_list_proposals(status="pending", limit=500):
        if is_canary_proposal(prop):
            counts["canary"] += 1
        elif clock_off:
            counts["adaptive"] += 1
        elif not _is_delete_only(_payload_of(prop)) and db.get_snooze_state(
            f"adaptive_unfounded_notified:{prop.get('id')}"
        ):
            counts["adaptive"] += 1
    for prop in db.list_skill_proposals(status="pending", limit=500):
        if _skill_needs_human(prop):
            counts["skills"] += 1
    return counts


def _body(counts: dict[str, int]) -> str:
    lines = []
    if counts["canary"]:
        lines.append(f"• {counts['canary']} canary proposal(s) — new canaries need your admit/reject")
    if counts["adaptive"]:
        why = (
            "the veto window is off"
            if settings.adaptive_auto_approve_after_hours <= 0
            else "held: their evidence cites nothing recorded"
        )
        lines.append(f"• {counts['adaptive']} adaptive proposal(s) — {why}")
    if counts["skills"]:
        why = (
            "the veto window is off"
            if settings.skill_proposal_auto_apply_after_hours <= 0
            else "too large or too low-confidence to apply unattended"
        )
        lines.append(f"• {counts['skills']} skill proposal(s) — {why}")
    return "None of these apply on their own.\n" + "\n".join(lines)


def refresh_review_pending() -> int:
    """Bring the one review.pending bell row in line with the count.

    n > 0 opens (or updates) it; n == 0 resolves it. Returns n. Never raises
    — it runs inside the idle pass and is advisory only.
    """
    try:
        counts = count_review_pending()
    except Exception as e:
        logger.debug("review.pending count failed: %s", e)
        return 0
    n = sum(counts.values())
    try:
        if n == 0:
            notices.resolve("review.pending", SUBJECT)
            db.set_snooze_state(_LAST_KEY, "")
            return 0
        body = _body(counts)
        if db.get_snooze_state(_LAST_KEY) == body:
            return n
        notices.notify(
            "review.pending",
            f"{n} proposal(s) wait for your decision",
            body,
            subject=SUBJECT,
            link=_LINK,
        )
        db.set_snooze_state(_LAST_KEY, body)
    except Exception as e:
        logger.debug("review.pending refresh failed: %s", e)
    return n
