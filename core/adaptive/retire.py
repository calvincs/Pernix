"""Pernix — Adaptive entry retirement: producer-agnostic plumbing.

`adaptive_max_entries_per_kind` (12) is a live-state cap. Candor has always
retired its own hints when the tool recovered (`core/snooze.py`), so its
slots cycle. Dream and Telos minted and never retired, so once `routing_hint`
filled, every later insight was rejected at apply time and the loop looked
identical to a loop with nothing to say.

This module holds only what every producer needs — recovering an entry's
originating evidence, its age, and building the delete edit. The "does the
evidence still hold?" predicate is producer-specific knowledge and stays in
the producer's own package (`core/dream/retire.py`, `core/telos/retire.py`).

A producer deleting its OWN entry is same-producer, so it stays low-risk:
the cross-producer escalation in `compute_risk` is what guards against one
subsystem silently unpublishing another's work.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from db import models as db

logger = logging.getLogger("pernix.adaptive")


def creating_evidence(entry_id: str) -> list[str]:
    """Evidence refs recorded on the event that created this entry.

    `adaptive_entries` keeps no evidence column — the audit chain lives in
    `adaptive_events`. The creating event is the oldest one for the entry.
    """
    events = db.adaptive_list_events(entry_id=entry_id, limit=50)
    for ev in reversed(events):  # list is newest-first
        if ev.get("action") != "create":
            continue
        try:
            refs = json.loads(ev.get("evidence_json") or "[]")
        except (TypeError, ValueError):
            return []
        return [str(r) for r in refs if r]
    return []


def entry_age_days(row: dict) -> float | None:
    """Days since the entry was created. None when the stamp is unreadable —
    an unparseable timestamp must never be read as "infinitely old" and
    retire an entry the sweep cannot actually vouch for.
    """
    raw = row.get("created_at") or ""
    if not raw:
        return None
    try:
        created = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created).total_seconds() / 86400.0


def retire_edit(row: dict, reason: str) -> dict:
    """The delete edit for one entry, version-fenced against concurrent edits."""
    return {
        "action": "delete",
        "kind": row["kind"],
        "scope": row.get("scope") or "global",
        "entry_id": row["id"],
        "baseline_version": row["version"],
        "evidence": [f"retired: {reason}"],
    }


def producer_entries(producer: str, kinds: tuple[str, ...]) -> list[dict]:
    """Active entries of the given kinds that this producer authored."""
    out: list[dict] = []
    for kind in kinds:
        out.extend(r for r in db.adaptive_list_entries(kind=kind) if r.get("source") == producer)
    return out
