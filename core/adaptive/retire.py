"""Pernix — Adaptive entry retirement: producer-agnostic plumbing + the
usage sweep.

`adaptive_max_entries_per_kind` (see config) is a live-state cap. Candor
has always retired its own hints when the tool recovered
(`core/snooze.py`), so its slots cycle. Dream and Telos minted and never
retired, so once `routing_hint` filled, every later insight was rejected at
apply time and the loop looked identical to a loop with nothing to say.

The producer-agnostic helpers here recover an entry's originating evidence,
its age, and build the delete edit; the "does the evidence still hold?"
predicate is producer-specific knowledge and stays in the producer's own
package (`core/dream/retire.py`, `core/telos/retire.py`).

`retire_unused_entries` is the value-based sweep (v3.1): entries that
rendered into prompts for `adaptive_usage_retire_days` without a single
recorded use (the `adaptive_entry` signal from scout's used_hints and
reflect's cited_policies) are retired. Retirement is a journaled
soft-delete — one click in the Adaptive tab restores any of them.

The sweep also reads the OUTCOME half of the same signal (successes/
failures attributed by synthesis): an entry with enough attributed
outcomes whose success share is failure-dominated retires even though it
is used. Usage alone kept a harmful hint alive forever — being cited
every turn was exactly what kept it in the prompt.

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


# The usage sweep's epoch: instrumentation only started counting when v3.1
# deployed, so "never used" is only meaningful for time OBSERVED. Stamped on
# the sweep's first run; nothing retires before epoch + the retire window —
# without this grace, every pre-instrumentation entry would mass-retire on
# day one for lacking a signal that did not exist yet.
_USAGE_EPOCH_KEY = "adaptive_usage_epoch"
_USAGE_SWEEP_KINDS = ("prompt_note", "routing_hint", "policy")
# Sources with their own lifecycle or their own authority. Candor retires
# its hints on recovery; a human's entry is never second-guessed by a
# counter.
_USAGE_EXEMPT_SOURCES = ("candor", "user")


def _epoch_age_days() -> float | None:
    """Days since the usage epoch; stamps it on first sight (returns 0)."""
    raw = db.get_snooze_state(_USAGE_EPOCH_KEY)
    if not raw:
        db.set_snooze_state(_USAGE_EPOCH_KEY, datetime.now(timezone.utc).isoformat())
        return 0.0
    try:
        stamp = datetime.fromisoformat(str(raw))
    except ValueError:
        return None  # unreadable stamp: sweep nothing rather than everything
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - stamp).total_seconds() / 86400.0


# The retro-lint sweep's watermark: runs once per LINT_VERSION, so the
# standing population is re-examined exactly when the lint itself changes.
# Live-box evidence (2026-08-31 agent-ergonomics audit): six dream-minted
# policies that were narrative meta-observations — "The lesson M7 … was
# violated", "The protocol … remains ineffective" — sat in the rendered
# policy slots because the v3.1 lint only ever gated NEW mints.
_LINT_SWEEP_KEY = "adaptive_lint_sweep_ver"


def retire_lint_failures() -> dict:
    """Retire active machine-authored entries that fail the current content
    lint. Returns {"retired": [...], "reasons": {}}. Runs once per
    LINT_VERSION (watermarked in snooze_state); a no-op on every later call
    until the lint changes.

    Human-authored entries are exempt for the same reason they are unlinted
    at mint time: the human is the authority the lint substitutes for.
    Deletions go through engine.delete_entry — journaled, individually
    rollbackable. The caller aggregates the notification.
    """
    from core.adaptive.lint import _LINTED_KINDS, LINT_VERSION, lint_edit

    out: dict = {"retired": [], "reasons": {}}
    if db.get_snooze_state(_LINT_SWEEP_KEY) == str(LINT_VERSION):
        return out

    from core.adaptive.engine import AdaptiveError, delete_entry

    for kind in sorted(_LINTED_KINDS):
        for r in db.adaptive_list_entries(kind=kind):
            if r.get("status") != "active" or r.get("source") == "user":
                continue
            reason = lint_edit({"action": "update", "kind": kind, "content": r.get("content")})
            if not reason:
                continue
            try:
                delete_entry(r["id"], actor="lint_sweep")
                out["retired"].append(r["id"])
                out["reasons"][r["id"]] = reason
            except AdaptiveError as e:
                logger.info("lint sweep skipped %s: %s", r["id"], e)
    # Stamp only after a complete pass, so a crash mid-sweep retries the
    # remaining entries next cycle (retiring twice is impossible — the first
    # pass already flipped their status off 'active').
    db.set_snooze_state(_LINT_SWEEP_KEY, str(LINT_VERSION))
    if out["retired"]:
        logger.info("Adaptive lint sweep retired %d entr(y/ies): %s", len(out["retired"]), ", ".join(out["retired"]))
    return out


def retire_unused_entries() -> dict:
    """Retire entries whose observed lifetime produced zero recorded uses,
    failure-dominated entries, and prompt_notes past their TTL. Returns
    {"retired": [...], "reasons": {}}.

    Every deletion goes through engine.delete_entry — journaled with a full
    snapshot, individually rollbackable. The caller aggregates the
    notification; this function never notifies.
    """
    from config import settings

    out: dict = {"retired": [], "reasons": {}}
    window = int(settings.adaptive_usage_retire_days or 0)
    note_ttl = int(settings.adaptive_prompt_note_ttl_days or 0)
    harm_min_uses = int(settings.adaptive_harmful_retire_min_uses or 0)
    harm_max_success = float(settings.adaptive_harmful_retire_max_success or 0.0)
    if window <= 0 and note_ttl <= 0 and harm_min_uses <= 0:
        return out
    epoch_age = _epoch_age_days()
    if epoch_age is None:
        return out

    from core.adaptive.engine import AdaptiveError, delete_entry

    rows: list[dict] = []
    for kind in _USAGE_SWEEP_KINDS:
        rows.extend(db.adaptive_list_entries(kind=kind))
    usage = {s["subject"]: s for s in db.get_signals_by_subjects([("adaptive_entry", r["id"]) for r in rows])}
    for r in rows:
        if r.get("status") != "active" or r.get("source") in _USAGE_EXEMPT_SOURCES:
            continue
        age = entry_age_days(r)
        if age is None:
            continue
        sig = usage.get(r["id"])
        used = bool(sig and int(sig.get("reinforcements") or 0) > 0)
        wins = int((sig or {}).get("successes") or 0)
        losses = int((sig or {}).get("failures") or 0)
        outcomes = wins + losses
        reason = ""
        # Failure-dominated check first: it applies precisely to USED
        # entries, and its reason names the evidence. No epoch/age gate —
        # the outcomes themselves are the observed window.
        if harm_min_uses > 0 and outcomes >= harm_min_uses and (wins / outcomes) < harm_max_success:
            reason = f"failure-dominated: {wins}/{outcomes} attributed outcomes succeeded"
        elif window > 0 and not used and age >= window and epoch_age >= window:
            reason = f"no recorded use in {int(age)} days of instrumented life"
        elif note_ttl > 0 and r.get("kind") == "prompt_note" and age >= note_ttl:
            # prompt_note is the kind with no producer-side retirement loop
            # at all — the TTL is its backstop even for still-used entries;
            # a note that still earns its place gets re-minted cheaply.
            reason = f"prompt_note past its {note_ttl}-day TTL"
        if not reason:
            continue
        try:
            delete_entry(r["id"], actor="usage_sweep")
            out["retired"].append(r["id"])
            out["reasons"][r["id"]] = reason
        except AdaptiveError as e:
            logger.info("usage sweep skipped %s: %s", r["id"], e)
    if out["retired"]:
        logger.info("Adaptive usage sweep retired %d entr(y/ies): %s", len(out["retired"]), ", ".join(out["retired"]))
    return out
