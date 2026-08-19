"""TELOS retirement passes — the half of each loop that takes things back.

Three sweeps live here, all mechanical (no LLM) and all bounded per pass:

- `retire_stale_hints` — adaptive entries this layer minted and no longer
  has evidence for (the original occupant of this module, documented below).
- `archive_untestable_pool` — pooled hypotheses whose stored `gate_reason` is
  a terminal diagnosis, moved out of the scan path as `untestable`.
- `prune_speculation_pool` / `prune_soup_archive` — the age axis: pooled
  entries archived `expired` past `telos_soup_retention_days`, archived files
  finally unlinked past `telos_soup_archive_retention_days`.

--- adaptive-hint retirement (the half of the loop Candor already had) ---

`evaluate.py` mints a `routing_hint` for every supported, evidence-backed,
judge-confident claim. Nothing ever took one back, so telos consumed slots
against `adaptive_max_entries_per_kind` permanently and, once the kind
filled, every later supported claim was rejected at apply time.

Retirement criteria, in the order they are checked, all mechanical:

1. **The evidence object is gone.** The hint's creating event cites the
   claim, the hypothesis, and the question (`evaluate.py`). If the
   hypothesis file no longer exists, nothing in the store backs the hint.
2. **The hypothesis no longer reads `supported`.** A hypothesis returned to
   the speculation pool or flipped to `refuted` by a later pass has had its
   support withdrawn; the hint outlived its premise.
3. **The parent question was abandoned.** `soup.py` abandons a question
   after `telos_question_max_attempts` dry generations — the line of inquiry
   the hint serves is closed.
4. **TTL.** Past `_HINT_TTL_DAYS` the hint is retired regardless. This is
   the honest criterion: 1–3 rarely fire, because a telos verdict is
   terminal by construction (evaluation only ever picks `gated` hypotheses,
   so a `supported` one is never revisited). Without a TTL the retirement
   pass would be decorative in exactly the way the mint-only version was.
   A still-true claim re-mints cheaply the next time its hypothesis is
   re-generated and re-supported; a slot held forever cannot.

Mechanical — no LLM.
"""

from __future__ import annotations

import logging

from config import settings
from core.telos.evaluate import _MAX_ATTEMPTS as _EVAL_MAX_ATTEMPTS
from core.telos.store import ARCHIVED_HYPOTHESIS_STATES, TelosStore

logger = logging.getLogger("pernix.telos.retire")

_KINDS = ("routing_hint",)
_HINT_TTL_DAYS = 90
# Bounded like Candor's pass: a sweep is maintenance, not a purge.
_MAX_PER_PASS = 3

# Bound on the hypothesis-file passes (archive sweeps and the archive's
# hard delete). Deliberately not _MAX_PER_PASS: 3 hint retirements a day is
# maintenance because hints are scarce, but 3 files a day against a pool that
# reached 526 entries in nine days is decorative — the backlog would outrun
# the sweep. An unbounded pass is the other failure: one cron run that
# rewrites the whole store. 100 clears a pool that size in under a week.
_MAX_FILES_PER_PASS = 100


def _retire_reason(store: TelosStore, refs: list[str]) -> str | None:
    """Why this hint's evidence no longer holds, or None when it still does."""
    hypothesis_ids = [r for r in refs if r.startswith("h_")]
    question_ids = [r for r in refs if r.startswith("q_")]

    for hid in hypothesis_ids:
        h = store.read("hypothesis", hid)
        if h is None:
            return f"hypothesis {hid} no longer in the store"
        if h.get("status") != "supported":
            return f"hypothesis {hid} is now '{h.get('status')}', not supported"
    for qid in question_ids:
        q = store.read("question", qid)
        if q is not None and q.get("state") == "abandoned":
            return f"question {qid} was abandoned"
    return None


