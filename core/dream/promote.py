"""Pernix — Dream promotion (adaptation plan 4d): validated hypotheses →
adaptive layer. The phase dream-plan.md deferred on 2026-07-31, shipped with
prime-agent's rails.

Mapping by kind:
  tool_pattern        → routing_hint edit. Dream's global-scope edits are
                        proposal-gated per plan 4b (the escalation rule wins
                        over 4d's "auto-eligible" phrasing — Dream is the
                        most speculative producer; compute_risk enforces it).
  lesson_ineffective  → policy edit (high-risk kind, gated regardless).
  contradiction /
  memory_stale        → memory-edit proposals rendered for human review —
                        no engine payload; approving acknowledges, a human
                        edits memory (no machine memory-edit path exists,
                        and building one is out of scope, I3).

Every promotion stamps status='promoted' + promoted_ref so a hypothesis is
promoted exactly once. Refuted/expired rows never promote.
"""

from __future__ import annotations

import json
import logging

from config import settings
from db import models as db

logger = logging.getLogger("pernix.dream")

_PROMOTE_LIMIT_PER_STEP = 3
# promoted_ref for a validated hypothesis that had nothing to propose. It is
# a terminal marker, so the row leaves the promotion queue instead of being
# retried forever, and it is distinguishable from a real proposal in the
# record.
_NO_EFFECTOR_REF = "reported:no-effector"
# promoted_ref for a hypothesis whose evidence already produced a promotion.
# Terminal for the same reason as _NO_EFFECTOR_REF: it must leave the queue.
_DUPLICATE_EVIDENCE_REF = "reported:duplicate-evidence"
# Terminal markers do not count toward the step's promotion yield.
_TERMINAL_NON_PROPOSAL = (_NO_EFFECTOR_REF, _DUPLICATE_EVIDENCE_REF)


def _evidence_refs(row: dict) -> list[str]:
    """Flatten the hypothesis's content-hash-pinned evidence into string refs."""
    refs: list[str] = [f"dream_hypothesis:{row['id']}"]
    try:
        for item in json.loads(row.get("evidence_json") or "[]"):
            if isinstance(item, dict):
                ident = item.get("id") or item.get("session_id") or item.get("file") or ""
                kind = item.get("type", "ref")
                if ident:
                    refs.append(f"{kind}:{ident}")
            elif item:
                refs.append(str(item))
    except (TypeError, ValueError):
        pass
    return refs


def _title_for(row: dict) -> str:
    stmt = (row.get("statement") or "").strip()
    return (stmt[:70] + "…") if len(stmt) > 70 else stmt or f"dream {row['id'][:8]}"


async def promote_validated(limit: int = _PROMOTE_LIMIT_PER_STEP) -> int:
    """Promote up to `limit` validated hypotheses. Returns promoted count."""
    if not settings.adaptive_enabled:
        return 0
    rows = db.list_dream_hypotheses(status="validated", limit=50, oldest_first=True)
    promoted = 0
    deferred = 0
    for row in rows:
        if promoted >= limit:
            break
        kind = row.get("kind")
        try:
            if kind == "tool_pattern":
                ref = _promote_edit(row, "routing_hint")
            elif kind == "lesson_ineffective":
                ref = _promote_edit(row, "policy")
            elif kind in ("contradiction", "memory_stale"):
                ref = _promote_memory_review(row)
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
        # per-row detail is at DEBUG in the promoters. This is the signal a
        # human can act on: the review queue needs draining in the UI.
        logger.info("dream: %d validated hypotheses waiting on proposal review (queue full or refused)", deferred)
    return promoted


def _evidence_already_promoted(row: dict) -> bool:
    """True when a prior promotion already rests on this row's Candor facts.

    `hypothesize.candor_keys` exists because "the same degradation
    (fetch_ok(*) p=0.49) was validated as ten differently worded
    tool_pattern hypotheses" — lexical dedup cannot see a paraphrase, so the
    evidence key is the semantic identity of the claim. Validation already
    applies that test; promotion did not, so those ten paraphrases each
    minted their own proposal. Eleven of the sixty-four proposals backed up
    on the live box were one finding about fetch_ok reliability, restated.
    """
    from core.dream.hypothesize import candor_keys, existing_candor_keys

    try:
        evidence = [e for e in json.loads(row.get("evidence_json") or "[]") if isinstance(e, dict)]
    except (TypeError, ValueError):
        return False
    keys = candor_keys(evidence)
    if not keys:
        return False  # nothing to key on: fall through to normal promotion
    prior = [
        r
        for r in db.list_dream_hypotheses(kind=row.get("kind"), limit=500)
        if r["id"] != row["id"] and r.get("status") == "promoted"
    ]
    return keys <= existing_candor_keys(prior)


