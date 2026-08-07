"""Pernix — Adaptive Layer engine: validate → queue → apply → rollback.

The lifecycle (plan §6c):

  producer pass ──queue_edits()──► low-risk  → adaptive_batches (pending)
                                   high-risk → adaptive_proposals (pending)
  snooze Activity 15 (idle) ──drain_pending()──► apply_batch() per batch
  human approve ──approve_proposal()──► apply_batch() (same engine, own batch)
  tripwire/human ──rollback()──► reverse events by autoincrement id

Plan/apply split: producers carry each touched entry's version as baseline;
apply re-reads and rejects moved entries while the rest of the batch lands.
Rollback restores before_json snapshots byte-for-byte (no version bump) and
hard-deletes entries whose creating event has no `before`.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone

from config import settings
from db import models as db

logger = logging.getLogger("pernix.adaptive")

KINDS = frozenset({"prompt_note", "routing_hint", "policy", "worker_spec"})
ACTIONS = frozenset({"create", "update", "delete"})
SOURCES = frozenset({"refine", "dream", "candor", "telos", "user", "agent"})
HIGH_RISK_KINDS = frozenset({"policy", "worker_spec"})

PROMPT_NOTE_MAX_CHARS = 400
CONTENT_MAX_CHARS = 2000
TITLE_MAX_CHARS = 80


class AdaptiveError(Exception):
    """Raised for structural problems (unknown batch, bad payload)."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return slug[:64] or f"entry-{uuid.uuid4().hex[:8]}"


def compute_risk(kind: str, scope: str, action: str, source: str, entry_source: str | None = None) -> str:
    """Risk tier from (kind, scope, action, source) — plan 4b, stored for audit.

    High: policy/worker_spec always; any delete of ANOTHER producer's entry;
    any global-scope edit originating from Dream.
    """
    if kind in HIGH_RISK_KINDS:
        return "high"
    if action == "delete" and entry_source is not None and entry_source != source:
        return "high"
    if source == "dream" and scope == "global":
        return "high"
    return "low"


def validate_edit(edit: dict, source: str) -> str | None:
    """Structural validation. Returns an error string or None when valid."""
    action = edit.get("action")
    if action not in ACTIONS:
        return f"unknown action {action!r}"
    kind = edit.get("kind")
    if kind not in KINDS:
        return f"unknown kind {kind!r}"
    scope = edit.get("scope") or "global"
    if scope != "global" and not scope.startswith("session:"):
        return f"scope must be 'global' or 'session:<id>', got {scope!r}"
    if source not in SOURCES:
        return f"unknown source {source!r}"
    evidence = edit.get("evidence") or []
    if not isinstance(evidence, list) or not evidence:
        return "evidence is required (at least one ref)"
    if action == "create":
        if not (edit.get("title") or "").strip():
            return "create requires a title"
        if not (edit.get("content") or "").strip():
            return "create requires content"
    if action in ("update", "delete") and not (edit.get("entry_id") or edit.get("title")):
        return f"{action} requires entry_id (or a title that slugs to one)"
    content = edit.get("content") or ""
    if kind == "prompt_note" and len(content) > PROMPT_NOTE_MAX_CHARS:
        return f"prompt_note content exceeds {PROMPT_NOTE_MAX_CHARS} chars"
    if len(content) > CONTENT_MAX_CHARS:
        return f"content exceeds {CONTENT_MAX_CHARS} chars"
    return None


def _entry_id_for(edit: dict) -> str:
    return edit.get("entry_id") or slugify(edit.get("title", ""))


def _mint_batch_id() -> str:
    return f"ab-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# Queue (producer entry point)
# ---------------------------------------------------------------------------


