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
from datetime import datetime, timedelta, timezone
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
    # adaptive_entry outcome attributions set this to 0: their usage was
    # already counted at the source (scout submit-time for hints; this pass
    # for cited policies), and double-counting the observation inflates the
    # denominator retirement divides by.
    delta_reinforcements: int = 1
    # Short, free-form note describing *why* this attribution was made.
    # Stored in signal payload for UI / debugging.
    rationale: str = ""
    # Optional per-observation resource metrics ({"tokens": int, "wall_ms":
    # int}), currently only on model_route attributions. apply_attributions
    # folds them into running accumulators in the signal's payload_json —
    # upsert_signal REPLACES payload_json wholesale, so accumulation is a
    # read-merge-write there, not an SQL delta.
    metrics: dict | None = None


# Threshold for calling a tool's performance in a single session "bad":
# if ≥50% of invocations failed, we attribute a failure against the tool.
# Anything below this is considered mixed / inconclusive and skipped.
_TOOL_FAILURE_RATIO: float = 0.5

# Failure causes that implicate the guidance an adaptive entry supplied.
# `scout` means the plan was wrong and `agent` means its execution was, and
# the rendered guidance had a hand in both. `env` (network, permissions,
# rate limit), `task` (ambiguous or impossible request) and `skill` (a
# broken skill) are failures the entry could not have caused, so they leave
# its counters alone rather than charging it for the weather.
_ENTRY_FAULT_CAUSES: frozenset[str] = frozenset({"agent", "scout"})


def attribute(pm_row: dict) -> list[Attribution]:
    """Given a post-mortem row, return the signal attributions to apply.

    Pure function — no DB access. `pm_row` is the dict returned from
    db.list_unsynthesized_post_mortems (keys: verdict, failure_cause,
    confidence, scout_viability, execution_mode, payload_json).
    """
    payload = _parse_payload(pm_row.get("payload_json"))

    # Canary post-mortems are stamped at write time (plan §5) — synthetic
    # tasks must not move real tool/skill reliability signal. The row still
    # gets marked synthesized by the caller so it never re-queues.
    if payload.get("session_type") == "canary":
        return []

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

    # --- Model-route attribution (H2, plan §12.4) ---
    # Keyed "{agent_model}|{task_category}" — the counters feed the scout's
    # [MODEL ROUTING INTEL] exception brief. Inherits every filter above
    # (canary exclusion, from_fallback skip, low-confidence skip) plus the
    # caller's latest-attempt-per-session dedupe and exactly-once watermark.
    agent_model = str(payload.get("agent_model") or "").strip()
    if agent_model:
        category = str(payload.get("task_category") or "").strip() or "general"
        # Decoupled resource channels (reward stays the primary signal;
        # tokens/wall-clock ride along as observability-only accumulators).
        turn_metrics = payload.get("turn_metrics") or {}
        metrics = None
        if isinstance(turn_metrics, dict) and int(turn_metrics.get("tokens") or 0) > 0:
            metrics = {
                "tokens": int(turn_metrics.get("tokens") or 0),
                "wall_ms": int(turn_metrics.get("wall_ms") or 0),
            }
        if verdict == "pass":
            attributions.append(
                Attribution(
                    "model_route",
                    f"{agent_model}|{category}",
                    delta_successes=1,
                    rationale="session verdict=pass",
                    metrics=metrics,
                )
            )
        elif verdict in ("retry", "escalate"):
            attributions.append(
                Attribution(
                    "model_route",
                    f"{agent_model}|{category}",
                    delta_failures=1,
                    rationale=f"verdict={verdict}, cause={failure_cause}",
                    metrics=metrics,
                )
            )

    # --- Adaptive-entry attribution (v3.1 usefulness signal) ---
    # Hints: usage was counted at scout submit-time; here only the OUTCOME
    # lands, so delta_reinforcements=0. Policies: reflect's citation is both
    # the usage and the outcome in one observation, so the reinforcement
    # rides along and a cited policy always books its use.
    #
    # Both channels record failures as well as successes. v1 credited wins
    # only, which made every entry's failure counter a structural zero: the
    # failure-dominated retirement in core/adaptive/retire.py divides wins by
    # (wins + losses) and so could never see a losing entry. Worse for
    # policies — the zero-use sweep is the only other retirement path and a
    # cited policy is used by definition, so nothing could retire one at all.
    # A non-pass verdict blamed on the plan (`scout`) or on its execution
    # (`agent`) is the honest evidence that the guidance did not carry the
    # turn; `env`/`task`/`skill` causes are not the entry's doing and still
    # book the use without a verdict either way.
    blamed = verdict in ("retry", "escalate") and failure_cause in _ENTRY_FAULT_CAUSES
    for hint_id in scout_summary.get("used_hints") or []:
        if not isinstance(hint_id, str) or not hint_id:
            continue
        if verdict == "pass":
            attributions.append(
                Attribution(
                    "adaptive_entry",
                    hint_id,
                    delta_successes=1,
                    delta_reinforcements=0,
                    rationale="hint shaped plan; verdict=pass",
                )
            )
        elif blamed:
            attributions.append(
                Attribution(
                    "adaptive_entry",
                    hint_id,
                    delta_failures=1,
                    delta_reinforcements=0,
                    rationale=(
                        f"hint shaped plan; verdict={verdict}, cause={failure_cause} — "
                        f"the plan it steered did not carry the turn"
                    ),
                )
            )
    for pol_id in payload.get("cited_policies") or []:
        if not isinstance(pol_id, str) or not pol_id:
            continue
        if verdict == "pass":
            rationale = "policy cited by reflect; verdict=pass"
        elif blamed:
            rationale = (
                f"policy cited by reflect; verdict={verdict}, cause={failure_cause} — "
                f"its guidance was in force on a turn that failed for a reason it covers"
            )
        else:
            rationale = (
                f"policy cited by reflect; verdict={verdict}, cause={failure_cause} — "
                f"use recorded, outcome not charged to the policy"
            )
        attributions.append(
            Attribution(
                "adaptive_entry",
                pol_id,
                delta_successes=1 if verdict == "pass" else 0,
                delta_failures=1 if blamed else 0,
                rationale=rationale,
            )
        )

    return attributions


