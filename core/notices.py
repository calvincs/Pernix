"""Pernix — the one place that decides what a notification IS.

Every notification used to be a row in the bell, written by whichever of ~40
call sites had something to say, each choosing its own urgency. Measured over
17 days that was ~104 bell items (about 6 a day); ~95% were receipts from
self-maintenance that already has its own tab, and the only items that tried
to buzz the phone were reflect verdicts on synthetic canary sessions.

Producers now call `notify(category, ...)`. The CATEGORY (registered below)
carries one tier, so urgency stops being a per-call-site guess:

  interrupt  the system needs the user, or must be told something: bell badge,
             live OS notification, Web Push / webhook.
  bell       worth a look, never a buzz: a quiet item and a dot on the bell.
             Many auto-resolve when their cause clears (see `resolve`).
  log        activity log only — read it when you want, never a badge.
  drop       not recorded at all.

Rules the callers rely on:
- `notify()` NEVER raises and never calls an LLM. A failed notification must
  not mask the error path that raised it, and a notice must never be able to
  trigger another notice (only `interrupt` reaches the event bus, and nothing
  subscribes to the bus except push/webhook delivery).
- A category's tier can differ by SESSION TYPE (a canary session's reflect
  verdict is a log line; a normal session's is an interrupt) and can be
  overridden per area or per category in `settings.notify_tier_overrides`.
- `settings.notify_tiers_enabled = False` is the kill switch: every category
  behaves like before v42 — a bell row with its legacy urgency, emitted on the
  same channels its old call site used.

`link` is a small dict the client turns into an "open" button:
  {"kind": "session", "id": <session_id>}   or   {"kind": "tab", "tab": "learning"|"canary"|"skills"|"dream"|"jobs"|"mcp"|"settings"}
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from config import settings

logger = logging.getLogger("pernix.notices")

TIERS = ("interrupt", "bell", "log", "drop")
_TIER_URGENCY = {"interrupt": "high", "bell": "normal", "log": "low"}


@dataclass(frozen=True)
class Category:
    tier: str
    # Session types whose tier differs from `tier` (normal sessions use `tier`).
    by_session_type: dict = field(default_factory=dict)
    # A repeat of an OPEN (category, subject) row updates it (x N) instead of stacking.
    coalesce: bool = False
    # Once per UTC day per (category, subject), via the notify_dedup markers.
    dedup_daily: bool = False
    # Kill-switch behaviour: the urgency the old call site wrote, and which
    # channels it reached: "none" (bell row only), "sse" (open browsers), "push".
    legacy_urgency: str = "normal"
    legacy_emit: str = "none"


# Canary runs are synthetic (the Canary tab records each outcome) and a worker
# reports to its orchestrator, so their reflect/timeout/stream-error alerts are
# not recorded at all; a background (snooze) session's go to the activity log.
_NOT_FOR_SYNTHETIC = {"canary": "drop", "worker": "drop", "snooze": "log"}

CATEGORIES: dict[str, Category] = {
    # --- the agent talking to the user -------------------------------------
    "agent.notify_user": Category("bell", legacy_emit="push"),
    "agent.notify_user_urgent": Category("interrupt", legacy_urgency="high", legacy_emit="push"),
    "external.message": Category("bell", legacy_emit="push"),
    "external.message_urgent": Category("interrupt", legacy_urgency="high", legacy_emit="push"),
    # --- sessions: the turn stopped, or ended badly ------------------------
    "sessions.reflect_attention": Category(
        "interrupt", {**_NOT_FOR_SYNTHETIC, "cron": "bell"}, legacy_urgency="high", legacy_emit="push"
    ),
    "sessions.reflect_followup": Category("bell", _NOT_FOR_SYNTHETIC, legacy_urgency="high", legacy_emit="push"),
    "sessions.timeout": Category(
        "interrupt", {**_NOT_FOR_SYNTHETIC, "cron": "bell"}, legacy_urgency="high", legacy_emit="push"
    ),
    "sessions.goal_budget": Category("interrupt", legacy_urgency="high", legacy_emit="sse"),
    "sessions.stream_error": Category("interrupt", _NOT_FOR_SYNTHETIC, legacy_urgency="high"),
    # --- scheduled jobs -----------------------------------------------------
    "jobs.failed": Category("interrupt", legacy_urgency="high", legacy_emit="push"),
    "jobs.uncertain_after_restart": Category("bell", legacy_urgency="high"),
    "jobs.test_passed": Category("log", legacy_emit="sse"),
    "jobs.test_failed": Category("bell", legacy_urgency="high", legacy_emit="sse"),
    # --- canary suite (synthetic tests; the Canary tab is the surface) -----
    "canary.contaminated": Category("log"),
    "canary.probe_retired": Category("log"),
    "canary.parked": Category("bell", coalesce=True),
    "canary.suite_chronic": Category("log"),
    "canary.suite_unhealthy": Category("bell", coalesce=True, legacy_urgency="high"),
    "canary.maintenance": Category("log", dedup_daily=True),
    "canary.auto_admitted": Category("log"),
    "canary.stale": Category("log"),
    # --- skills --------------------------------------------------------------
    "skills.verify_unsafe": Category("bell"),
    "skills.auto_rolled_back": Category("bell", legacy_urgency="high"),
    "skills.rolled_back": Category("log"),
    "skills.proposals_auto_applied": Category("log"),
    # --- adaptive layer (the Learning tab is the surface) -------------------
    "adaptive.queue_full": Category("log", dedup_daily=True),
    "adaptive.cap_reached": Category("log", dedup_daily=True),
    "adaptive.proposal_held": Category("log"),
    "adaptive.tripwire_suspect": Category("bell", coalesce=True, legacy_urgency="high"),
    "adaptive.tripwire_rolled_back": Category("bell", legacy_urgency="high"),
    "adaptive.auto_approved": Category("log"),
    "adaptive.edits_applied": Category("log"),
    "adaptive.value_sweep": Category("log", dedup_daily=True),
    "adaptive.lint_sweep": Category("log", dedup_daily=True),
    "adaptive.trial_sweep": Category("log", dedup_daily=True),
    "adaptive.cleanup": Category("log"),
    # One computed row: "N proposals wait for your decision". Replaces a bell
    # item per proposal; resolved when the count reaches zero.
    "review.pending": Category("bell", coalesce=True),
    # --- dream ---------------------------------------------------------------
    "dream.corrections_applied": Category("log", dedup_daily=True),
    "dream.queue_stalled": Category("log"),
    "dream.promotion_stalled": Category("log"),
    # --- spaces ---------------------------------------------------------------
    "spaces.suggested": Category("log"),
    # --- system health --------------------------------------------------------
    "system.fallback_burn": Category("interrupt", legacy_urgency="high"),
    "system.embeddings_down": Category("bell", coalesce=True),
    "system.embeddings_switched": Category("log"),
    "system.embeddings_recovered": Category("log"),
    "system.mcp_down": Category("bell", coalesce=True),
    "system.tavily_key": Category("bell"),
    "system.tavily_limit": Category("bell"),
    "system.tool_quarantined": Category("bell"),
    "system.memory_oversized": Category("bell"),
    "system.push_rejected": Category("bell"),
}

_UNKNOWN = Category("bell")
_warned_unknown: set[str] = set()


def area_of(category: str) -> str:
    """The area is the part before the dot: 'canary.parked' -> 'canary'."""
    return category.split(".", 1)[0] if "." in category else category


def resolve_tier(category: str, session_type: str | None = None) -> str:
    """Effective tier: registry default, then the session-type map, then the
    user's override (a category override beats an area override)."""
    cat = CATEGORIES.get(category, _UNKNOWN)
    tier = cat.tier
    if session_type and session_type in cat.by_session_type:
        tier = cat.by_session_type[session_type]
    overrides = getattr(settings, "notify_tier_overrides", None) or {}
    for key in (category, area_of(category)):
        wanted = overrides.get(key)
        if wanted in TIERS:
            return wanted
    return tier


