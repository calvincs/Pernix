"""Pernix — Snooze synthesis: post-mortems → tool/skill performance counters.

Pure attribution rules (testable without I/O) + a run() driver that
reads unsynthesized post-mortems and upserts success/failure counters.
Results are displayed in the Skills and Tools UI sections as observed
performance — not fed to scout as advisory signals.

Attribution decisions are deliberately conservative. When in doubt, we
skip rather than attribute — better a missing counter than a poisoned one.
Ambiguous verdict ↔ failure_cause combinations produce no attributions.

Run this from snooze's idle cycle. Watermark is the post_mortems
`synthesized_at` column: NULL = unprocessed, ISO = processed. Idempotent
by construction — a crashed run retries only the rows it didn't mark.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Iterable

from db import models as db

logger = logging.getLogger("pernix.synthesis")


@dataclass
class Attribution:
    """One (signal_type, subject, delta) derivation from a post-mortem."""

    signal_type: str
    subject: str
    delta_successes: int = 0
    delta_failures: int = 0
    # Short, free-form note describing *why* this attribution was made.
    # Stored in signal payload for UI / debugging.
    rationale: str = ""


# Threshold for calling a tool's performance in a single session "bad":
# if ≥50% of invocations failed, we attribute a failure against the tool.
# Anything below this is considered mixed / inconclusive and skipped.
_TOOL_FAILURE_RATIO: float = 0.5


def attribute(pm_row: dict) -> list[Attribution]:
    """Given a post-mortem row, return the signal attributions to apply.

    Pure function — no DB access. `pm_row` is the dict returned from
    db.list_unsynthesized_post_mortems (keys: verdict, failure_cause,
    confidence, scout_viability, execution_mode, payload_json).
    """
    payload = _parse_payload(pm_row.get("payload_json"))

    scout_summary = payload.get("scout_summary") or {}

    # Attribution sensitivity:
    #   from_cache=True  -> attribute normally. A cache hit reused a real scout
    #                       plan that actually ran through the full scout loop;
    #                       the recommendations are real signal.
    #   from_fallback=True -> skip entirely. Fallback reports are synthesized
    #                         defaults produced when scout couldn't run (LLM
    #                         down, parse failure); recommendations are not
    #                         evidence of what scout would have chosen, so
    #                         crediting/penalizing subjects based on them
    #                         poisons the signal.
    if scout_summary.get("from_fallback"):
        return []

    verdict = pm_row.get("verdict", "pass")
    failure_cause = pm_row.get("failure_cause", "none")

    # Very-low-confidence reflect outputs are too noisy to act on.
    try:
        confidence = float(pm_row.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < 0.5 and verdict != "pass":
        return []

    attributions: list[Attribution] = []

    # --- Skill attribution ---
    skills = scout_summary.get("recommended_skills") or []
    for skill in skills:
        if not isinstance(skill, str) or not skill:
            continue
        if verdict == "pass":
            attributions.append(
                Attribution(
                    "skill",
                    skill,
                    delta_successes=1,
                    rationale="session verdict=pass",
                )
            )
        elif verdict == "retry" and failure_cause in ("scout", "skill"):
            # The skill was part of a wrong plan (scout) or itself broken (skill).
            attributions.append(
                Attribution(
                    "skill",
                    skill,
                    delta_failures=1,
                    rationale=f"verdict=retry, cause={failure_cause}",
                )
            )
        # retry with cause=agent/env/task → not the skill's fault; skip.
        # escalate → ambiguous, skip.

    # --- Tool attribution (from tool_summary: calls + failures per tool) ---
    tool_summary = payload.get("tool_summary") or {}
    for tool_name, stats in tool_summary.items():
        if not isinstance(stats, dict):
            continue
        try:
            calls = int(stats.get("calls") or 0)
            failures = int(stats.get("failures") or 0)
        except (TypeError, ValueError):
            continue
        if calls < 1:
            continue
        ratio = failures / calls if calls else 0.0
        if ratio >= _TOOL_FAILURE_RATIO:
            attributions.append(
                Attribution(
                    "tool",
                    tool_name,
                    delta_failures=1,
                    rationale=f"{failures}/{calls} calls failed",
                )
            )
        elif failures == 0:
            # Strictly clean — every call succeeded. Mixed results (1 failure,
            # many successes) are skipped as inconclusive.
            attributions.append(
                Attribution(
                    "tool",
                    tool_name,
                    delta_successes=1,
                    rationale=f"{calls} calls, all succeeded",
                )
            )

    return attributions


def apply_attributions(attrs: Iterable[Attribution]) -> int:
    """Apply attributions to the signals table. Returns count applied.

    Each attribution becomes one `upsert_signal` call. Safe to call with
    an empty iterable.
    """
    n = 0
    for a in attrs:
        try:
            db.upsert_signal(
                a.signal_type,
                a.subject,
                delta_successes=a.delta_successes,
                delta_failures=a.delta_failures,
                payload_json=json.dumps({"last_rationale": a.rationale}),
            )
            n += 1
        except Exception as e:
            logger.warning(
                "Failed to upsert signal %s:%s — %s",
                a.signal_type,
                a.subject,
                e,
            )
    return n


@dataclass
class SynthesisStats:
    processed: int = 0
    attributions: int = 0
    # Per-session retries that were marked synthesized but NOT attributed,
    # because a later attempt in the same session superseded them.
    superseded: int = 0


def run(batch_limit: int = 500) -> SynthesisStats:
    """Process a batch of unsynthesized post-mortems.

    Intended to be called from snooze's idle cycle. Processes up to
    `batch_limit` rows, applies signals, then marks them as synthesized.
    Returns stats.

    Dedupe: multiple post-mortems from the same session (retry attempts)
    collapse to the latest one — only the final attempt is attributed,
    preventing a session with N retries from writing N signal updates
    per subject. The superseded rows still get marked synthesized so
    they don't re-queue on the next cycle.
    """
    rows = db.list_unsynthesized_post_mortems(limit=batch_limit)
    stats = SynthesisStats()
    if not rows:
        return stats

    # Group by session_id, then pick the "latest" per group: highest attempt
    # wins, created_at breaks ties.
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row.get("session_id") or "", []).append(row)

    processed_ids: list[str] = []
    for sid, group in groups.items():
        group.sort(
            key=lambda r: (int(r.get("attempt") or 0), str(r.get("created_at") or "")),
        )
        latest = group[-1]
        superseded = group[:-1]

        try:
            attrs = attribute(latest)
            applied = apply_attributions(attrs)
            stats.attributions += applied
            processed_ids.append(latest["id"])
            stats.processed += 1
        except Exception as e:
            logger.warning(
                "Synthesis failed on post_mortem %s (session %s): %s",
                latest.get("id"),
                sid,
                e,
            )
            # Do NOT mark superseded rows synthesized if the latest failed —
            # we want a future run to retry the whole group together.
            continue

        # Latest attributed successfully → mark superseded as synthesized too.
        for r in superseded:
            processed_ids.append(r["id"])
            stats.superseded += 1

        if superseded:
            logger.info(
                "Synthesis dedupe: session %s had %d retry attempts; " "attributed only attempt %s.",
                sid,
                len(group),
                latest.get("attempt"),
            )

    if processed_ids:
        db.mark_post_mortems_synthesized(processed_ids)
    logger.info(
        "Synthesis processed %d groups, %d attributions, %d superseded retries",
        stats.processed,
        stats.attributions,
        stats.superseded,
    )
    return stats


def _parse_payload(raw) -> dict:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}
