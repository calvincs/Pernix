"""Pernix — Distillation coverage audit: the feedback loop on the memory lens.

Every introspective consumer (dream, refine, user insights) sits downstream
of distillation: a class of fact the distiller systematically drops is
invisible to all of them, and until now nothing measured that. This audit is
the measurement: sample one already-distilled session per run (budgeted per
UTC day), re-derive the durable facts a future session would need from the
raw transcript, and check each against the memory store's dedup gate.

Two outputs per fact:
  - a Candor ``distill_coverage(*)`` frequency observation (covered=True),
    plus a ``distill_miss_kind`` categorical for misses — so lens degradation
    surfaces in the intel brief and becomes dreamable evidence;
  - repair: missed facts are written back to memory (source="audit"), so the
    audit fixes what it measures instead of only counting it.

Coverage is judged end-to-end (distill + storage + consolidation), not by
per-entry provenance: the question is "does memory contain it", regardless of
which session stored it.

**Measuring and repairing must not use the same predicate.** They did: the
coverage verdict was ``store.is_duplicate(fact)`` and the repair then called
``store.add_entry(fact)``, whose gate is that same function, on the same
content, against the same corpus. If coverage said "missing", the write
succeeded by construction — so every audit deterministically wrote up to
``_MAX_REPAIRS`` LLM paraphrases of facts that may well have been present,
and counted them as recovered. A measurement whose own repair guarantees
non-zero misses is a growth pump, not an instrument.

The two now sit at deliberately different thresholds:

- **Coverage** stays on the store's own dedup gate. That is the point of the
  metric: it asks whether memory contains the fact *under the same lens
  distillation writes through*, so a drift in what that lens accepts is
  exactly what should show up in ``distill_coverage``.
- **Repair** must clear a strictly stronger bar — it mutates the store, and
  writing a paraphrase of a present fact is worse than leaving a real miss
  unrepaired for one cycle. A candidate is written back only if it also fails
  a deliberately laxer wider-net scan (``_is_absent``: lower similarity
  thresholds over more BM25 hits than the gate inspects). Anything the wider
  net catches is treated as present and is never counted as recovered.

The gap between the two is a feature: facts landing in it are ones the store
plausibly holds in a form the strict gate cannot see. Collapsing the
thresholds to a single number re-creates the pump.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

from config import settings
from db import models as db

logger = logging.getLogger("pernix.memory.audit")

_TRANSCRIPT_CHAR_CAP = 40000
_MAX_FACTS = 6
_MAX_REPAIRS = 3

# Wider net for the repair gate — laxer than the store's dedup gate (0.70
# ratio / 0.55 Jaccard over the top-3 BM25 hits) on every axis, so it catches
# everything the gate catches and more. See the module docstring for why the
# two must not converge.
_REPAIR_SCAN_K = 10
_REPAIR_RATIO = 0.50
_REPAIR_JACCARD = 0.35
_FACT_KINDS = frozenset({"finding", "decision", "preference", "constraint", "skill", "experience"})

AUDIT_PROMPT = """You are auditing a memory system's distillation quality. Below is the raw \
transcript of a session that has ALREADY been distilled into persistent memory. Your job is to \
independently re-derive what should have been remembered — you will not see what was stored, \
and you must not guess at it.

List the durable facts a future session would genuinely need from this transcript. Kinds:
- finding: something discovered or established (about a system, the world, the work)
- decision: a choice that was made and its rationale
- preference: something the user wants done a particular way
- constraint: a limit, requirement, or rule that will still apply later
- skill: a working recipe/procedure that succeeded (or a failure pattern to avoid)
- experience: how the interaction itself went, when that would change future behavior

RULES:
- Each fact must be self-contained: readable with no access to this transcript.
- Durable only — skip anything that matters solely within this session (transient state,
  one-off values, conversational filler).
- Fewer, sharper facts beat many vague ones. Quality over coverage.
- If the transcript contains nothing durable, respond with just: SKIP

