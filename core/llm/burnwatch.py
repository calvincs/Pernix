"""Pernix — Fallback-burn watch: is the backup tier silently carrying the load?

The 2026-08-19 incident: the primary provider lost its API key on a container
recreate, every chat call silently failed over to the PAID fallback model, and
nothing surfaced it until a human grepped the logs days later. The signature
is unambiguous in token_usage: the fallback model's share of a window's
tokens jumps from ~0 to dominant.

This module encodes that signature as a standing check. Pure evaluation is
separated from I/O (house style — see core/synthesis.py): snooze calls
`check_fallback_burn()`, tests call `evaluate_fallback_burn()` with rows.

Watch-only by design: it mints a notification, never touches routing.
`fallback_burn_alert_share = 0` disables it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("pernix.burnwatch")

_WINDOW_HOURS = 24


def evaluate_fallback_burn(
    rows: list[dict],
    fallback_model: str,
    share_threshold: float,
    min_tokens: int,
) -> dict | None:
    """Pure check over per-model token rows ({model, total, calls}).

    Returns a finding dict when the fallback model's token share meets the
    threshold AND the window carries enough volume to matter; else None.
    """
    if not fallback_model or share_threshold <= 0:
        return None
    total = sum(int(r.get("total") or 0) for r in rows)
    if total < max(int(min_tokens), 1):
        return None
    burned = sum(int(r.get("total") or 0) for r in rows if str(r.get("model") or "") == fallback_model)
    if burned <= 0:
        return None
    share = burned / total
    if share < share_threshold:
        return None
    calls = sum(int(r.get("calls") or 0) for r in rows if str(r.get("model") or "") == fallback_model)
    return {
        "model": fallback_model,
        "share": share,
        "tokens": burned,
        "total_tokens": total,
        "calls": calls,
        "window_hours": _WINDOW_HOURS,
    }


def check_fallback_burn() -> dict | None:
    """Read the trailing window from token_usage and evaluate. Never raises."""
    from config import settings
    from db import models as db

    try:
        if not settings.fallback_model or float(settings.fallback_burn_alert_share or 0) <= 0:
            return None
        # A fallback_model that IS the primary (misconfig) would always read
        # as 100% burn; that config is its own problem, not this watch's.
        if settings.fallback_model == settings.llm_model:
            return None
        since = (datetime.now(timezone.utc) - timedelta(hours=_WINDOW_HOURS)).isoformat()
        rows = db.token_usage_by_model_since(since)
        return evaluate_fallback_burn(
            rows,
            settings.fallback_model,
            float(settings.fallback_burn_alert_share),
            int(settings.fallback_burn_min_tokens or 0),
        )
    except Exception as e:
        logger.warning("Fallback-burn check failed: %s", e)
        return None
