"""TELOS anomaly extraction — the turn-end fast-loop entry (spec §3.1).

An anomaly is a prediction error against priors. Surprise scales with the
confidence of the violated prior: being wrong about a 0.95 claim deserves
more attention than being wrong about a 0.55 claim. With Candor enabled the
violated prior's calibrated p IS the surprise source; without it, fixed
priors approximate it (tools mostly work: a failure violates ~0.9; turns
mostly complete: a retry violates ~0.8).

Questions are first-class objects with provenance — nothing enters the
system as a bare task. A slice of minted questions is tagged serendipity
(origin='serendipity', parent g_root): high-surprise, no goal relevance,
the eternity clause at the tactical level (§3.2).

Mechanical, no LLM. Called from run_post_task_hooks after Candor's hook so
the reliability record already contains this turn. Failure is never fatal.

record_gate_outcomes also lives here: it is the other turn-end trace emitter,
writing the deterministic gate verdicts that hypotheses about the gate/retry
loop have to be evaluable against.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from config import settings

logger = logging.getLogger("pernix.telos.anomaly")

# 24, down from 120 (v3.1): the live box abandoned 16 of its 18 questions —
# a deep backlog of open questions is inventory nothing will ever service,
# and every open file is hot-path scan weight.
_MAX_OPEN_QUESTIONS = 24
_MAX_QUESTIONS_PER_TURN = 2


async def _candor_priors(tools: list[str]) -> dict[str, float]:
    """Calibrated p for tool_ok(tool) per tool, when Candor has one worth
    trusting. Uses the loop-safe async ``predict`` — ``predict_sync`` raises
    on the event loop (it is for tool-executor threads only), which used to
    silently disable the prior here: surprise was always the fixed 0.9 and
    the known-flaky-tool skip never fired."""
    priors: dict[str, float] = {}
    if not settings.candor_enabled or not tools:
        return priors
    try:
        from core.extensions.candor.bridge import get_candor_bridge

        bridge = get_candor_bridge()
        for tool in tools:
            outcome = await bridge.predict("tool_ok", [tool])
            p = outcome.get("p") if isinstance(outcome, dict) else getattr(outcome, "p", None)
            if p is not None:
                priors[tool] = float(p)
    except Exception as e:
        logger.debug("telos: candor prior lookup failed: %s", e)
    return priors


def extract_turn_anomalies(
    tool_summary: dict,
    termination_reason: str | None,
    reflect_retry: bool,
    session_type: str,
    priors: dict[str, float] | None = None,
) -> list[dict]:
    """Turn record -> anomaly candidates, highest surprise first.

    ``priors`` carries pre-fetched Candor calibrations (tool -> p); this
    function stays mechanical and synchronous.
    """
    anomalies: list[dict] = []
    priors = priors or {}
    for tool, s in (tool_summary or {}).items():
        calls = int(s.get("calls", 0))
        failures = int(s.get("failures", 0))
        if calls <= 0 or failures <= 0:
            continue
        prior = priors.get(tool)
        if prior is not None:
            # Candor has ANY calibrated record for this tool → the failure is
            # already tracked by the system that actually closes reliability
            # loops (ledger, intel brief, degraded-tool hints). Re-asking
            # "under what conditions does tool X fail" here produced the
            # 16-abandoned-question class on the live box — TELOS's questions
            # are for anomalies the rest of the system CANNOT explain.
            continue
        surprise = 0.9
        errors = s.get("errors") or []
        detail = f" ({str(errors[0])[:120]})" if errors else ""
        # Phrase the question against observables that are CONTINUOUSLY
        # recorded, never against the triggering turn's snapshot. The old
        # wording ("fail N/M calls this turn") named evidence that no longer
        # existed by evaluation time, so every spawned hypothesis was
        # un-evaluable by construction — 14 of the 18 questions abandoned by
        # 2026-08-16 were this exact class (session 1e2806e0d2ea's audit).
        # tool_ok(tool) and tool_failure_mode(tool) have standing Candor
        # loggers, so hypotheses citing them can actually be discriminated.
        anomalies.append(
            {
                "text": (
                    f"Under what conditions does tool '{tool}' fail? It just failed "
                    f"{failures}/{calls} calls and Candor has no calibrated record for it yet."
                    f"{detail} Evaluable against the standing ledgers: tool_ok('{tool}') and "
                    f"tool_failure_mode('{tool}') record every call."
                ),
                "surprise": surprise,
                "derived_from": [f"tool:{tool}"],
            }
        )
    if reflect_retry:
        anomalies.append(
            {
                "text": "What class of turn keeps failing its first attempt? Reflect requested a "
                "retry; the reflect_verdict ledger and post-mortems record every verdict with "
                "its failure cause.",
                "surprise": 0.8,
                "derived_from": ["reflect:retry"],
            }
        )
    if termination_reason == "round_ceiling":
        anomalies.append(
            {
                "text": "What loop pattern consumes tool rounds without reducing the task? A turn "
                "hit the round ceiling; the trace's turn events record per-turn tool call counts.",
                "surprise": 0.85,
                "derived_from": ["termination:round_ceiling"],
            }
        )
    anomalies.sort(key=lambda a: -a["surprise"])
    return anomalies


async def on_post_task(session_id: str, session: dict, session_obj) -> None:
    """The _maybe_telos hook body. Appends the turn to the trace, mints
    questions from anomalies, delta-tracked per turn like Candor's hook.

    Runs at turn end on the event loop: the Candor priors are awaited via the
    loop-safe bridge, and all filesystem store work (glob + YAML parse +
    SequenceMatcher dedup over the question corpus) is pushed to a thread so
    the turn's critical path never blocks on it.
    """
    import asyncio

    if session.get("session_type") == "canary":
        return  # canary isolation: synthetic turns must not mint questions

    from sessions.state import turn_state

    turn = turn_state(session_obj)
    turn_id = getattr(session_obj, "current_turn_user_msg_id", None)
    if turn.telos_turn_traced == turn_id and turn_id is not None:
        return
    turn.telos_turn_traced = turn_id

    tool_summary = turn.tool_summary or {}
    termination = getattr(session_obj, "termination_reason", None)
    reflect_retry = bool(turn.reflect_count)

    failing_tools = [t for t, s in tool_summary.items() if int(s.get("calls", 0)) > 0 and int(s.get("failures", 0)) > 0]
    priors = await _candor_priors(failing_tools)

    await asyncio.to_thread(
        _post_task_store_work,
        session_id,
        session.get("session_type") or "normal",
        turn_id,
        tool_summary,
        termination,
        reflect_retry,
        priors,
    )


def _post_task_store_work(
    session_id: str,
    session_type: str,
    turn_id,
    tool_summary: dict,
    termination,
    reflect_retry: bool,
    priors: dict[str, float],
) -> None:
    """Synchronous store side of the post-task hook (runs in a thread)."""
    from core.telos.store import TelosStore

    store = TelosStore.open()

    # 1) Trace: every turn lands in the record ("the story that is told of us").
    store.trace_append(
        "turn",
        {
            "session": session_id,
            "turn": turn_id,
            "session_type": session_type,
            "termination": termination,
            "reflect_retry": reflect_retry,
            "tools": {
                t: {"calls": s.get("calls", 0), "failures": s.get("failures", 0)} for t, s in tool_summary.items()
            },
        },
    )

    # 2) Questions from anomalies, bounded and deduplicated. ONE corpus scan
    # feeds every check below — this hook used to trigger up to six full
    # directory scans per turn (open-count, serendipity share, per-anomaly
    # remint + duplicate checks) inside its 10s teardown budget.
    questions = store.list_questions()
    if sum(1 for q in questions if q.get("state") == "open") >= _MAX_OPEN_QUESTIONS:
        return
    serendipity_due = _serendipity_due(questions, store.serendipity_budget())
    minted = 0
    for a in extract_turn_anomalies(tool_summary, termination, reflect_retry, session_type, priors=priors):
        if minted >= _MAX_QUESTIONS_PER_TURN:
            break
        if _recently_minted(questions, a["derived_from"]):
            continue
        if store.question_is_duplicate(a["text"], questions=questions):
            continue
        origin = "anomaly"
        if serendipity_due and a["surprise"] >= 0.8:
            # High-surprise, deliberately unbound from any active goal.
            origin, serendipity_due = "serendipity", False
        q = store.add_question(
            text=a["text"],
            surprise=a["surprise"],
            derived_from=a["derived_from"] + [f"session:{session_id}"],
            parent_goal="g_root",
            origin=origin,
        )
        questions.append(q)
        minted += 1


def record_gate_outcomes(
    session_id: str,
    session_type: str,
    attempt: int,
    reflect_mode: str,
    gates: list[dict],
) -> None:
    """Append one trace event per gate for this attempt. Blocking — call via
    to_thread, like the rest of this module's store work.

    Separate events, never one event carrying an attempts array: the
    hypothesis evaluator keyword-matches whole trace events against a
    falsifier's observables, so the fail -> retry -> pass arc has to be
    readable as a SEQUENCE of events to be discriminable at all. Field order
    puts the discriminating fields first because _gather_evidence shows only
    the first 300 characters of an event (it matches on the full blob).

    trace_append never raises; this loop is bounded by the session's gate
    count, so the whole call is cheap.
    """
    from core.telos.store import TelosStore

    if not gates:
        return
    store = TelosStore.open()
    for gate in gates:
        passed = bool(gate.get("passed"))
        event = {
            "name": gate.get("name"),
            "passed": passed,
            "attempt": attempt,
            "reflect_mode": reflect_mode,
            "session_type": session_type,
            "session": session_id,
        }
        excerpt = str(gate.get("excerpt") or "")
        if not passed and excerpt:
            event["excerpt"] = excerpt
        store.trace_append("gates", event)


def _recently_minted(questions: list, derived_from: list[str]) -> bool:
    """A question from the same source marker exists within the cooldown —
    or is still OPEN at any age (v3.1): one open line of inquiry per source,
    full stop; a cooldown that expires while the question still sits open
    just re-mints the backlog.

    Text dedup alone cannot do this job from either direction: the old
    per-turn wording varied by failure counts, so the same flaky tool minted
    a near-identical question every day; a stable wording would flip that
    into a forever-block against the abandoned corpus. The derived_from
    marker (tool:X, reflect:retry, termination:round_ceiling) is stable per
    source, so suppression keys on it.
    """
    markers = {m for m in derived_from if not m.startswith("session:")}
    if not markers:
        return False
    days = max(0, settings.telos_anomaly_remint_cooldown_days)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ") if days else ""
    for q in questions:
        if not markers & set(q.get("derived_from") or []):
            continue
        if q.get("state") == "open":
            return True
        if cutoff and str(q.get("created_at") or "") >= cutoff:
            return True
    return False


_SERENDIPITY_WINDOW = 30


def _serendipity_due(questions: list, budget: float) -> bool:
    """Keep the serendipity share of *recent* minting near the budget.

    Measured over the last _SERENDIPITY_WINDOW questions rather than the
    lifetime corpus — a lifetime share stops serendipity permanently once the
    historical average crosses the budget, regardless of recent throughput.
    """
    if not questions:
        return False
    recent = sorted(questions, key=lambda q: q.get("created_at") or "")[-_SERENDIPITY_WINDOW:]
    share = sum(1 for q in recent if q.get("origin") == "serendipity") / len(recent)
    return share < budget
