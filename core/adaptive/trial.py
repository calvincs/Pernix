"""Pernix — trial arms: every adaptation is an experiment (hardening W6).

An adaptive entry used to go straight from "a producer wrote it" to "in every
prompt, forever", and the only things that could ever take it back out were a
usage counter, a lint, or a human. Nothing in that loop measured whether the
entry made the agent better — the veto window measured whether anyone
objected in time, which is a different question.

With `adaptive_trial_enabled`, a machine-minted policy/prompt_note/routing_hint
lands as `trial` instead of `active`. A trial entry renders on a deterministic
half of the turns: the coin is `sha1(turn_key + entry_id)`, so the same turn
always lands on the same side, the scout prompt and the compiled system prompt
agree without either of them storing anything, and a restart cannot re-roll a
turn that is already being graded. Reflect records which trial entries the
turn actually rendered and which it held out; the sweep compares the two arms
with the same two-proportion test the tripwire uses.

Human-authored entries never enter a trial: the author is the evidence.
"""

from __future__ import annotations

import hashlib
import json
import logging

from config import settings
from db import models as db

logger = logging.getLogger("pernix.adaptive")

# The status a trialled entry holds. It is LIVE (it renders, it counts against
# the per-kind cap, it can be updated and retired) — just not on every turn.
TRIAL_STATUS = "trial"

# Only kinds that render into a prompt can have their effect measured this
# way. worker_spec and the memory-correction edits are not prompt text, so
# there is no "held out" half of a turn for them to be absent from.
TRIAL_KINDS = ("policy", "prompt_note", "routing_hint")


def status_for_new_entry(kind: str, producer: str) -> str:
    """`trial` for a machine-minted prompt entry, `active` for everything else.

    The gate is the producer, not the actor: an edit a human approved in the
    review queue is still prose a producer wrote and nobody has measured, and
    approving it is a veto being declined, not authorship. The one path that
    yields `active` under the flag is `create_entry` (source `user`), where a
    person typed the content themselves.
    """
    if not settings.adaptive_trial_enabled:
        return "active"
    if producer == "user" or kind not in TRIAL_KINDS:
        return "active"
    return TRIAL_STATUS


def turn_key(session_id: str, turn_id: int) -> str:
    """The per-turn coin: one string per (session, turn)."""
    if not session_id:
        return ""
    return f"{session_id}:{int(turn_id or 0)}"


def turn_key_for_session(session_id: str) -> str:
    """This session's current turn key, or "" when there is no live turn.

    Read from the live AgentSession (`turn_key`, stamped by the state machine
    at every turn boundary) rather than recomputed from the DB: the compiled
    prompt, the scout prompt and the grade must all use the SAME key, and the
    state log's idea of the current turn moves the moment the next one starts.
    """
    if not session_id:
        return ""
    try:
        from sessions.manager import get_manager

        live = get_manager().get(session_id)
    except Exception as e:  # no manager (scripts, tests), or none for this id
        logger.debug("No live session for turn key %s: %s", session_id[:12], e)
        return ""
    if live is None:
        return ""
    key = str(getattr(live, "turn_key", "") or "")
    if key:
        return key
    # Restored sessions get their _turn_id back before their first transition.
    turn_id = int(getattr(live, "_turn_id", 0) or 0)
    return turn_key(session_id, turn_id) if turn_id else ""


def renders_this_turn(key: str, entry_id: str) -> bool:
    """Is this trial entry in the TREATED half of the given turn?

    sha1 over `turn_key + entry_id`, first 32 bits, even means rendered. Any
    stable hash would do; what matters is that it is a pure function of the
    pair, so every consumer — the two prompt builders and the grade that
    records what they did — computes the same answer without coordination.

    No key (no live turn: a worker prompt build, a script, a test) means no
    trial entry renders at all. Rendering one outside a turn would put it in
    a prompt no post-mortem can attribute, which is an unmeasured effect —
    exactly what the trial exists to stop.
    """
    if not key or not entry_id:
        return False
    digest = hashlib.sha1(f"{key}{entry_id}".encode()).hexdigest()
    return int(digest[:8], 16) % 2 == 0