def retire_stale_hints(store: TelosStore) -> dict:
    """One retirement sweep over telos-authored adaptive entries."""
    result = {"retired": 0, "reasons": []}
    if not (settings.adaptive_enabled and settings.telos_enabled):
        return result

    from core.adaptive.contract import queue_producer_edits
    from core.adaptive.retire import creating_evidence, entry_age_days, producer_entries, retire_edit

    edits = []
    for row in producer_entries("telos", _KINDS):
        if len(edits) >= _MAX_PER_PASS:
            break
        refs = creating_evidence(row["id"])
        reason = _retire_reason(store, refs)
        if reason is None:
            age = entry_age_days(row)
            if age is not None and age >= _HINT_TTL_DAYS:
                reason = f"older than the {_HINT_TTL_DAYS}d hint TTL ({age:.0f}d)"
        if reason is None:
            continue
        edits.append(retire_edit(row, reason))
        result["reasons"].append({"entry_id": row["id"], "reason": reason})

    if edits:
        q = queue_producer_edits(edits, "telos", rationale="telos routing-hint retirement (evidence no longer holds)")
        result["retired"] = q["queued"] + q["gated"]
        store.trace_append("adaptive_retire", {"count": result["retired"], "reasons": result["reasons"]})
        logger.info("telos: queued %d routing-hint retirement(s)", result["retired"])
    return result


# A pooled hypothesis's `gate_reason` records why it never ran. Some of
# those reasons are evaluability verdicts — the entry cannot be tested as
# written, by this system, ever — and those are terminal. The prefixes and
# exact strings below are the ones `soup.gate` and `evaluate.evaluate_one`
# actually write.
#
# NOT in this list, on purpose: "eig <n> below floor ..." and its
# calibration-discounted variant. Expected information gain is a *prior* —
# how much answering would move the question — and a low one says the
# hypothesis was not worth the budget today, not that it is unanswerable. The
# two axes were conflated once already; conflating them again would archive
# the single largest and most testable class in the pool (265 of 543 entries
# on the live box) on the strength of a number the generator made up about
# its own output. EIG-floor entries stay in the speculation pool.
_TERMINAL_GATE_EXACT = {"no falsifier": "no falsifier named at mint"}
_TERMINAL_GATE_PREFIXES = {"observable absent from records": "observable absent from the records"}
_INCONCLUSIVE_PREFIX = "inconclusive x"


def terminal_gate_class(gate_reason: str) -> str | None:
    """Human-readable class name when this `gate_reason` is a terminal
    evaluability verdict, else None. Pure — the sweep's whole judgement."""
    reason = (gate_reason or "").strip()
    if not reason:
        return None
    if reason in _TERMINAL_GATE_EXACT:
        return _TERMINAL_GATE_EXACT[reason]
    for prefix, label in _TERMINAL_GATE_PREFIXES.items():
        if reason.startswith(prefix):
            return label
    if reason.startswith(_INCONCLUSIVE_PREFIX):
        # "inconclusive x2: <judge note>". Read the count rather than trusting
        # the prefix: only a hypothesis that spent the evaluator's full
        # attempt budget is a dead end, and a single inconclusive pass is a
        # retry, not a verdict.
        digits = ""
        for ch in reason[len(_INCONCLUSIVE_PREFIX) :]:
            if not ch.isdigit():
                break
            digits += ch
        if int(digits or 0) >= _EVAL_MAX_ATTEMPTS:
            return f"examined x{digits}, inconclusive"
    return None


def archive_untestable_pool(store: TelosStore) -> dict:
    """Move pooled hypotheses with a terminal `gate_reason` into the archive.

    The backfill half of the mechanism `evaluate.evaluate_one` now applies at
    the moment of the verdict. Entries already in the pool carry the same
    diagnoses — written by the mint-time gate (no falsifier, observable the
    records do not contain) or by the pre-archive evaluator (inconclusive x2)
    — and each one is re-read by every `list_hypotheses()` call in the
    generate/evaluate loop for as long as it sits there.

    Archived, never deleted, and `gate_reason` is preserved: the classes this
    sweep separates are exactly what the calibration review needs to count.
    Mechanical — no LLM, no judgement beyond `terminal_gate_class`.

    It also finishes interrupted archives: a file stamped with a terminal
    status but still sitting in soup/ is the one state a crash between
    `archive_hypothesis`'s write and its move can leave, and nothing else
    would ever look at it again.
    """
    result: dict = {"archived": 0, "classes": {}}
    for h in store.list_hypotheses():
        if result["archived"] >= _MAX_FILES_PER_PASS:
            break
        status = str(h.get("status") or "")
        if status in ARCHIVED_HYPOTHESIS_STATES:
            terminal = status
            label = "interrupted move, finished"
            reason = str(h.get("archive_reason") or label)
        elif status == "soup":
            terminal = "untestable"
            # The mint-time probe verdict (E7) is authoritative when present;
            # the gate_reason prefix match remains for pre-E7 files, whose
            # frontmatter has no `reachable` field.
            if h.get("reachable") is False:
                label = "observable absent from the records"
            else:
                label = terminal_gate_class(str(h.get("gate_reason") or ""))
            if label is None:
                continue
            reason = f"backfill sweep: {label}"
        else:
            continue
        if store.archive_hypothesis(h, terminal, reason) is None:
            continue
        result["archived"] += 1
        result["classes"][label] = result["classes"].get(label, 0) + 1

    if result["archived"]:
        store.trace_append("soup_archived", {"count": result["archived"], "classes": result["classes"]})
        logger.info("telos: archived %d untestable pooled hypotheses %s", result["archived"], result["classes"])
    return result