def queue_edits(edits: list[dict], producer: str, rationale: str = "") -> dict:
    """Split a producer pass into pending auto-applies and gated proposals.

    Returns {"batch_id": str|None, "queued": n, "proposal_id": int|None,
    "proposal_ids": [int], "gated": n, "rejected": [(edit, reason)]}. One
    producer pass mints at most one auto batch and one proposal PER RISK
    TIER (plan 4d) — with auto-apply off that is two, so a reviewer can take
    the low-risk edits without also taking the high-risk ones.
    """
    result: dict = {
        "batch_id": None,
        "queued": 0,
        "proposal_id": None,
        "proposal_ids": [],
        "gated": 0,
        "rejected": [],
    }
    if not settings.adaptive_enabled:
        return result

    low: list[dict] = []
    high: list[dict] = []
    for edit in edits or []:
        err = validate_edit(edit, producer)
        if err:
            result["rejected"].append({"edit": edit, "reason": err})
            continue
        edit = dict(edit)
        edit["entry_id"] = _entry_id_for(edit)
        edit["scope"] = edit.get("scope") or "global"
        existing = db.adaptive_get_entry(edit["entry_id"])
        risk = compute_risk(
            edit["kind"],
            edit["scope"],
            edit["action"],
            producer,
            entry_source=(existing or {}).get("source"),
        )
        edit["risk"] = risk
        # A producer contradicting RULES.md must gate the edit itself (4e
        # conflict rule); the flag rides the edit into the proposal payload.
        if edit.get("conflicts_with_rules"):
            risk = edit["risk"] = "high"
        (high if risk == "high" else low).append(edit)

    low_gated: list[dict] = []
    if low and not settings.adaptive_auto_apply:
        # Auto-apply off: everything routes through human review — but as its
        # OWN proposal per tier. Folding low-risk edits into the high-risk
        # proposal would make approval all-or-nothing across risk tiers, so a
        # reviewer could not take the safe hint without also taking the policy.
        low_gated, low = low, []

    split = bool(high and low_gated)  # qualify rationales only when it matters

    def _propose(edits: list[dict], tier: str) -> None:
        evidence = [ref for e in edits for ref in (e.get("evidence") or [])]
        why = rationale or f"{len(edits)} gated edit(s) from {producer}"
        pid = db.adaptive_add_proposal(
            producer=producer,
            payload_json=json.dumps(edits),
            evidence_json=json.dumps(evidence),
            rationale=f"{why} — {tier}-risk tier" if split else why,
        )
        result["proposal_ids"].append(pid)
        if result["proposal_id"] is None:
            result["proposal_id"] = pid
        result["gated"] += len(edits)

    if low:
        batch_id = _mint_batch_id()
        db.adaptive_create_batch(batch_id, producer, json.dumps(low), status="pending")
        result["batch_id"] = batch_id
        result["queued"] = len(low)
    if high:
        _propose(high, "high")
    if low_gated:
        _propose(low_gated, "low")
    return result


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def _snapshot(entry: dict | None) -> str | None:
    return json.dumps(entry, sort_keys=True) if entry is not None else None