def in_arm(rows: list[dict], key: str) -> list[dict]:
    """Drop the trial entries this turn's coin held out. Active rows pass."""
    return [r for r in rows if r.get("status") != TRIAL_STATUS or renders_this_turn(key, r.get("id") or "")]


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------

# Alphas: the destructive decision takes the stricter one. Retiring an entry
# on a false positive is a lesson thrown away and re-learned; promoting one is
# reversible by every sweep that follows it.
ALPHA_PROMOTE = 0.05
ALPHA_RETIRE = 0.01

# Graded turns read per sweep. Post-mortem payloads are large, and every trial
# entry is scored from the same scan, so this is a memory bound, not a lookback
# horizon: at ~200 graded turns a day it covers nearly three weeks, and the
# arms of anything older are already decided.
_PM_SCAN_LIMIT = 4000

# How far back the Trust tab looks for trials the sweep has already settled.
_SETTLED_WINDOW_DAYS = 14

# The actor stamped on every event this module writes. It is also how a
# settled trial is found again: the journal, not a column.
SWEEP_ACTOR = "trial_sweep"


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _turn_succeeded(row: dict, payload: dict) -> bool:
    """Did this turn go well? Ground truth outranks the grader.

    Precedence is the hardening's, in order: the user's own thumb, then the
    deterministic reading of their next message (a correction is a miss
    whatever the grader concluded), then reflect's verdict.
    """
    signal = str(row.get("user_signal") or "").strip().lower()
    if signal == "up":
        return True
    if signal == "down":
        return False
    if payload.get("next_msg_correction") is True:
        return False
    return str(row.get("verdict") or "") == "pass"


def turn_records(since_iso: str = "", limit: int = _PM_SCAN_LIMIT) -> list[dict]:
    """One record per graded TURN that carried a trial arm.

    [{"success": bool, "rendered": set[str], "held_out": set[str]}]. Rows with
    neither list are turns from before trial mode (or with nothing on trial)
    and are dropped: they are not observations of anything.

    A turn is counted once. Post-mortems are per ATTEMPT, and a turn that was
    retried has two or three of them; the latest attempt is the one whose
    verdict became the turn's outcome. Canary rows are excluded like they are
    everywhere else — a canary turn measures the suite, not the agent.
    """
    try:
        rows = db.list_post_mortems(since_iso=since_iso or None, limit=limit)
    except Exception as e:
        logger.warning("Trial post-mortem scan failed: %s", e)
        return []

    out: list[dict] = []
    seen: set[tuple] = set()
    for row in rows:  # newest first, so the first row per turn is the latest attempt
        try:
            payload = json.loads(row.get("payload_json") or "{}")
        except (TypeError, ValueError):
            continue
        if payload.get("session_type") == "canary":
            continue
        rendered = payload.get("rendered_entries") or []
        held_out = payload.get("held_out_entries") or []
        if not rendered and not held_out:
            continue
        turn = (row.get("session_id"), payload.get("turn_user_msg_id") or f"pm:{row.get('id')}")
        if turn in seen:
            continue
        seen.add(turn)
        out.append(
            {
                "success": _turn_succeeded(row, payload),
                "rendered": {str(e) for e in rendered},
                "held_out": {str(e) for e in held_out},
            }
        )
    return out


