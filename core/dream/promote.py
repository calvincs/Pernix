"""Pernix — Dream promotion: what a validated hypothesis turns into.

Mapping by kind:
  contradiction /
  memory_stale        → memory corrections apply immediately on promotion:
                        apply_memory_correction appends an additive
                        corrective note beside the disputed entries —
                        nothing is deleted, the dream journal narrates it,
                        and the operator gets at most one notification per
                        day.
  tool_pattern /
  lesson_ineffective  → report only ("reported:report-only"). These used to
                        mint adaptive routing hints and policies; the
                        adaptive layer was retired in 3.2, so the finding
                        reaches the dream report and nothing else.
  open_question       → never promoted (report material).

Every promotion stamps status='promoted' + promoted_ref so a hypothesis is
promoted exactly once, and terminal report-only rows leave the queue rather
than sitting `validated` and tripping dream.promotion_stalled. Refuted and
expired rows never promote.
"""

from __future__ import annotations

import json
import logging
import time

from core import notices
from db import models as db

logger = logging.getLogger("pernix.dream")

_PROMOTE_LIMIT_PER_STEP = 10
# promoted_ref for a validated hypothesis that had nothing to propose. It is
# a terminal marker, so the row leaves the promotion queue instead of being
# retried forever, and it is distinguishable from a real proposal in the
# record.
_NO_EFFECTOR_REF = "reported:no-effector"
# promoted_ref for a hypothesis whose evidence already produced a promotion.
# Terminal for the same reason as _NO_EFFECTOR_REF: it must leave the queue.
_DUPLICATE_EVIDENCE_REF = "reported:duplicate-evidence"
# promoted_ref for a kind with no effector since the adaptive layer was
# retired (tool_pattern, lesson_ineffective). The finding is real and stays
# in the dream report; the marker only takes it out of the queue.
_REPORT_ONLY_REF = "reported:report-only"
# Terminal markers do not count toward the step's promotion yield.
_TERMINAL_NON_PROPOSAL = (_NO_EFFECTOR_REF, _DUPLICATE_EVIDENCE_REF, _REPORT_ONLY_REF)
_REPORT_ONLY_KINDS = ("tool_pattern", "lesson_ineffective")


async def promote_validated(limit: int = _PROMOTE_LIMIT_PER_STEP) -> int:
    """Promote up to `limit` validated hypotheses. Returns promoted count."""
    rows = db.list_dream_hypotheses(status="validated", limit=50, oldest_first=True)
    promoted = 0
    deferred = 0
    applied: list[dict] = []
    for row in rows:
        if promoted >= limit:
            break
        kind = row.get("kind")
        try:
            if kind in _REPORT_ONLY_KINDS:
                ref = _REPORT_ONLY_REF
            elif kind in ("contradiction", "memory_stale"):
                ref = _promote_memory_review(row, applied)
            else:
                continue  # open_question is report material, never promoted
            if ref:
                db.update_dream_hypothesis(row["id"], status="promoted", promoted_ref=ref)
                # A row that reached a terminal non-proposal outcome has left
                # the queue but produced nothing to review; don't count it as
                # a promotion or it inflates the step's reported yield.
                if ref not in _TERMINAL_NON_PROPOSAL:
                    promoted += 1
            else:
                deferred += 1
        except Exception as e:
            logger.warning("dream promote failed for %s: %s", row["id"][:8], e)
    if deferred:
        # One line per pass instead of one per parked row per pass — the
        # per-row detail is in the promoter's log lines.
        logger.info("dream: %d validated hypotheses left for the next pass", deferred)
    if applied:
        _announce_applied_corrections(applied)
    return promoted