def _apply_one(edit: dict, producer: str, actor: str, batch_id: str, proposal_id: int | None) -> str | None:
    """Apply a single edit. Returns an error string or None on success."""
    entry_id = _entry_id_for(edit)
    action = edit["action"]
    existing = db.adaptive_get_entry(entry_id)
    active = existing if existing and existing.get("status") == "active" else None

    # Plan/apply split: reject entries that moved since planning.
    baseline = edit.get("baseline_version")
    if action == "create":
        if active is not None:
            return f"entry '{entry_id}' already exists (version {active['version']})"
    else:
        if active is None:
            return f"entry '{entry_id}' not found or not active"
        if baseline is not None and int(baseline) != int(active["version"]):
            return f"entry changed during planning (baseline v{baseline}, now v{active['version']})"

    # Caps (checked against live state, not planning-time state).
    if action == "create" and db.adaptive_entry_count(edit["kind"]) >= settings.adaptive_max_entries_per_kind:
        return f"kind '{edit['kind']}' at max entries ({settings.adaptive_max_entries_per_kind})"
    if action in ("update", "delete") and actor == "auto":
        cooldown_h = settings.adaptive_edit_cooldown_hours
        updated = (active or {}).get("updated_at") or ""
        if updated and cooldown_h > 0:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=cooldown_h)).isoformat()
            if updated > cutoff:
                return f"entry edited within cooldown ({cooldown_h}h)"

    now = _now_iso()
    if action == "create":
        new_row = {
            "id": entry_id,
            "kind": edit["kind"],
            "scope": edit.get("scope", "global"),
            "title": (edit.get("title") or "").strip()[:TITLE_MAX_CHARS],
            "content": (edit.get("content") or "").strip(),
            "risk": edit.get("risk", "low"),
            "version": 1,
            "status": "active",
            "source": producer,
            "created_at": now,
            "updated_at": now,
        }
    elif action == "update":
        new_row = dict(active)
        if edit.get("title"):
            new_row["title"] = edit["title"].strip()[:TITLE_MAX_CHARS]
        if edit.get("content"):
            new_row["content"] = edit["content"].strip()
        if edit.get("scope"):
            new_row["scope"] = edit["scope"]
        new_row["risk"] = edit.get("risk", new_row.get("risk", "low"))
        new_row["version"] = int(active["version"]) + 1
        new_row["updated_at"] = now
    else:  # delete — a status flip, so rollback can restore it
        new_row = dict(active)
        new_row["status"] = "deleted"
        new_row["version"] = int(active["version"]) + 1
        new_row["updated_at"] = now

    db.adaptive_put_entry(new_row)
    db.adaptive_add_event(
        entry_id=entry_id,
        action=action,
        before_json=_snapshot(existing),
        after_json=_snapshot(new_row),
        evidence_json=json.dumps(edit.get("evidence") or []),
        actor=actor,
        batch_id=batch_id,
        proposal_id=proposal_id,
    )
    return None


def apply_batch(batch_id: str, actor: str = "auto", proposal_id: int | None = None) -> dict:
    """Apply a pending batch edit-by-edit. Partial application is by design:
    a rejected edit (version moved, cap hit) never blocks its siblings."""
    batch = db.adaptive_get_batch(batch_id)
    if batch is None:
        raise AdaptiveError(f"unknown batch {batch_id}")
    if batch.get("status") != "pending":
        raise AdaptiveError(f"batch {batch_id} is {batch.get('status')}, not pending")
    try:
        edits = json.loads(batch.get("payload_json") or "[]")
    except (TypeError, ValueError) as e:
        raise AdaptiveError(f"batch {batch_id} payload unreadable: {e}") from e

    applied: list[str] = []
    rejected: list[dict] = []
    for edit in edits:
        err = _apply_one(edit, batch["producer"], actor, batch_id, proposal_id)
        if err:
            rejected.append({"entry_id": _entry_id_for(edit), "reason": err})
        else:
            applied.append(_entry_id_for(edit))

    # A batch where NOTHING landed changed no state. Calling it 'applied'
    # would enrol it in the tripwire sweep and the post-batch canary sweep
    # with nothing to measure, so it gets its own terminal, inert status.
    status = "applied" if applied else "rejected"
    db.adaptive_update_batch(batch_id, status=status)
    if applied:
        from core.adaptive.render import render_mirror

        render_mirror()
    logger.info("Adaptive batch %s %s: %d ok, %d rejected", batch_id, status, len(applied), len(rejected))
    return {"batch_id": batch_id, "applied": applied, "rejected": rejected, "status": status}


# ---------------------------------------------------------------------------
# Drain (called from snooze Activity 15, inside the idle window)
# ---------------------------------------------------------------------------