Output: a JSON array, no markdown fences, at most {max_facts} items:
[{{"kind": "finding", "content": "..."}}]
/no_think"""


def _today_key() -> str:
    return f"distill_audits:{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"


def audit_budget_left() -> bool:
    if settings.distill_audit_per_day <= 0:
        return False
    used = int(db.get_snooze_state(_today_key()) or "0")
    return used < settings.distill_audit_per_day


def _debit_audit() -> None:
    used = int(db.get_snooze_state(_today_key()) or "0")
    db.set_snooze_state(_today_key(), str(used + 1))


def parse_facts(raw: str) -> list[dict]:
    """Fence-strip + parse + per-item shape validation. [] on any failure."""
    text = (raw or "").strip()
    if text.upper() == "SKIP":
        return []
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:])
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        logger.warning("distill audit: unparseable fact output: %s", text[:200])
        return []
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind", "") or "").strip().lower()
        content = str(item.get("content", "") or "").strip()
        if kind not in _FACT_KINDS:
            kind = "finding"
        # Below ~60 chars the store's dedup similarity is unreliable, so a
        # coverage verdict on a stub would be noise, not measurement.
        if len(content) < 60:
            continue
        out.append({"kind": kind, "content": content[:800]})
    return out[:_MAX_FACTS]


def _pick_session() -> dict | None:
    """One distilled, idle, not-yet-audited session — oldest first.

    Distilled = snooze_reviewed_at stamped (Activity 1 ran). Worker and
    canary sessions are excluded: workers are already summarized into their
    parent, and canary transcripts are synthetic by design (plan §5).
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=settings.snooze_cooldown_minutes * 2)).isoformat()
    with db.connect_sessions() as conn:
        rows = conn.execute(
            f"""SELECT s.* FROM sessions s
               WHERE s.snooze_reviewed_at IS NOT NULL
                 AND {db.SQL_SESSION_IS_IDLE}
                 AND s.updated_at < ?
                 AND s.session_type NOT IN ('worker', 'canary')
                 AND (
                     SELECT COUNT(*) FROM messages m
                     WHERE m.session_id = s.id
                       AND m.role IN ('user', 'assistant')
                       AND m.content != ''
                 ) >= 4
               ORDER BY s.updated_at ASC
               LIMIT 5""",
            (cutoff,),
        ).fetchall()
    for row in rows:
        if not db.get_snooze_state(f"distill_audit:{row['id']}"):
            return dict(row)
    return None


def _build_transcript(title: str, messages: list[dict]) -> str:
    lines = [f"Session: {title}"]
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "user" and content:
            lines.append(f"[USER] {content[:1000]}")
        elif role == "assistant" and content:
            lines.append(f"[ASSISTANT] {content[:600]}")
    return "\n".join(lines)


def _is_absent(store, content: str) -> bool:
    """True only when the wider net finds nothing resembling `content`.

    The repair gate. Scans more BM25 hits than the store's dedup gate does and
    accepts far weaker resemblance as evidence of presence, so it is strictly
    harder to pass: a fact already flagged as missing by the gate is written
    back only if even this lax scan comes up empty.

    Lexical mode on purpose — the vector channel would make the verdict depend
    on whether an embedding model happens to be configured, and this runs on a
    worker thread behind a snooze budget where a blocking embedding call buys
    nothing. Any failure returns False: absence must be demonstrated, and a
    scan that did not run demonstrates nothing.
    """
    try:
        results = store.search(content, mode="bm25", limit=_REPAIR_SCAN_K, _track_hits=False)
    except Exception as e:
        logger.debug("distill audit: repair scan failed, treating fact as present: %s", e)
        return False

    words = set(content.lower().split())
    for r in results:
        existing = r.entry.content
        if SequenceMatcher(None, content, existing).ratio() > _REPAIR_RATIO:
            return False
        other = set(existing.lower().split())
        if len(words) > 3 and len(other) > 3:
            if len(words & other) / len(words | other) > _REPAIR_JACCARD:
                return False
    return True