def _promote_edit(row: dict, target_kind: str) -> str | None:
    """Queue an adaptive edit; dream+global risk rules route it to a proposal."""
    from core.adaptive.contract import queue_producer_edits

    if _evidence_already_promoted(row):
        logger.info(
            "dream: %s hypothesis %s rests on already-promoted evidence — not re-proposed",
            row.get("kind"),
            row["id"][:8],
        )
        return _DUPLICATE_EVIDENCE_REF

    edit = {
        "action": "create",
        "kind": target_kind,
        "scope": "global",
        "title": _title_for(row),
        "content": (row.get("statement") or "").strip(),
        "evidence": _evidence_refs(row),
    }
    result = queue_producer_edits(
        [edit],
        "dream",
        rationale=f"dream {row.get('kind')} hypothesis {row['id'][:8]} "
        f"(validated, confidence {float(row.get('confidence') or 0):.2f})",
    )
    if result["proposal_id"]:
        return f"proposal:{result['proposal_id']}"
    if result["batch_id"]:
        return f"batch:{result['batch_id']}"
    return None  # rejected (e.g. duplicate slug) — leave unpromoted for review


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


def _correction_already_pending(kind: str | None, files: list[str]) -> bool:
    """True when the same file is already awaiting the same kind of correction.

    Dream re-derives a contradiction every time it re-samples the file that
    holds it, so one genuinely-conflicted memory file produced four separate
    proposals. They are not alternatives a reviewer chooses between — they
    are the same finding, and approving any one of them writes the note.
    """
    target = {kind, *files}
    for p in db.adaptive_list_proposals(status="pending", limit=200):
        try:
            edits = json.loads(p.get("payload_json") or "[]")
        except (TypeError, ValueError):
            continue
        for e in edits:
            if not isinstance(e, dict) or e.get("action") != "memory_correction":
                continue
            if {e.get("kind"), *(e.get("files") or [])} == target:
                return True
    return False


def _promote_memory_review(row: dict) -> str | None:
    """contradiction/memory_stale → an approvable memory correction
    (audit P5). The old empty-payload proposal dead-ended: 72/75 pending
    proposals on the live box had no effector. Approving now writes a
    corrective entry into each cited file — additive and non-destructive,
    the human approval is the gate.

    With no cited files there is no effector, so **no proposal is minted**.
    The effectorless variant was not merely useless, it was corrosive: 62 of
    126 pending proposals on the live box were empty payloads whose approval
    would have written nothing, and a review queue that is half no-ops is a
    queue nobody finishes reading. The finding still reaches the operator —
    the hypothesis stays `validated` and the dream report carries it — it
    just stops pretending to be an actionable decision.
    """
    files = _memory_files_from_evidence(row)
    if files and _correction_already_pending(row.get("kind"), files):
        logger.info(
            "dream: %s correction for %s already awaiting review — not re-proposed",
            row.get("kind"),
            ", ".join(files),
        )
        return _DUPLICATE_EVIDENCE_REF
    if not files:
        logger.info(
            "dream: %s hypothesis %s validated with no citable memory file — reported, not proposed (nothing to apply)",
            row.get("kind"),
            row["id"][:8],
        )
        # Terminal, not skipped. Returning None would leave the row
        # `validated` forever at the head of this oldest-first queue, to be
        # re-examined on every pass and eventually to fill the window — the
        # same starvation shape that killed the validator.
        return _NO_EFFECTOR_REF
    pid = db.adaptive_add_proposal(
        producer="dream",
        payload_json=json.dumps(
            [
                {
                    "action": "memory_correction",
                    "kind": row.get("kind"),
                    "statement": (row.get("statement") or "").strip()[:1200],
                    "files": files,
                    "hypothesis_id": row["id"],
                }
            ]
        ),
        evidence_json=json.dumps(_evidence_refs(row)),
        rationale=(
            f"[memory correction — approving writes a corrective entry into "
            f"{', '.join(files)}] Dream {row.get('kind')} hypothesis (validated): "
            f"{(row.get('statement') or '').strip()[:500]}"
        ),
        max_pending=settings.adaptive_max_pending_proposals,
        max_pending_per_producer=settings.adaptive_max_pending_per_producer,
    )
    if pid is None:
        # Queue full: leave the row `validated` so it is reconsidered once a
        # human drains the backlog. Unlike the no-effector case this is a
        # transient refusal, and the finding is worth re-raising. DEBUG, not
        # INFO: this fires for every parked row on every cycle while the
        # queue stays full — promote_validated logs the one-line summary.
        logger.debug("dream: proposal queue full, deferring %s hypothesis %s", row.get("kind"), row["id"][:8])
        return None
    return f"proposal:{pid}"