def drain_pending(max_batches: int | None = None) -> dict:
    """Apply pending auto batches, oldest first, under the daily cap.

    Deferral, not rejection: active work or an exhausted cap leaves batches
    pending for the next idle window. Global-scope entries land in the
    stable prefix, so this must only ever run when no session is mid-turn —
    the caller (Activity 15) guarantees the snooze idle window; the
    has_active_work re-check here is belt-and-braces (plan §6c / done-when:
    "a global apply is deferred while any session is mid-turn").
    """
    out: dict = {"applied_batches": [], "deferred": 0, "results": []}
    if not settings.adaptive_enabled:
        return out
    pending = db.adaptive_list_batches(status="pending")
    if not pending:
        return out
    try:
        from sessions.manager import get_manager

        if get_manager().has_active_work():
            out["deferred"] = len(pending)
            logger.info("Adaptive drain deferred: active work present")
            return out
    except Exception:
        pass

    day_ago = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    used = db.adaptive_auto_apply_batches_since(day_ago)
    budget = max(0, settings.adaptive_max_auto_applies_per_day - used)
    if max_batches is not None:
        budget = min(budget, max_batches)
    if budget <= 0:
        out["deferred"] = len(pending)
        logger.info("Adaptive drain deferred: daily auto-apply cap reached (%d)", used)
        return out

    for batch in pending[:budget]:
        try:
            result = apply_batch(batch["batch_id"], actor="auto")
            out["applied_batches"].append(batch["batch_id"])
            out["results"].append(result)
        except AdaptiveError as e:
            logger.warning("Adaptive drain skipped batch %s: %s", batch["batch_id"], e)
    out["deferred"] = len(pending) - len(out["applied_batches"])
    return out


# ---------------------------------------------------------------------------
# Approve (apply-on-approve, plan 4a)
# ---------------------------------------------------------------------------


def approve_proposal(proposal_id: int, actor: str = "user") -> dict:
    """Approving EXECUTES the batch through the same apply engine and mints
    a batch_id the post-batch canary sweep can join on."""
    prop = db.adaptive_get_proposal(proposal_id)
    if prop is None:
        raise AdaptiveError(f"unknown proposal {proposal_id}")
    if prop.get("status") != "pending":
        raise AdaptiveError(f"proposal {proposal_id} is {prop.get('status')}, not pending")

    try:
        payload_edits = json.loads(prop.get("payload_json") or "[]")
    except (TypeError, ValueError):
        payload_edits = []

    # Canary proposals (§12.2): dict payload, not an edit batch. Approving
    # MATERIALIZES the CANARY.md (validated round-trip) and enqueues a
    # manual vetting run — the human approval is what satisfies I6.
    if isinstance(payload_edits, dict) and payload_edits.get("canary"):
        from core.canary.propose import materialize_canary

        name, err = materialize_canary(payload_edits["canary"])
        if name is None:
            raise AdaptiveError(f"canary materialization failed: {err}")
        db.adaptive_resolve_proposal(proposal_id, "approved")
        try:
            from core.extensions.scheduling import enqueue_manual_canary

            enqueue_manual_canary(name)
        except Exception as e:
            logger.warning("Vetting run enqueue failed for canary '%s': %s", name, e)
        return {"batch_id": None, "applied": [], "rejected": [], "canary_written": name}

    # Memory-correction proposals (audit P5): validated dream contradiction/
    # stale findings used to dead-end in an empty payload — 72 of 75 pending
    # proposals on the live box had no effector. Approving now writes a
    # corrective entry into each cited memory file: mechanical, additive,
    # non-destructive — recall surfaces the correction alongside the disputed
    # entries, which is what actually changes downstream behavior.
    if (
        isinstance(payload_edits, list)
        and payload_edits
        and all(isinstance(e, dict) and e.get("action") == "memory_correction" for e in payload_edits)
    ):
        written = []
        for e in payload_edits:
            try:
                from core.memory.ingest import apply_memory_correction

                written += apply_memory_correction(
                    files=list(e.get("files") or [])[:3],
                    statement=str(e.get("statement") or ""),
                    source_ref=f"dream:{e.get('hypothesis_id', '')[:12]}",
                    kind=str(e.get("kind") or "contradiction"),
                )
            except Exception as ce:
                logger.warning("memory correction failed for proposal %s: %s", proposal_id, ce)
        db.adaptive_resolve_proposal(proposal_id, "approved")
        return {"batch_id": None, "applied": [], "rejected": [], "corrections_written": written}

    # Review-only proposals (legacy Dream memory reviews) carry no engine
    # payload: approving acknowledges — nothing to apply, no batch, no sweep.
    if not payload_edits:
        db.adaptive_resolve_proposal(proposal_id, "approved")
        return {"batch_id": None, "applied": [], "rejected": [], "review_only": True}

    batch_id = _mint_batch_id()
    db.adaptive_create_batch(batch_id, prop["producer"], prop["payload_json"], status="pending")
    result = apply_batch(batch_id, actor=actor, proposal_id=proposal_id)
    db.adaptive_resolve_proposal(proposal_id, "approved")

    try:
        from core.extensions.scheduling import enqueue_post_batch_sweep

        # Nothing landed (every edit refused) → no state change to measure.
        if result["applied"] and enqueue_post_batch_sweep(batch_id):
            result["sweep_enqueued"] = True
    except Exception as e:
        logger.warning("Post-batch sweep enqueue failed for %s: %s", batch_id, e)
    return result