def _emit_coverage_observations(results: list[dict]) -> None:
    """Fold per-fact coverage into the Candor ledger (fire-and-forget).

    Labels and kinds only — fact prose never enters the append-only chain
    (same rule as build_memory_observations)."""
    if not settings.candor_enabled or not results:
        return
    try:
        from core.extensions.candor.bridge import get_candor_bridge

        ts_ms = int(time.time() * 1000)
        observations: list[dict] = []
        for r in results:
            observations.append(
                {
                    "pred": "distill_coverage",
                    "args": ["*"],
                    "stmt_type": "frequency",
                    "outcome": bool(r["covered"]),
                    "ctx": {"kind": r["kind"]},
                    "actor": "verifier:audit",
                    "ts": ts_ms,
                }
            )
            if not r["covered"]:
                observations.append(
                    {
                        "pred": "distill_miss_kind",
                        "args": ["*"],
                        "stmt_type": "categorical",
                        "value": r["kind"],
                        "ctx": {},
                        "actor": "verifier:audit",
                        "ts": ts_ms,
                    }
                )
        get_candor_bridge().record_nowait(observations)
    except Exception as e:
        logger.debug("distill audit: candor emission skipped: %s", e)


async def run_audit(store, is_cancelled) -> dict:
    """One audit unit: pick a session, re-derive facts, score coverage, repair.

    Returns counters for snooze stats; all-zero dict when nothing was due.
    The watermark is stamped once the LLM call completes (even on SKIP or
    parse failure) — only a transport error leaves the session for retry.
    """
    out = {"audited": 0, "facts": 0, "missed": 0, "recovered": 0, "repair_blocked": 0}
    if store is None or not audit_budget_left():
        return out
    session = await asyncio.to_thread(_pick_session)
    if session is None:
        return out
    sid = session["id"]

    messages = await asyncio.to_thread(db.get_messages, sid)
    transcript = _build_transcript(session.get("title", "Untitled"), messages)
    if len(transcript) < 400:
        db.set_snooze_state(f"distill_audit:{sid}", datetime.now(timezone.utc).isoformat())
        return out
    if is_cancelled():
        return out

    from core.llm.client import get_llm_client

    model = settings.background_model or settings.llm_model
    try:
        response = await get_llm_client().chat(
            messages=[
                {"role": "system", "content": AUDIT_PROMPT.format(max_facts=_MAX_FACTS)},
                {"role": "user", "content": transcript[:_TRANSCRIPT_CHAR_CAP]},
            ],
            model=model,
            max_tokens=1200,
        )
    except Exception as e:
        logger.warning("distill audit: LLM call failed for %s: %s", sid, e)
        return out

    db.set_snooze_state(f"distill_audit:{sid}", datetime.now(timezone.utc).isoformat())
    _debit_audit()
    out["audited"] = 1

    facts = parse_facts(response.content or "")
    if not facts:
        logger.info("distill audit: session %s — no durable facts to check", sid)
        return out

    results: list[dict] = []
    for fact in facts:
        if is_cancelled():
            break
        covered = await asyncio.to_thread(store.is_duplicate, fact["content"])
        results.append({**fact, "covered": covered})

    out["facts"] = len(results)
    misses = [r for r in results if not r["covered"]]
    out["missed"] = len(misses)
    _emit_coverage_observations(results)

    # Repair: write back only the misses the wider net also fails to find.
    # add_entry's gate cannot be the safety net here — it is the very function
    # that produced the miss verdict, so it would refuse nothing.
    for miss in misses[:_MAX_REPAIRS]:
        if is_cancelled():
            break
        if not await asyncio.to_thread(_is_absent, store, miss["content"]):
            out["repair_blocked"] += 1
            continue
        entry_type = miss["kind"] if miss["kind"] in ("finding", "decision", "skill") else "note"
        file_name = "user.profile" if miss["kind"] == "preference" else None
        confirmation = str(
            await asyncio.to_thread(
                store.add_entry,
                content=miss["content"],
                file_name=file_name,
                entry_type=entry_type,
                tags=f"audit,recovered,{miss['kind']},{time.strftime('%Y-%m-%d')}",
                weight="normal",
                source="audit",
            )
        )
        # Count only entries that actually landed: a refusal or an error is a
        # repair that did not happen, and calling it "recovered" is how the
        # counter came to overstate the audit's effect.
        if confirmation.startswith("Saved to"):
            out["recovered"] += 1
        else:
            out["repair_blocked"] += 1
            logger.debug("distill audit: repair not written — %s", confirmation[:120])

    logger.info(
        "distill audit: session %s — %d fact(s), %d missed, %d recovered, %d repair(s) blocked",
        sid,
        out["facts"],
        out["missed"],
        out["recovered"],
        out["repair_blocked"],
    )
    return out
