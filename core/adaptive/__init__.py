"""Pernix — Adaptive Layer (adaptation plan §6): governed machine-editable policy.

Distinct from memory (facts, I3) and skills (instructions, I6): a DB-first,
version-chained store of policy the machine may edit under rails — full
event history, plan/apply version checks, exact rollback, risk tiers, caps,
and a canary-backed tripwire. NOT "harness" — core/harness/ means the nudge
machinery and is untouched.

Invariant I4 stays structural: nothing in this package can write SOUL.md,
RULES.md, or the base prompt. The apply path writes adaptive_* rows only;
the markdown mirror is render-only and never read back.
"""

from core.adaptive.engine import (
    AdaptiveError,
    annotate_proposal,
    apply_batch,
    approve_proposal,
    auto_approve_stale_proposals,
    compute_risk,
    create_entry,
    delete_entry,
    describe_proposal,
    describe_resolution,
    drain_pending,
    is_canary_proposal,
    queue_edits,
    rollback,
    validate_edit,
)
from core.adaptive.render import build_adaptive_block, build_routing_hints_block, render_mirror

__all__ = [
    "AdaptiveError",
    "annotate_proposal",
    "apply_batch",
    "approve_proposal",
    "auto_approve_stale_proposals",
    "build_adaptive_block",
    "build_routing_hints_block",
    "compute_risk",
    "create_entry",
    "delete_entry",
    "describe_proposal",
    "describe_resolution",
    "drain_pending",
    "is_canary_proposal",
    "queue_edits",
    "render_mirror",
    "rollback",
    "validate_edit",
]