def prune_speculation_pool(store: TelosStore) -> dict:
    """Archive pooled hypotheses past `telos_soup_retention_days` as `expired`.

    The pool is a retained record, not a feedstock: nothing reads
    `status == "soup"` (the spec's recombination pass is unimplemented, and
    `core/telos/soup.py` says so). Left unbounded it grows by roughly the
    generation rate forever — 526 files in nine days on the live box — and
    every `list_hypotheses()` call in the generate/evaluate loop re-reads all
    of them, so the cost lands on the hot path rather than on disk. Moving
    the file out of soup/ is what pays that cost back; the status is only how
    the record reads afterwards.

    Only `soup` rows are eligible: `gated` is queued work, and `supported` /
    `refuted` are the falsification record that hint retirement and the
    calibration fallback both read.

    **Archived, not deleted.** This pass used to unlink. The pool doubles as
    the calibration review's forensic record, and the review scheduled for
    September 2026 reads exactly the cohort a deleting pruner would have
    eaten first — the oldest entries, which are the ones with outcomes. The
    id-reuse hazard that made deletion defensible (ids minted against a
    persisted high-water mark, `TelosStore.mint_id`) is not an argument for
    deleting; it only means either choice is safe. `prune_soup_archive` holds
    the one horizon where a hypothesis file is actually removed.

    `expired` is a distinct terminal status from `untestable` on purpose: it
    means the entry aged out without ever being examined, which is a fact
    about the queue, not about the hypothesis.
    """
    result: dict = {"archived": 0}
    days = int(settings.telos_soup_retention_days or 0)
    if days <= 0:
        return result

    for h in _older_than(store.list_hypotheses(status="soup"), days):
        if result["archived"] >= _MAX_FILES_PER_PASS:
            break
        if store.archive_hypothesis(h, "expired", f"aged out of the pool unexamined (>{days}d)") is not None:
            result["archived"] += 1

    if result["archived"]:
        store.trace_append("soup_pruned", {"count": result["archived"], "older_than_days": days, "action": "archived"})
        logger.info("telos: archived %d pooled hypotheses older than %dd", result["archived"], days)
    return result


def prune_soup_archive(store: TelosStore) -> dict:
    """Hard-delete horizon for soup/archive/ — the only unlink in the layer.

    The archive is out of the scan path, so it costs nothing but disk and
    there is no hurry: `telos_soup_archive_retention_days` defaults to 180
    (0 = keep forever) because the forensic value of a terminal hypothesis
    outlives its operational value by a lot. Bounded per pass like the rest.
    """
    result: dict = {"deleted": 0}
    days = int(settings.telos_soup_archive_retention_days or 0)
    if days <= 0:
        return result

    for h in _older_than(store.list_archived("hypothesis"), days, field="archived_at"):
        if result["deleted"] >= _MAX_FILES_PER_PASS:
            break
        try:
            if h.path is not None:
                h.path.unlink()
                result["deleted"] += 1
        except OSError as e:
            logger.debug("telos: archive prune failed for %s: %s", h.id, e)

    if result["deleted"]:
        store.trace_append("soup_archive_pruned", {"count": result["deleted"], "older_than_days": days})
        logger.info("telos: deleted %d archived hypotheses older than %dd", result["deleted"], days)
    return result


def _older_than(objects: list, days: int, field: str = "created_at") -> list:
    """Objects whose timestamp `field` is further back than `days`. Missing or
    unparseable stamps are skipped — a pass that guessed at an absent date
    would be deciding retention on a coin flip."""
    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    for obj in objects:
        stamp = str(obj.get(field) or "")
        if not stamp:
            continue
        try:
            when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when < cutoff:
            out.append(obj)
    return out