# --- Model routing brief (H2, plan §12.4) --------------------------------
# Exception-report shape borrowed from candor/intel.py: only degraded pairs
# render; a (model, category) absent from the brief has no known problem.

_ROUTE_MIN_OBSERVATIONS = 5
_ROUTE_HEALTHY_RATE = 0.7
_ROUTE_MAX_LINES = 8
_ROUTE_CHAR_CAP = 1200
# Rows whose counters stopped moving are history, not intel — and the
# task_category re-keying (execution_mode → scout task_type) left legacy
# "inline"/"tasks" subjects behind that would otherwise render forever.
_ROUTE_STALE_DAYS = 45

_ROUTE_HEADER = (
    "[MODEL ROUTING INTEL] Observed reflect-verdict rates by (model, task "
    "category) — exception report: models absent here have no known problem. "
    "Steer recommended_model away from listed pairs when alternatives exist."
)


def build_model_routing_brief() -> str | None:
    """Scout-facing brief over model_route counters. None when nothing
    qualifies, so the scout prompt is byte-identical without signal."""
    try:
        rows = db.get_model_route_signals()
    except Exception as e:
        logger.warning("Model routing brief read failed: %s", e)
        return None
    stale_floor = ""
    if _ROUTE_STALE_DAYS > 0:
        stale_floor = (datetime.now(timezone.utc) - timedelta(days=_ROUTE_STALE_DAYS)).isoformat()
    lines: list[str] = []
    for r in rows:
        if len(lines) >= _ROUTE_MAX_LINES:
            break
        if stale_floor and str(r.get("last_reinforced_at") or "") < stale_floor:
            continue
        wins = int(r.get("successes") or 0)
        losses = int(r.get("failures") or 0)
        n = wins + losses
        if n < _ROUTE_MIN_OBSERVATIONS:
            continue
        rate = wins / n
        if rate >= _ROUTE_HEALTHY_RATE:
            continue
        subject = str(r.get("subject") or "")
        model, _, category = subject.partition("|")
        line = f"- {model} on {category or 'general'}: {rate:.0%} pass over {n} turns"
        # Decoupled resource channels, when accumulated: average tokens and
        # wall-clock per turn. Context for the reader — never a routing rule.
        try:
            mp = _parse_payload(r.get("payload_json"))
            m_count = int(mp.get("m_count") or 0)
            if m_count > 0:
                avg_tok = int(mp.get("m_tokens_total") or 0) // m_count
                avg_s = (int(mp.get("m_wall_ms_total") or 0) / m_count) / 1000.0
                line += f" (avg ~{avg_tok / 1000:.0f}k tok, ~{avg_s:.0f}s/turn)"
        except Exception:
            pass
        lines.append(line)
    if not lines:
        return None
    return "\n".join([_ROUTE_HEADER, *lines])[:_ROUTE_CHAR_CAP]


def apply_attributions(attrs: Iterable[Attribution]) -> int:
    """Apply attributions to the signals table. Returns count applied.

    Each attribution becomes one `upsert_signal` call. Safe to call with
    an empty iterable.

    Attributions carrying metrics accumulate them into the signal's
    payload_json (m_tokens_total / m_wall_ms_total / m_count) via
    read-merge-write: upsert_signal replaces payload_json wholesale, and
    synthesis is the payload's only writer for these signal types, running
    serially inside snooze — so the read-merge-write is race-free in
    practice and a lost update would cost one observation, not corrupt.
    """
    n = 0
    for a in attrs:
        try:
            payload: dict = {"last_rationale": a.rationale}
            # Accumulators must survive metric-less observations too (an old
            # payload without turn_metrics would otherwise wipe the totals),
            # so any signal type that ever carries metrics reads-and-carries.
            if a.metrics or a.signal_type == "model_route":
                try:
                    existing = db.get_signal(a.signal_type, a.subject) or {}
                    prior = json.loads(existing.get("payload_json") or "{}")
                    if not isinstance(prior, dict):
                        prior = {}
                except Exception:
                    prior = {}
                m_tokens = int(prior.get("m_tokens_total") or 0)
                m_wall = int(prior.get("m_wall_ms_total") or 0)
                m_count = int(prior.get("m_count") or 0)
                if a.metrics:
                    m_tokens += int(a.metrics.get("tokens") or 0)
                    m_wall += int(a.metrics.get("wall_ms") or 0)
                    m_count += 1
                if m_count:
                    payload["m_tokens_total"] = m_tokens
                    payload["m_wall_ms_total"] = m_wall
                    payload["m_count"] = m_count
            db.upsert_signal(
                a.signal_type,
                a.subject,
                delta_successes=a.delta_successes,
                delta_failures=a.delta_failures,
                payload_json=json.dumps(payload),
                delta_reinforcements=a.delta_reinforcements,
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
