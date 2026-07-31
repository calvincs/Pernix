"""Pernix — Dream journal: the snooze/dream thread of thought as a session.

A day-keyed session (session_type="snooze") that appears in the session
list the way cron sessions do. Lines are role="notice" messages: rendered
by the existing UI notice branch, excluded from messages_fts (only
user/assistant/tool are indexed) so journal narration can never surface in
scout's cross-session search, and invisible to distillation (the selector
requires >= 4 user/assistant messages). The journal is a mirror of the
process, never an input to it.

Two writers:
- run_journal_listener() — a job-bus subscriber narrating every
  snooze.start / snooze.activity / snooze.done event: full ladder
  visibility with zero changes to the activity ladder itself.
- append() — rich dream detail (evidence packs, hypothesis verdicts,
  judge notes) written directly by core/dream modules.

Gated on dream_enabled at write time (live toggle): when off, the listener
idles and append() no-ops, so the add-on stays inert.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from config import settings
from db import models as db

logger = logging.getLogger("pernix.dream.journal")

_LINE_CAP = 8000


def _journal_session_id() -> str:
    """Get or create today's journal session. Sync — call via to_thread."""
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    key = f"dream_journal:{date}"
    sid = db.get_snooze_state(key)
    if sid and db.get_session(sid):
        return sid
    sid = db.create_session(title=f"Dream journal — {date}", session_type="snooze")
    # Belt and braces on top of the distill selector's user/assistant
    # message requirement: never a distillation candidate.
    db.mark_session_reviewed(sid)
    db.set_snooze_state(key, sid)
    logger.info("dream journal: opened session %s for %s", sid[:8], date)
    return sid


def append_sync(text: str) -> None:
    """Append one journal line. Never raises — the journal must never be
    the reason a snooze activity fails."""
    if not settings.dream_enabled or not text.strip():
        return
    try:
        sid = _journal_session_id()
        db.add_message(sid, "notice", text.strip()[:_LINE_CAP])
    except Exception as e:
        logger.debug("dream journal write failed: %s", e)


async def append(text: str) -> None:
    await asyncio.to_thread(append_sync, text)


async def run_journal_listener() -> None:
    """Narrate snooze bus events into the journal. Runs for process life;
    started from the app lifespan, cancelled at shutdown."""
    from core.events import get_event_bus

    bus = get_event_bus()
    q = bus.subscribe()
    logger.info("dream journal listener subscribed")
    try:
        while True:
            evt = await q.get()
            if not settings.dream_enabled or not isinstance(evt, dict):
                continue
            etype = evt.get("type", "")
            try:
                if etype == "snooze.start":
                    await append("◐ Snooze cycle started")
                elif etype == "snooze.activity":
                    await append(f"→ {evt.get('activity', '?')}: {evt.get('detail', '')}")
                elif etype == "snooze.done":
                    dur = int(evt.get("duration_ms", 0) / 1000)
                    outcome = evt.get("outcome", "")
                    suffix = f" ({outcome})" if outcome and outcome != "ran" else ""
                    await append(f"◆ Cycle complete in {dur}s{suffix}")
            except Exception as e:
                logger.debug("dream journal narration failed: %s", e)
    except asyncio.CancelledError:
        raise
    finally:
        bus.unsubscribe(q)
