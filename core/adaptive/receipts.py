"""Pernix — Adaptive receipts: does this entry cite a recorded outcome?

The adaptive layer's evidence field has always been free text. `validate_edit`
asks only for a non-empty list, so "the agent kept re-reading files" satisfies
it exactly as well as a post-mortem id does — and an LLM quoting another LLM
is how a story becomes policy. This module is the other half of that field: a
small grammar of REFERENCES to things the system actually recorded, and a
resolver that checks each one against the database.

The grammar (nothing else is a ref — free text stays legal, it just does not
count):

    pm:<post_mortem_id>          a graded turn
    candor:<key>                 a Candor fact key, "pred(arg, ...)"
    signal:<type>/<subject>      a scout_signals row
    feedback:<message_id>        a user's thumbs on one message
    hypothesis:<id>              a dream hypothesis
    session:<id>                 the producer pass that emitted the edit

An entry is `grounded` when at least one ref RESOLVES — the row is really
there. `session:` never grounds anything: `queue_producer_edits` stamps it on
every producer edit automatically, so treating it as evidence would grade the
whole population grounded and measure nothing. A `hypothesis:` ref grounds
only when the hypothesis itself rests on a pm or candor ref, which is what
keeps dream from bootstrapping its own evidence.

The grade is computed on demand from the creating event's evidence_json. No
migration, no stored column, and re-gradeable the moment a resolver learns
something new (message_feedback lands in v36; until then a feedback ref is
simply unresolvable, never an error).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger("pernix.adaptive")

GROUNDED = "grounded"
UNFOUNDED = "unfounded"

# The six recognised prefixes, in the order a reader would rank them.
REF_KINDS: tuple[str, ...] = ("pm", "candor", "signal", "feedback", "hypothesis", "session")

# `session:` is deliberately absent: it is stamped automatically, so it can
# never be the thing that makes an entry evidence-backed.
GROUNDING_KINDS: frozenset[str] = frozenset({"pm", "candor", "signal", "feedback", "hypothesis"})

_REF_RE = re.compile(r"^(" + "|".join(REF_KINDS) + r"):(.+)$", re.IGNORECASE)
# "pred(a, b)" — Candor renders its fact keys this way in the intel brief and
# in the hint evidence snooze already writes.
_CANDOR_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)$")


@dataclass(frozen=True)
class Ref:
    """One parsed receipt. `raw` keeps the original string for logs."""

    kind: str
    value: str
    raw: str

    @property
    def signal_parts(self) -> tuple[str, str]:
        """(signal_type, subject) for a `signal:` ref; ("", "") otherwise."""
        if self.kind != "signal":
            return ("", "")
        head, _, tail = self.value.partition("/")
        return (head.strip(), tail.strip())


def parse(evidence: list[str] | None) -> list[Ref]:
    """Extract the receipts from an evidence list, preserving order.

    Anything that is not one of the six prefixes is free text and is dropped
    here — silently, because free text remains a legal (just unpersuasive)
    thing to write in an evidence list.
    """
    refs: list[Ref] = []
    seen: set[tuple[str, str]] = set()
    for item in evidence or []:
        text = str(item or "").strip()
        m = _REF_RE.match(text)
        if not m:
            continue
        kind = m.group(1).lower()
        value = m.group(2).strip()
        if not value:
            continue
        if kind == "signal":
            # A signal is identified by BOTH halves; "signal:tool_pattern"
            # names a whole family, not a row, so it is not a ref.
            head, _, tail = value.partition("/")
            if not head.strip() or not tail.strip():
                continue
        if (kind, value) in seen:
            continue
        seen.add((kind, value))
        refs.append(Ref(kind=kind, value=value, raw=text))
    return refs


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _resolve_pm(pm_id: str) -> bool:
    from db import models as db

    return db.get_post_mortem(pm_id) is not None


def _resolve_signal(signal_type: str, subject: str) -> bool:
    from db import models as db

    return db.get_signal(signal_type, subject) is not None


def _resolve_feedback(message_id: str) -> bool:
    """A row in message_feedback (migration v36).

    The table does not exist on every branch yet, and an OperationalError
    here must read as "cannot be checked", not as "the entry lied".
    """
    from db.database import connect_sessions

    try:
        with connect_sessions() as conn:
            row = conn.execute(
                "SELECT 1 FROM message_feedback WHERE message_id = ? LIMIT 1",
                (message_id,),
            ).fetchone()
        return row is not None
    except Exception:
        return False


def _candor_key_parts(key: str) -> tuple[str, list[str]] | None:
    m = _CANDOR_KEY_RE.match(key.strip())
    if not m:
        return None
    args = [a.strip() for a in m.group(2).split(",") if a.strip()]
    return (m.group(1), args)


def _resolve_candor(key: str) -> bool:
    """An admitted Candor fact, read through the bridge.

    Unresolvable — not false — when candor is off or the bridge cannot be
    reached; the same posture as a table that does not exist yet. The sync
    read refuses to run on the event loop by design, so callers that might
    be on one get False and, with it, a human review.
    """
    from config import settings

    if not settings.candor_enabled:
        return False
    parts = _candor_key_parts(key)
    if parts is None:
        return False
    pred, args = parts
    try:
        from core.extensions.candor.bridge import get_candor_bridge

        return get_candor_bridge().predict_sync(pred, args) is not None
    except Exception as e:
        logger.debug("receipts: candor resolution failed for %s: %s", key, e)
        return False


def _hypothesis_row(hypothesis_id: str) -> dict | None:
    from db.database import connect_sessions

    try:
        with connect_sessions() as conn:
            row = conn.execute(
                "SELECT * FROM dream_hypotheses WHERE id = ?",
                (hypothesis_id,),
            ).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def _resolve_hypothesis(hypothesis_id: str) -> bool:
    """A hypothesis grounds an entry only if IT rests on a recorded outcome.

    Dream's evidence items are dicts pinned at observation time: a
    post-mortem carries {"type": "pm", "id": ...}, a Candor line carries
    {"type": "candor", "pred": ..., "args": [...]}. Anything else (a memory
    entry, a bare string) is the model's own material and cannot promote
    itself into evidence.
    """
    row = _hypothesis_row(hypothesis_id)
    if row is None:
        return False
    try:
        items = json.loads(row.get("evidence_json") or "[]")
    except (TypeError, ValueError):
        return False
    if not isinstance(items, list):
        return False
    for item in items:
        if isinstance(item, dict):
            kind = str(item.get("type") or "")
            if kind == "pm" and str(item.get("id") or ""):
                return True
            if kind == "candor" and str(item.get("pred") or ""):
                return True
        elif isinstance(item, str):
            for ref in parse([item]):
                if ref.kind in ("pm", "candor") and resolve(ref):
                    return True
    return False


def resolve(ref: Ref) -> bool:
    """Does this ref point at something the system actually recorded?

    Never raises: an unreachable store is an unresolvable ref, which costs
    the proposal a human review rather than crashing the sweep that is
    reviewing it.
    """
    try:
        if ref.kind == "pm":
            return _resolve_pm(ref.value)
        if ref.kind == "candor":
            return _resolve_candor(ref.value)
        if ref.kind == "signal":
            signal_type, subject = ref.signal_parts
            return bool(signal_type and subject) and _resolve_signal(signal_type, subject)
        if ref.kind == "feedback":
            return _resolve_feedback(ref.value)
        if ref.kind == "hypothesis":
            return _resolve_hypothesis(ref.value)
    except Exception as e:  # pragma: no cover — defensive
        logger.debug("receipts: resolution failed for %s: %s", ref.raw, e)
    return False  # session: and everything unknown


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------


def grade(entry_or_evidence: str | list[str] | None) -> str:
    """ "grounded" | "unfounded" for an entry id or a raw evidence list.

    A string is read as an adaptive entry id and graded on the evidence
    recorded with its CREATING event — `adaptive_entries` has no evidence
    column, the audit chain lives in `adaptive_events`.
    """
    if isinstance(entry_or_evidence, str):
        from core.adaptive.retire import creating_evidence

        try:
            evidence = creating_evidence(entry_or_evidence)
        except Exception as e:
            logger.debug("receipts: could not read creating evidence for %s: %s", entry_or_evidence, e)
            evidence = []
    else:
        evidence = list(entry_or_evidence or [])

    for ref in parse(evidence):
        if ref.kind in GROUNDING_KINDS and resolve(ref):
            return GROUNDED
    return UNFOUNDED


def grade_evidence_json(evidence_json: str | None) -> str:
    """Grade a stored evidence_json blob (proposals, events). Never raises."""
    try:
        data = json.loads(evidence_json or "[]")
    except (TypeError, ValueError):
        return UNFOUNDED
    if not isinstance(data, list):
        return UNFOUNDED
    return grade([str(x) for x in data])


def count_unfounded() -> int:
    """How many ACTIVE entries cite nothing the system recorded.

    The headline number for /api/trust: entries whose only evidence is
    prose. Counted live rather than stored, so retiring an entry or landing
    a resolver (message_feedback, candor coming online) moves it.
    """
    from db import models as db

    try:
        rows = db.adaptive_list_entries(status="active", limit=500)
    except Exception as e:
        logger.debug("receipts: entry listing failed: %s", e)
        return 0
    return sum(1 for r in rows if grade(str(r.get("id") or "")) == UNFOUNDED)
