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


def _candor_prior(tool: str) -> float | None:
    """Calibrated p for tool_ok(tool), when Candor has one worth trusting."""
    if not settings.candor_enabled:
        return None
    try:
        from core.extensions.candor.bridge import get_candor_bridge

        bridge = get_candor_bridge()
        outcome = bridge.predict_sync("tool_ok", [tool]) if hasattr(bridge, "predict_sync") else None
        p = outcome.get("p") if isinstance(outcome, dict) else getattr(outcome, "p", None)
        if p is not None:
            return float(p)
    except Exception as e:
        logger.debug("telos: candor prior lookup failed: %s", e)
    return None


def extract_turn_anomalies(
    tool_summary: dict,
    termination_reason: str | None,
    reflect_retry: bool,
    session_type: str,
) -> list[dict]:
    """Turn record -> anomaly candidates, highest surprise first."""
    anomalies: list[dict] = []
    for tool, s in (tool_summary or {}).items():
        calls = int(s.get("calls", 0))
        failures = int(s.get("failures", 0))
        if calls <= 0 or failures <= 0:
            continue
        prior = _candor_prior(tool)
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
    questions from anomalies, delta-tracked per turn like Candor's hook."""
    if session.get("session_type") == "canary":
        return  # canary isolation: synthetic turns must not mint questions

    from core.telos.store import TelosStore

    turn_id = getattr(session_obj, "current_turn_user_msg_id", None)
    if getattr(session_obj, "_telos_turn_traced", None) == turn_id and turn_id is not None:
        return
    session_obj._telos_turn_traced = turn_id

    store = TelosStore.open()
    tool_summary = getattr(session_obj, "last_tool_summary", None) or {}
    termination = getattr(session_obj, "termination_reason", None)
    reflect_retry = bool(getattr(session_obj, "reflect_count", 0))

    # 1) Trace: every turn lands in the record ("the story that is told of us").
    store.trace_append(
        "turn",
        {
            "session": session_id,
            "turn": turn_id,
            "session_type": session.get("session_type") or "normal",
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
    for a in extract_turn_anomalies(tool_summary, termination, reflect_retry, session.get("session_type") or "normal"):
        if minted >= _MAX_QUESTIONS_PER_TURN:
            break
        if store.question_is_duplicate(a["text"]):
            continue
        origin = "anomaly"
        if serendipity_due and a["surprise"] >= 0.8:
            # High-surprise, deliberately unbound from any active goal.
            origin, serendipity_due = "serendipity", False
        store.add_question(
            text=a["text"],
            surprise=a["surprise"],
            derived_from=a["derived_from"] + [f"session:{session_id}"],
            parent_goal="g_root",
            origin=origin,
        )
        minted += 1


def _serendipity_due(store) -> bool:
    """Keep the serendipity share of minted questions near the budget."""
    qs = store.list_questions()
    if not qs:
        return False
    share = sum(1 for q in qs if q.get("origin") == "serendipity") / len(qs)
    return share < store.serendipity_budget()
