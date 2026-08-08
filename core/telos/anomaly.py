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
"""

from __future__ import annotations

import logging

from config import settings

logger = logging.getLogger("pernix.telos.anomaly")

_MAX_OPEN_QUESTIONS = 120
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
        if prior is not None and prior < 0.6:
            continue  # known-flaky tool failing is not an anomaly — no violated prior
        surprise = prior if prior is not None else 0.9
        errors = s.get("errors") or []
        detail = f" ({str(errors[0])[:120]})" if errors else ""
        anomalies.append(
            {
                "text": (
                    f"Why did tool '{tool}' fail {failures}/{calls} calls this turn when its "
                    f"prior reliability is ~{surprise:.2f}?{detail}"
                ),
                "surprise": surprise,
                "derived_from": [f"tool:{tool}"],
            }
        )
    if reflect_retry:
        anomalies.append(
            {
                "text": "Why did this turn's first attempt miss the intent (reflect requested a retry)? "
                "What class of turn does this keep happening on?",
                "surprise": 0.8,
                "derived_from": ["reflect:retry"],
            }
        )
    if termination_reason == "round_ceiling":
        anomalies.append(
            {
                "text": "Why did the turn hit the tool-round ceiling instead of converging? Is a loop "
                "pattern consuming rounds without reducing the task?",
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

    # Goal attribution (audit P5 port 1): bind minted questions to the goal
    # the session is actually executing instead of hardcoding g_root.
    goal_id = getattr(session_obj, "active_goal_id", None)
    goal_objective = ""
    if goal_id:
        try:
            from db import models as _db

            row = await asyncio.to_thread(_db.get_active_goal, session_id)
            if row and int(row.get("id", 0)) == int(goal_id):
                goal_objective = str(row.get("objective") or "")
            else:
                goal_id = None
        except Exception:
            goal_id = None

    await asyncio.to_thread(
        _post_task_store_work,
        session_id,
        session.get("session_type") or "normal",
        turn_id,
        tool_summary,
        termination,
        reflect_retry,
        priors,
        goal_id,
        goal_objective,
    )


def _post_task_store_work(
    session_id: str,
    session_type: str,
    turn_id,
    tool_summary: dict,
    termination,
    reflect_retry: bool,
    priors: dict[str, float],
    goal_id: int | None = None,
    goal_objective: str = "",
) -> None:
    """Synchronous store side of the post-task hook (runs in a thread)."""
    from core.telos.store import TelosStore

    store = TelosStore.open()
    parent_goal = "g_root"
    if goal_id:
        try:
            parent_goal = store.ensure_db_goal(goal_id, goal_objective).id
        except Exception as e:
            logger.debug("telos: db-goal mirror failed for %s: %s", goal_id, e)

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

    # 2) Questions from anomalies, bounded and deduplicated.
    open_count = len(store.list_questions(state="open"))
    if open_count >= _MAX_OPEN_QUESTIONS:
        return
    serendipity_due = _serendipity_due(store)
    minted = 0
    for a in extract_turn_anomalies(tool_summary, termination, reflect_retry, session_type, priors=priors):
        if minted >= _MAX_QUESTIONS_PER_TURN:
            break
        if store.question_is_duplicate(a["text"]):
            continue
        origin = "anomaly"
        q_parent = parent_goal
        if serendipity_due and a["surprise"] >= 0.8:
            # High-surprise, deliberately unbound from any active goal.
            origin, serendipity_due = "serendipity", False
            q_parent = "g_root"
        store.add_question(
            text=a["text"],
            surprise=a["surprise"],
            derived_from=a["derived_from"] + [f"session:{session_id}"],
            parent_goal=q_parent,
            origin=origin,
        )
        minted += 1


_SERENDIPITY_WINDOW = 30


def _serendipity_due(store) -> bool:
    """Keep the serendipity share of *recent* minting near the budget.

    Measured over the last _SERENDIPITY_WINDOW questions rather than the
    lifetime corpus — a lifetime share stops serendipity permanently once the
    historical average crosses the budget, regardless of recent throughput.
    """
    qs = store.list_questions()
    if not qs:
        return False
    recent = sorted(qs, key=lambda q: q.get("created_at") or "")[-_SERENDIPITY_WINDOW:]
    share = sum(1 for q in recent if q.get("origin") == "serendipity") / len(recent)
    return share < store.serendipity_budget()
