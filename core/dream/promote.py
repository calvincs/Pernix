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
                promoted += 1
        except Exception as e:
            logger.warning("dream promote failed for %s: %s", row["id"][:8], e)
    return promoted


def _promote_edit(row: dict, target_kind: str) -> str | None:
    """Queue an adaptive edit; dream+global risk rules route it to a proposal."""
    from core.adaptive.contract import queue_producer_edits

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


def _promote_memory_review(row: dict) -> str | None:
    """contradiction/memory_stale: a human-review proposal with NO engine
    payload — approving acknowledges; memory edits stay human (I3)."""
    pid = db.adaptive_add_proposal(
        producer="dream",
        payload_json="[]",
        evidence_json=json.dumps(_evidence_refs(row)),
        rationale=(
            f"[memory review — no automatic apply] Dream {row.get('kind')} "
            f"hypothesis (validated): {(row.get('statement') or '').strip()[:500]} "
            f"— review the cited memory entries and correct them via "
            f"update_memory/forget if the claim holds."
        ),
    )
    return f"proposal:{pid}"