def entry_stats(entry_id: str, records: list[dict] | None = None) -> dict:
    """Treated vs control outcomes for one entry since it was created.

    {"treated": {"n", "successes"}, "control": {"n", "successes"}, "p", "since"}.
    `p` is the two-sided two-proportion p-value — the tripwire's own test, so
    the two channels cannot disagree about what significance means. With
    either arm empty it is 1.0: no evidence, which is the honest reading of
    an entry nobody has measured yet.

    `records` lets a sweep score every entry from one scan of the table.
    """
    row = db.adaptive_get_entry(entry_id) or {}
    since = str(row.get("created_at") or "")
    recs = turn_records(since) if records is None else records
    treated = [r for r in recs if entry_id in r["rendered"]]
    control = [r for r in recs if entry_id in r["held_out"]]
    t_ok = sum(1 for r in treated if r["success"])
    c_ok = sum(1 for r in control if r["success"])

    from core.adaptive.tripwire import two_proportion_z_test

    _, p = two_proportion_z_test(t_ok, len(treated), c_ok, len(control))
    return {
        "treated": {"n": len(treated), "successes": t_ok},
        "control": {"n": len(control), "successes": c_ok},
        "p": p,
        "since": since,
    }


def _counts(stats: dict) -> str:
    """The evidence line every decision is journaled with."""
    t, c = stats["treated"], stats["control"]
    t_rate = f"{t['successes'] / t['n']:.0%}" if t["n"] else "—"
    c_rate = f"{c['successes'] / c['n']:.0%}" if c["n"] else "—"
    return (
        f"treated {t['successes']}/{t['n']} ({t_rate}) vs "
        f"control {c['successes']}/{c['n']} ({c_rate}), p={stats['p']:.4f}"
    )


def decide(row: dict, stats: dict) -> tuple[str, str]:
    """(action, evidence) for one trial entry: promote, retire, or wait.

    Order matters. The retire test is checked first and at the stricter alpha,
    so an entry that is measurably harmful leaves even in the same cycle its
    TTL comes due. The TTL promotion is last and always reads `unproven`: if
    the entry HAD separated, the early branch above would already have taken
    it.
    """
    from core.adaptive.retire import entry_age_days

    min_arm = int(settings.adaptive_trial_min_arm or 0)
    ttl_days = int(settings.adaptive_trial_ttl_days or 0)
    t, c = stats["treated"], stats["control"]
    both_arms = t["n"] > 0 and c["n"] > 0
    enough = t["n"] >= min_arm and c["n"] >= min_arm
    t_rate = (t["successes"] / t["n"]) if t["n"] else 0.0
    c_rate = (c["successes"] / c["n"]) if c["n"] else 0.0
    worse = both_arms and t_rate < c_rate
    better = both_arms and t_rate > c_rate
    counts = _counts(stats)

    if enough and worse and stats["p"] < ALPHA_RETIRE:
        return "retire", f"trial: measurably worse when rendered — {counts}"
    if enough and better and stats["p"] < ALPHA_PROMOTE:
        return "promote", f"trial: measurably better when rendered — {counts}"

    age = entry_age_days(row)
    if ttl_days > 0 and age is not None and age >= ttl_days:
        if worse and stats["p"] < ALPHA_PROMOTE:
            # Suggestive of harm but short of the retire alpha: keep it in the
            # arm rather than promote something the evidence leans against.
            return "wait", f"trial: past its TTL but leaning worse — {counts}"
        return "promote", f"trial: unproven after {int(age)} days — {counts}"
    return "wait", f"trial: running — {counts}"


def _promote(row: dict, evidence: str) -> int:
    """trial → active, journaled so it can be rolled back like any edit."""
    new_row = dict(row)
    new_row["status"] = "active"
    new_row["version"] = int(row.get("version") or 1) + 1
    new_row["updated_at"] = _now_iso()
    db.adaptive_put_entry(new_row)
    return db.adaptive_add_event(
        entry_id=row["id"],
        action="promote",
        before_json=json.dumps(dict(row), sort_keys=True),
        after_json=json.dumps(new_row, sort_keys=True),
        evidence_json=json.dumps([evidence]),
        actor=SWEEP_ACTOR,
    )