# ---------------------------------------------------------------------------
# Delete (human release valve)
# ---------------------------------------------------------------------------


def delete_entry(entry_id: str, actor: str = "human") -> dict:
    """Soft-delete one entry outside the batch machinery.

    The valve for a wedged per-kind cap: producers can only ever add under
    their own rails, so without a direct human delete a full kind stays full.
    Same status flip the engine's own delete action uses (version bumped,
    before_json journaled), so rollback restores it byte-for-byte and the
    entry drops out of the prompt blocks and the cap count immediately.
    """
    existing = db.adaptive_get_entry(entry_id)
    if existing is None or existing.get("status") != "active":
        raise AdaptiveError(f"entry '{entry_id}' not found or not active")

    new_row = dict(existing)
    new_row["status"] = "deleted"
    new_row["version"] = int(existing["version"]) + 1
    new_row["updated_at"] = _now_iso()
    db.adaptive_put_entry(new_row)
    event_id = db.adaptive_add_event(
        entry_id=entry_id,
        action="delete",
        before_json=_snapshot(existing),
        after_json=_snapshot(new_row),
        evidence_json=json.dumps([f"human delete of {entry_id} via /api/adaptive/entries"]),
        actor=actor,
    )

    from core.adaptive.render import render_mirror

    render_mirror()
    logger.info("Adaptive entry %s deleted by %s (event %s)", entry_id, actor, event_id)
    return {"entry_id": entry_id, "status": "deleted", "version": new_row["version"], "event_id": event_id}


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


def _reverse_event(ev: dict, actor: str) -> None:
    """Undo one event: before present → byte-for-byte restore; absent →
    hard delete (the entry did not exist before its create)."""
    entry_id = ev["entry_id"]
    current = db.adaptive_get_entry(entry_id)
    before_json = ev.get("before_json")
    if before_json:
        db.adaptive_put_entry(json.loads(before_json))
        after = before_json
    else:
        db.adaptive_remove_entry(entry_id)
        after = None
    db.adaptive_add_event(
        entry_id=entry_id,
        action="rollback",
        before_json=_snapshot(current),
        after_json=after,
        evidence_json=json.dumps([f"rollback of event {ev['id']}"]),
        actor=actor,
        batch_id=ev.get("batch_id"),
        proposal_id=ev.get("proposal_id"),
    )


def rollback(batch_id: str | None = None, event_id: int | None = None, actor: str = "user") -> dict:
    """Exact rollback of a batch (reverse autoincrement order) or one event."""
    if bool(batch_id) == bool(event_id):
        raise AdaptiveError("rollback takes exactly one of batch_id or event_id")

    if event_id is not None:
        ev = db.adaptive_get_event(event_id)
        if ev is None:
            raise AdaptiveError(f"unknown event {event_id}")
        if ev.get("action") == "rollback":
            raise AdaptiveError("cannot roll back a rollback event")
        _reverse_event(ev, actor)
        reversed_ids = [ev["id"]]
    else:
        events = [e for e in db.adaptive_events_for_batch(batch_id) if e.get("action") != "rollback"]
        if not events:
            raise AdaptiveError(f"no events for batch {batch_id}")
        for ev in reversed(events):
            _reverse_event(ev, actor)
        db.adaptive_update_batch(batch_id, status="rolled_back")
        reversed_ids = [e["id"] for e in reversed(events)]

    from core.adaptive.render import render_mirror

    render_mirror()
    logger.info("Adaptive rollback (%s): reversed %d event(s)", actor, len(reversed_ids))
    return {"reversed_events": reversed_ids}
