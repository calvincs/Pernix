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
which session stored it. The auditor's misses are themselves LLM output, so
repairs go through add_entry's own dedup gate a second time at write.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone

from config import settings
from db import models as db

logger = logging.getLogger("pernix.memory.audit")

_TRANSCRIPT_CHAR_CAP = 40000
_MAX_FACTS = 6
_MAX_REPAIRS = 3
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
            """SELECT s.* FROM sessions s
               WHERE s.snooze_reviewed_at IS NOT NULL
                 AND s.state = 'idle'
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
    out = {"audited": 0, "facts": 0, "missed": 0, "recovered": 0}
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

    # Repair: write the misses back through the normal store path. add_entry
    # re-runs its own dedup gate, so a near-miss of an existing entry is
    # refused there rather than duplicated here.
    for miss in misses[:_MAX_REPAIRS]:
        if is_cancelled():
            break
        entry_type = miss["kind"] if miss["kind"] in ("finding", "decision", "skill") else "note"
        file_name = "user.profile" if miss["kind"] == "preference" else None
        confirmation = await asyncio.to_thread(
            store.add_entry,
            content=miss["content"],
            file_name=file_name,
            entry_type=entry_type,
            tags=f"audit,recovered,{miss['kind']},{time.strftime('%Y-%m-%d')}",
            weight="normal",
            source="audit",
        )
        if not str(confirmation).startswith("Memory already contains"):
            out["recovered"] += 1

    logger.info(
        "distill audit: session %s — %d fact(s), %d missed, %d recovered",
        sid,
        out["facts"],
        out["missed"],
        out["recovered"],
    )
    return out