def _announce_applied_corrections(applied: list[dict]) -> None:
    """Every applied correction goes to the dream journal; the operator gets
    one notification per day (the first pass that applies anything), because
    an auto-applied correction is observable, not actionable — the undo is
    deleting the tagged entry, which nobody does per notice."""
    lines = [
        f"{a['id'][:8]} {a['kind']} → {', '.join(a['written']) or 'no file accepted it'}: {a['statement'][:140]}"
        for a in applied
    ]
    try:
        from core.dream.journal import append_sync

        for line in lines:
            append_sync(f"memory correction applied on validation: {line}")
    except Exception as e:  # the journal is narration, never a reason to fail
        logger.debug("dream journal append failed: %s", e)
    marker = f"dream_corrections_notice:{time.strftime('%Y-%m-%d', time.gmtime())}"
    try:
        if db.get_snooze_state(marker):
            return
        notices.notify(
            "dream.corrections_applied",
            f"Dream: {len(applied)} memory correction(s) applied",
            (
                "Validated dream findings now write their corrective entry when they are promoted — no "
                "veto window, because a correction is additive (the disputed entries stay; recall surfaces "
                "the correction beside them). Undo one by deleting the entry tagged dream:<id> in that "
                "memory file. Further corrections today go to the Dream journal only.\n"
                + "\n".join(f"• {line}" for line in lines[:12])
                + (f"\n(+{len(lines) - 12} more in the journal)" if len(lines) > 12 else "")
            ),
            link={"kind": "tab", "tab": "dream"},
        )
        db.set_snooze_state(marker, "1")
    except Exception as e:
        logger.warning("dream: corrections notification failed: %s", e)


def _memory_files_from_evidence(row: dict) -> list[str]:
    """Memory file names cited by the hypothesis's pinned evidence."""
    files: list[str] = []
    try:
        for item in json.loads(row.get("evidence_json") or "[]"):
            if isinstance(item, dict) and item.get("type") in ("memory", "memory_entry"):
                f = item.get("file") or item.get("id") or ""
                if f and f not in files:
                    files.append(str(f))
    except (TypeError, ValueError):
        pass
    return files[:3]


_CORRECTION_DEDUP_WINDOW_S = 7 * 86400


def _correction_already_pending(kind: str | None, files: list[str]) -> bool:
    """True when the same files already got the same kind of correction in
    the last week.

    Dream re-derives a contradiction every time it re-samples the file that
    holds it, so one genuinely-conflicted memory file produced four separate
    findings. They are the same finding, and the first one already wrote the
    note. The record of what was written is the promoted hypothesis itself:
    same kind, same cited files, promoted (not merely reported) recently.
    """
    from datetime import datetime

    target = set(files)
    cutoff = time.time() - _CORRECTION_DEDUP_WINDOW_S
    for prior in db.list_dream_hypotheses(status="promoted", kind=kind, limit=500):
        if str(prior.get("promoted_ref") or "").startswith("reported:"):
            continue
        try:
            stamp = datetime.fromisoformat(str(prior.get("updated_at") or "")).timestamp()
        except ValueError:
            continue
        if stamp < cutoff:
            continue
        if set(_memory_files_from_evidence(prior)) == target:
            return True
    return False


def _promote_memory_review(row: dict, applied: list[dict] | None = None) -> str | None:
    """contradiction/memory_stale → a memory correction, APPLIED ON PROMOTION.

    The effector (audit P5) writes a corrective entry into each cited file —
    additive and non-destructive: the disputed entries stay, recall surfaces
    the correction beside them. `applied` collects what landed for the
    pass's journal/notification.

    With no cited files there is no effector, so nothing is written: the
    finding still reaches the operator through the dream report, and the
    row leaves the queue as reported:no-effector.
    """
    from core.memory.ingest import apply_memory_correction

    files = _memory_files_from_evidence(row)
    if files and _correction_already_pending(row.get("kind"), files):
        logger.info(
            "dream: %s correction for %s already applied this week — not re-applied",
            row.get("kind"),
            ", ".join(files),
        )
        return _DUPLICATE_EVIDENCE_REF
    if not files:
        logger.info(
            "dream: %s hypothesis %s validated with no citable memory file — reported, nothing to apply",
            row.get("kind"),
            row["id"][:8],
        )
        # Terminal, not skipped. Returning None would leave the row
        # `validated` forever at the head of this oldest-first queue, to be
        # re-examined on every pass and eventually to fill the window — the
        # same starvation shape that killed the validator.
        return _NO_EFFECTOR_REF
    statement = (row.get("statement") or "").strip()
    written = apply_memory_correction(
        files,
        statement[:1200],
        source_ref=f"dream:{row['id'][:12]}",
        kind=str(row.get("kind") or "contradiction"),
        approved_by="dream",
    )
    if not written:
        logger.warning("dream: correction for %s was accepted by no cited file", row["id"][:8])
        return _NO_EFFECTOR_REF
    if applied is not None:
        applied.append(
            {
                "id": row["id"],
                "kind": row.get("kind"),
                "files": files,
                "written": written,
                "statement": statement,
            }
        )
    return f"correction:{','.join(written)}"