def trial_entries() -> list[dict]:
    """Every entry currently on trial, deterministically ordered."""
    rows: list[dict] = []
    for kind in TRIAL_KINDS:
        rows.extend(db.adaptive_list_entries(kind=kind, status=TRIAL_STATUS, limit=200))
    return sorted(rows, key=lambda r: r["id"])


def sweep_trials() -> dict:
    """Settle every trial the evidence can settle. Never raises.

    Returns {"promoted": [...], "retired": [...], "waiting": [...],
    "reasons": {entry_id: evidence}} — the caller aggregates the notification.

    Terminal decisions are journaled (a `promote` event, or the delete event's
    own evidence) with the counts and the p-value. A `wait` is not: it is the
    absence of a decision, it is re-derivable from the same post-mortems at any
    time, and one journal row per trial per idle cycle would bury the decisions
    that matter under thousands that do not.
    """
    out: dict = {"promoted": [], "retired": [], "waiting": [], "reasons": {}}
    if not settings.adaptive_enabled:
        return out
    rows = trial_entries()
    if not rows:
        return out
    oldest = min(str(r.get("created_at") or "") for r in rows)
    records = turn_records(oldest)

    from core.adaptive.engine import AdaptiveError, delete_entry

    for row in rows:
        entry_id = row["id"]
        try:
            action, evidence = decide(row, entry_stats(entry_id, records))
        except Exception as e:
            logger.warning("Trial decision failed for %s: %s", entry_id, e)
            continue
        out["reasons"][entry_id] = evidence
        if action == "retire":
            try:
                delete_entry(entry_id, actor=SWEEP_ACTOR, reason=evidence)
                out["retired"].append(entry_id)
            except AdaptiveError as e:
                logger.info("trial sweep could not retire %s: %s", entry_id, e)
        elif action == "promote":
            try:
                _promote(row, evidence)
                out["promoted"].append(entry_id)
            except Exception as e:
                logger.warning("trial sweep could not promote %s: %s", entry_id, e)
        else:
            out["waiting"].append(entry_id)
        logger.info("Adaptive trial %s: %s (%s)", entry_id, action, evidence)

    if out["promoted"] or out["retired"]:
        from core.adaptive.render import render_mirror

        render_mirror()
    return out


def list_trials(limit: int = 50) -> list[dict]:
    """What /api/trust reports: running trials, then recently settled ones.

    A settled trial is found through the journal — the sweep's own events in
    the last two weeks — because the entry itself keeps no memory of having
    been on trial once it is promoted.
    """
    if not settings.adaptive_enabled:
        return []
    from datetime import datetime, timedelta, timezone

    selected: list[tuple[dict, str]] = [(row, "trial") for row in trial_entries()]
    seen = {row["id"] for row, _ in selected}
    since = (datetime.now(timezone.utc) - timedelta(days=_SETTLED_WINDOW_DAYS)).isoformat()
    try:
        events = db.adaptive_list_events(limit=500)
    except Exception as e:
        logger.debug("Trial event scan failed: %s", e)
        events = []
    for ev in events:  # newest first
        if ev.get("actor") != SWEEP_ACTOR or str(ev.get("created_at") or "") < since:
            continue
        entry_id = str(ev.get("entry_id") or "")
        if not entry_id or entry_id in seen:
            continue
        row = db.adaptive_get_entry(entry_id)
        if row is None:
            continue
        seen.add(entry_id)
        selected.append((row, "promoted" if ev.get("action") == "promote" else "retired"))

    selected = selected[:limit]
    if not selected:
        return []
    oldest = min(str(row.get("created_at") or "") for row, _ in selected)
    records = turn_records(oldest)
    out = []
    for row, status in selected:
        stats = entry_stats(row["id"], records)
        out.append(
            {
                "entry_id": row["id"],
                "title": row.get("title") or row["id"],
                "kind": row.get("kind") or "",
                "treated": stats["treated"],
                "control": stats["control"],
                "p": stats["p"],
                "status": status,
                "since": stats["since"],
            }
        )
    return out