def notify(
    category: str,
    title: str,
    body: str = "",
    *,
    session_id: str = "",
    subject: str = "",
    link: dict | None = None,
    session_type: str | None = None,
    dedup_key: str | None = None,
) -> str:
    """Record one notification according to its category's tier.

    Returns the row id, or "" when it was dropped, deduplicated, or failed.
    Never raises.
    """
    try:
        return _notify(category, title, body, session_id, subject, link, session_type, dedup_key)
    except Exception as e:  # a notice must never break the path that raised it
        logger.debug("notify(%s) failed: %s", category, e)
        return ""


def _notify(category, title, body, session_id, subject, link, session_type, dedup_key) -> str:
    from db import models as db

    cat = CATEGORIES.get(category)
    if cat is None:
        cat = _UNKNOWN
        if category not in _warned_unknown:
            _warned_unknown.add(category)
            logger.warning("notify(): unregistered category %r — treated as a bell item", category)

    if session_type is None and session_id and cat.by_session_type:
        try:
            session_type = (db.get_session(session_id) or {}).get("session_type")
        except Exception:
            session_type = None

    killed = not getattr(settings, "notify_tiers_enabled", True)
    if killed:
        tier, urgency = "bell", cat.legacy_urgency
        event_tier = "interrupt" if cat.legacy_emit == "push" else "bell"
        emit_sse, emit_bus = cat.legacy_emit in ("sse", "push"), cat.legacy_emit == "push"
    else:
        tier = resolve_tier(category, session_type)
        if tier == "drop":
            return ""
        urgency = _TIER_URGENCY[tier]
        event_tier = tier
        emit_sse, emit_bus = tier in ("interrupt", "bell"), tier == "interrupt"

    if dedup_key is None and cat.dedup_daily:
        dedup_key = f"{category}:{subject}" if subject else category

    nid = db.add_notification(
        session_id=session_id,
        title=title,
        body=body,
        urgency=urgency,
        dedup_key=dedup_key or "",
        category=category,
        tier=tier,
        subject=subject,
        link=link,
        coalesce=cat.coalesce,
    )
    if not nid:
        return ""  # a same-day repeat — already announced

    if emit_sse or emit_bus:
        event = {
            "type": "dialog.notification",
            "notification_id": nid,
            "title": title,
            "body": body,
            "urgency": urgency,
            "source_session_id": session_id,
            "tier": event_tier,
            "category": category,
            "area": area_of(category),
        }
        try:
            from sessions.manager import get_manager

            get_manager().broadcast(event)
        except Exception as e:
            logger.debug("notify(%s): SSE broadcast failed: %s", category, e)
        if emit_bus:
            try:
                from core.events import get_event_bus

                get_event_bus().emit({**event, "session_id": session_id})
            except Exception as e:
                logger.debug("notify(%s): bus emit failed: %s", category, e)
    return nid


def resolve(category: str, subject: str = "") -> int:
    """The cause went away — close the open row(s) so they leave the bell by
    themselves. Never raises; returns how many rows it closed."""
    try:
        from db import models as db

        n = db.resolve_notifications(category, subject)
        if n:
            try:
                from sessions.manager import get_manager

                get_manager().broadcast(
                    {"type": "dialog.notification", "tier": "bell", "category": category, "resolved": True}
                )
            except Exception:
                pass
        return n
    except Exception as e:
        logger.debug("resolve(%s) failed: %s", category, e)
        return 0
