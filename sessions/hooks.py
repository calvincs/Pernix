"""Pernix — Post-task hooks: auto-title, memory distillation, reflect, evaluation.

Every DB read here runs off the event loop. Post-hooks fire at the tail of
each turn while other sessions are mid-stream, and several of these load the
full transcript — with 100KB tool results that is enough to freeze every
session's SSE for the duration if done inline.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from config import settings
from db import models as db

logger = logging.getLogger("pernix.sessions.hooks")

# How much of the transcript tail _maybe_reflect loads. A single turn —
# scout row, assistant rounds, tool results, reflect row — is far smaller
# than this even for a long tool loop, so the window comfortably covers the
# turn while keeping the read bounded on a long-lived session.
REFLECT_TAIL_MESSAGES = 400


def _strip_thinking(text: str) -> str:
    """Strip LLM thinking/reasoning blocks from response content.

    Handles <think>...</think> tags and 'Thinking Process:' style prefixes
    that thinking models emit before the actual answer.
    """
    # Remove <think>...</think> blocks (greedy, handles multiline)
    text = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL)
    # If a TITLE: line exists, strip everything before it
    m = re.search(r"^(TITLE:.*)", text, flags=re.MULTILINE)
    if m:
        text = text[m.start() :]
    return text.strip()


async def run_post_task_hooks(session_id: str, emit=None, session_obj=None) -> None:
    """Run all post-task hooks for a completed session turn.

    Args:
        emit: Optional callback(event_dict) to emit SSE events.
        session_obj: Optional AgentSession for Reflect state tracking.
    """
    session = await asyncio.to_thread(db.get_session, session_id)
    if not session:
        return

    # Auto-title if still default
    if session["title"] == "New session":
        await _auto_title(session_id, emit=emit)

    # Clean up any stale questions (answered inline or moot after turn completed)
    await _cleanup_stale_questions(session_id, session_obj=session_obj)

    # Memory distillation
    await _maybe_distill(session_id, session)

    # Deterministic gates (plan 3a): run in FINALIZING immediately before
    # Reflect, once per attempt (they re-run on every reflect retry — the
    # unchanged-watch_paths guard exists for exactly that). Results feed the
    # clamp inside reflect; when reflect doesn't run, a failing gate
    # requests the retry directly.
    gate_results: list = []
    if settings.gates_enabled and session_obj:
        gate_results = await _run_turn_gates(session_id, session, session_obj, emit=emit)

    # Reflect: post-execution verification. For interactive sessions this may
    # only schedule the grade (see _reflect_is_deferred) — the gates above are
    # then the sole synchronous verification this turn gets.
    if settings.reflect_enabled and session_obj:
        await _maybe_reflect(session_id, session, emit=emit, session_obj=session_obj, gate_results=gate_results)
    elif gate_results and session_obj:
        _apply_gate_retry_fallback(session_id, session, session_obj, gate_results, emit=emit)


async def _cleanup_stale_questions(session_id: str, session_obj=None) -> None:
    """Delete questions that the user answered inline (bypassing the modal).

    A question is stale if a user message was sent AFTER the question was created,
    meaning the conversation continued without using the question answer flow.
    Questions from the current turn are NOT deleted — the user hasn't seen them yet.
    """
    questions = db.get_questions(session_id)
    if not questions:
        return

    last_user_ts = db.get_last_message_at(session_id, "user")
    if not last_user_ts:
        return
    cleaned = 0
    for q in questions:
        if q.get("created_at", "") < last_user_ts:
            db.delete_question(q["id"])
            cleaned += 1

    if cleaned:
        # If every stale question was cleaned and the session is still parked
        # in AWAITING_USER, transition it out via question-dismissed so the
        # state machine agrees with the DB (no outstanding question rows).
        remaining = db.get_questions(session_id)
        if not remaining and session_obj:
            from sessions import state_v2 as sv2

            if sv2._current_state(session_obj) is sv2.SessionStateV2.AWAITING_USER:
                try:
                    sv2.transition(
                        session_obj,
                        sv2.SessionStateV2.IDLE_READY,
                        "question-dismissed",
                    )
                except Exception as e:
                    logger.error("stale-question cleanup transition failed: %s", e)
        logger.info("Cleaned up %d stale question(s) for session %s", cleaned, session_id)


async def _auto_title(session_id: str, emit=None) -> None:
    """Generate a session title and subtitle from the first user+assistant exchange."""
    # Full read: the title comes from the FIRST exchange, so `last=` can't
    # bound it. Off-loop instead.
    messages = await asyncio.to_thread(db.get_messages, session_id)
    user_msgs = [m for m in messages if m["role"] == "user"]
    if not user_msgs:
        return

    # Build context from first exchange — assistant response reveals actual topic
    asst_msgs = [m for m in messages if m["role"] == "assistant"]
    context_parts = [f"User: {user_msgs[0]['content'][:300]}"]
    if asst_msgs:
        context_parts.append(f"Assistant: {asst_msgs[0]['content'][:300]}")
    context = "\n".join(context_parts)

    try:
        from core.llm.client import get_llm_client

        client = get_llm_client()
        model = settings.background_model or settings.llm_model

        system_prompt = (
            "Generate two things for this conversation:\n"
            "1. TITLE: A concise title (3-6 words) capturing the specific intent. "
            "Use an action verb for requests (e.g. 'Fix nginx proxy timeout'). "
            "For questions, lead with the topic (e.g. 'Redis caching strategies').\n"
            "2. SUBTITLE: A brief phrase (3-5 words) describing the task area or domain "
            "(e.g. 'backend api debugging', 'weather data lookup', 'ui component styling').\n\n"
            "Reply in exactly this format, nothing else:\n"
            "TITLE: <title>\n"
            "SUBTITLE: <subtitle>"
        )

        from core.llm.client import chat_with_backup

        response = await chat_with_backup(
            client,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": context},
            ],
            model=model,
            max_tokens=300,
        )
        raw = _strip_thinking(response.content)
        title = ""
        subtitle = ""
        for line in raw.split("\n"):
            line = line.strip()
            if line.upper().startswith("TITLE:"):
                title = line[6:].strip().strip("\"'")[:60]
            elif line.upper().startswith("SUBTITLE:"):
                subtitle = line[9:].strip().strip("\"'")[:40]

        # Fallback: if model didn't follow format, use whole response
        # BUT reject thinking/reasoning garbage
        if not title:
            candidate = raw.strip().strip("\"'")[:60]
            if not re.match(r"^(Thinking|Thought|<think|Step \d|1\.|I need to)", candidate, re.IGNORECASE):
                title = candidate

        if title:
            updates = {"title": title}
            if subtitle:
                updates["subtitle"] = subtitle
            db.update_session(session_id, **updates)
            logger.debug("Auto-titled session %s: %s [%s]", session_id, title, subtitle)
            if emit:
                emit({"type": "session.title", "title": title, "subtitle": subtitle})
    except Exception as e:
        logger.warning("Auto-title failed for %s: %s", session_id, e)


async def _maybe_distill(session_id: str, session: dict) -> None:
    """Trigger memory distillation if session qualifies."""
    if not settings.memory_recall:
        return
    # Canary isolation (plan §5): memory writes are disabled for synthetic
    # runs — reads stay (recall quality is part of what canaries measure).
    if session.get("session_type") == "canary":
        return

    # Full read: distill_session summarizes the whole session. Off-loop.
    messages = await asyncio.to_thread(db.get_messages, session_id)
    substantive = [m for m in messages if m["role"] in ("user", "assistant")]

    # Quality gate: need enough substance to distill
    if len(substantive) < 4:
        return
    total_chars = sum(len(m.get("content", "")) for m in substantive)
    if total_chars < 500:
        return

    try:
        from core.memory.distill import distill_session

        await distill_session(
            session_id=session_id,
            title=session.get("title", ""),
            messages=messages,
            session_type=session.get("session_type", "normal"),
        )
    except Exception as e:
        logger.warning("Distillation failed for %s: %s", session_id, e)


def _broadcast_reflect_notification(
    session_id: str,
    session: dict,
    title: str,
    body: str,
    kind: str = "attention",
) -> None:
    """Record a reflect notice; core.notices decides tier and delivery.

    kind="attention": the turn stopped and needs the user (escalate, retries
    exhausted, circuit breaker, budget skip) — sessions.reflect_attention.
    kind="followup": the answer was already delivered and a background grade
    found it incomplete — worth a look, never a buzz (sessions.reflect_followup).
    Canary/worker sessions record nothing: the registry drops them (the Canary
    tab records each outcome; a worker reports to its orchestrator).
    """
    from core import notices

    session_title = session.get("title", "")
    label = f"{session_title}: {title}" if session_title else title
    category = "sessions.reflect_followup" if kind == "followup" else "sessions.reflect_attention"
    notices.notify(
        category,
        label,
        body,
        session_id=session_id,
        link={"kind": "session", "id": session_id},
        session_type=session.get("session_type"),
    )


async def _run_turn_gates(session_id: str, session: dict, session_obj, emit=None) -> list:
    """Execute this attempt's gates (blocking runner via to_thread), persist
    a transcript-visible eval row, emit SSE, log the outcomes to the standing
    ledgers. Never raises."""
    import json as _json

    from core.gates import run_gates_for_turn

    try:
        attempt = session_obj.turn.reflect_count + 1
        results = await asyncio.to_thread(run_gates_for_turn, session_id, session_obj, attempt)
    except Exception as e:
        logger.warning("Gate execution failed for %s: %s", session_id, e)
        return []
    if not results:
        return []
    failed = [r for r in results if not r.passed]

    # A gate that could not RUN is a defect in the gate, not a failure of the
    # turn. It no longer clamps the verdict or forces a retry (core.gates
    # .failing excludes it), so this is the only channel that tells the agent
    # its check has been verifying nothing — carried to the next turn's scout
    # via reflect_lessons, the same way retry guidance travels.
    try:
        from core.gates import format_broken_notice

        notice = format_broken_notice(results)
        if notice:
            existing = session_obj.turn.reflect_lessons or ""
            if notice not in existing:
                session_obj.turn.reflect_lessons = (existing + "\n\n" + notice).strip()
            logger.info("Gate(s) broken for %s — notice carried to the next turn", session_id)
    except Exception as e:
        logger.debug("Broken-gate notice skipped for %s: %s", session_id, e)
    try:
        await asyncio.to_thread(
            db.add_message,
            session_id,
            "eval",
            _json.dumps({"kind": "gate", "attempt": attempt, "gates": [r.to_payload() for r in results]}),
        )
    except Exception as e:
        logger.debug("Gate eval-row insert skipped: %s", e)
    if emit:
        emit(
            {
                "type": "gates.done",
                "attempt": attempt,
                "total": len(results),
                "failed": len(failed),
                "names_failed": [r.name for r in failed],
                "names_broken": [r.name for r in results if r.broken],
            }
        )
    return results


def _same_failure_repeating(session_id: str, turn_started_iso: str | None = None) -> str | None:
    """Cross-retry circuit breaker predicate (audit P1f).

    Returns a short human-readable signature when the two most recent
    post-mortems for this session are both 'retry' verdicts with the same
    failure_cause and near-identical reasoning — i.e. the retry mechanism is
    reproducing the failure rather than correcting it. Callers only invoke
    this once reflect_count >= 2, so both rows belong to the current turn.
    Field case 2072ab68cfd4: ten consecutive retries, byte-similar reasoning
    ("spawned workers against explicit scout instruction") every time.
    """
    import json as _json
    from difflib import SequenceMatcher

    from db import models as db

    try:
        pms = db.list_post_mortems(session_id=session_id, limit=2)
    except Exception:
        return None
    if len(pms) < 2:
        return None
    a, b = pms[0], pms[1]
    # Turn scoping: gate-fallback retries bump reflect_count WITHOUT writing
    # post-mortems, so the second row can belong to a previous turn — and
    # gate-failure texts are templated enough to false-trip the breaker.
    # Both rows must postdate this turn's user message.
    if turn_started_iso and (a.get("created_at", "") < turn_started_iso or b.get("created_at", "") < turn_started_iso):
        return None
    if a.get("verdict") != "retry" or b.get("verdict") != "retry":
        return None
    if a.get("failure_cause") != b.get("failure_cause"):
        return None

    def _txt(pm: dict) -> str:
        try:
            p = _json.loads(pm.get("payload_json") or "{}")
        except Exception:
            p = {}
        return ((p.get("reasoning") or "") + " " + (p.get("diagnostic") or "")).strip().lower()

    ta, tb = _txt(a), _txt(b)
    if not ta or not tb:
        return None
    if SequenceMatcher(None, ta, tb).ratio() < 0.7:
        return None
    return f"cause={a.get('failure_cause')}: {ta[:160]}"


def _apply_gate_retry_fallback(session_id: str, session: dict, session_obj, gate_results, emit=None) -> None:
    """When Reflect's verdict can't gate the turn (reflect disabled, skipped
    for a short turn, or deferred to an observe-only background grade), a
    failing gate still requests the retry — subject to the same cap Reflect
    honors. A gate result is deterministic and material by construction, so it
    keeps its clamp even where LLM verdicts lost theirs.
    AWAITING_USER and errored turns deliberately get no fallback:
    waiting on a human is a legitimate block, and error turns lack reliable
    evidence."""
    from core.gates import failing, format_retry_guidance

    bad = failing(gate_results)
    if not bad:
        return
    max_retries = (
        settings.reflect_max_retries_worker if session.get("session_type") == "worker" else settings.reflect_max_retries
    )
    if session_obj.turn.reflect_count >= max_retries:
        logger.info("Gates failing for %s but retry cap reached (%d)", session_id, session_obj.turn.reflect_count)
        return
    session_obj.turn.reflect_count += 1
    guidance = format_retry_guidance(gate_results)
    session_obj.turn.reflect_lessons = ((session_obj.turn.reflect_lessons or "") + "\n\n" + guidance).strip()
    session_obj.turn.reflect_retry_requested = True
    logger.info(
        "Gate retry fallback: requesting retry #%d for %s (%s failing, reflect skipped)",
        session_obj.turn.reflect_count,
        session_id,
        ", ".join(g.name for g in bad),
    )
    if emit:
        emit(
            {
                "type": "reflect.retry",
                "attempt": session_obj.turn.reflect_count,
                "max": max_retries,
                "reasoning": f"deterministic gate failure ({', '.join(g.name for g in bad)}); reflect skipped",
                "strategy": "",
            }
        )


async def _recall_lesson_evidence(session_id: str, messages: list) -> str:
    """Past-lesson recall block appended to reflect's evidence blob.

    Shared by the synchronous and deferred grading paths so both verdicts are
    formed against the same evidence. Never raises: a memory problem must not
    cost the turn its verdict.
    """
    try:
        last_user_msg = next(
            (m["content"] for m in reversed(messages) if m["role"] == "user" and m.get("content")),
            "",
        )
        if not last_user_msg:
            return ""
        from core.memory.store import get_memory_store

        store = get_memory_store()
        if not store:
            return ""
        lessons = await asyncio.to_thread(store.search_lessons, last_user_msg, limit=3)
        if not lessons:
            return ""
        import time as _t

        _now_ts = int(_t.time())
        lines = ["## Past lessons that may apply (verify current relevance — codebase moves fast)"]
        for r in lessons:
            _age_d = max(0, (_now_ts - int(r.entry.epoch or _now_ts)) // 86400)
            _age_s = f"{_age_d}d ago" if _age_d > 0 else "today"
            lines.append(f"- [{r.entry.file_name}, {_age_s}] {r.entry.content[:400]}")
        return "\n".join(lines)
    except Exception as e:
        logger.debug("Reflect lesson recall failed for %s: %s", session_id, e)
        return ""


# ---------------------------------------------------------------------------
# Deferred reflect — observe-only grading for interactive sessions
# ---------------------------------------------------------------------------


def _reflect_is_deferred(session: dict) -> bool:
    """True when this session's reflect grade runs in the background.

    Only interactive ("normal") sessions defer: they're the ones with a human
    watching the turn finish (measured: 16.5s median, 47s p90 of reflect
    latency in front of them, and roughly half the resulting non-pass verdicts
    were strictness artifacts that shouldn't have re-run anything).
    cron/worker/canary keep the synchronous retry-capable path — nobody is
    waiting, and an unattended retry is the point.
    """
    return bool(settings.reflect_deferred_normal) and (session.get("session_type") or "normal") == "normal"


@dataclass
class _DeferredGrade:
    """Turn-end snapshot the background grade runs against.

    Everything here is copied at scheduling time. `session.turn` is replaced
    wholesale at the next turn boundary, so the deferred task must neither read
    from nor write to it — the snapshot is its only view of the graded turn.
    """

    session_id: str
    ticket: int  # matched against session._deferred_reflect_seq to detect supersession
    turn_id: int
    turn_user_msg_id: int | None
    attempt: int
    # Closing bound of this turn's message-id window, read at scheduling time.
    # With next-turn grading the grade can run while turn N+1 is already
    # appending to the transcript, and reflect's "slice back to the last scout
    # marker" would then land inside that newer turn. The pair
    # (turn_user_msg_id, turn_last_msg_id) pins the slice to the turn that was
    # actually snapshotted.
    turn_last_msg_id: int | None = None
    tool_summary: dict = field(default_factory=dict)
    # Per-attempt breakdown (C2). Snapshotted like tool_summary: the deferred
    # grader runs ~minutes after the turn, when session.turn is long replaced.
    tool_summary_attempts: list = field(default_factory=list)
    scout_report: Any = None
    termination_reason: str | None = None
    prior_termination_reasons: list = field(default_factory=list)
    gate_results: list = field(default_factory=list)


# The one staleness reason next-turn grading does NOT override: the session
# object this snapshot belongs to is gone, so there is nothing left to grade
# against or to hold a background ref on.
SESSION_REPLACED = "session object was replaced (reaped and re-created)"

# How often the waiting grade looks up to see whether the user has replied.
# Two seconds is far below the 300s window it is embedded in and far above
# the cost of reading two in-memory ints.
_GRADE_POLL_S = 2.0


def _deferred_grade_superseded(session_obj, snap: _DeferredGrade) -> str | None:
    """Reason this snapshot is stale, or None when it's still gradable.

    Rapid-fire policy: only the latest completed turn gets graded. A turn the
    user has already moved past is explicitly marked ungraded rather than
    graded late against a transcript that has since grown.

    This is the LEGACY rule, and with ``reflect_next_turn_grading`` on it no
    longer decides on its own — see `_grade_despite`. Its reasons are still
    the honest description of what happened to the turn, so it keeps producing
    them; only "the user moved on" stopped meaning "drop the grade".
    """
    from sessions import state_v2 as sv2
    from sessions.manager import get_manager

    live = get_manager().get(snap.session_id)
    if live is not None and live is not session_obj:
        return SESSION_REPLACED
    if getattr(session_obj, "_deferred_reflect_seq", 0) != snap.ticket:
        return "a later turn scheduled its own grade"
    if _real_turn_started(session_obj, snap):
        return "turn counter advanced"
    if session_obj.current_turn_user_msg_id is not None:
        return "a turn is in flight"
    state = sv2._current_state(session_obj)
    if state is not sv2.SessionStateV2.IDLE_READY:
        return f"session is {state.value}"
    return None


def _real_turn_started(session_obj, snap: _DeferredGrade) -> bool:
    """True when a turn the USER started has begun since this snapshot.

    A worker-resume turn is the harness talking to itself, not the user moving
    on (field case, session 3dc5a307d751: the redundant resume turn superseded
    the grade of the turn that did the work). Synthetic turn ids are tagged at
    the source in sessions/manager.py; every other advance of the counter is a
    real turn, and a real turn is exactly the trigger next-turn grading waits
    for.
    """
    live_turn = getattr(session_obj, "_turn_id", 0)
    if live_turn == snap.turn_id:
        return False
    synthetic = getattr(session_obj, "_synthetic_turn_ids", None) or set()
    return any(t not in synthetic for t in range(snap.turn_id + 1, live_turn + 1))


def _grade_despite(stale: str) -> bool:
    """True when a stale reason no longer justifies dropping the grade.

    The old rule dropped a pending grade the moment the user replied inside
    the quiet window — which is to say it threw the grade away precisely when
    the best evidence for it had just arrived. On the box that left roughly a
    quarter of turns permanently ungraded. With next-turn grading on, every
    reason except a vanished session object is context for the grade rather
    than a reason to skip it.
    """
    if not settings.reflect_next_turn_grading:
        return False
    return stale != SESSION_REPLACED


def _grade_lock(session_obj) -> asyncio.Lock:
    """The session's single in-flight-deferred-grade lock, created on demand.

    This is what bounds the cost of "every real turn gets graded": a burst of
    five rapid-fire turns queues five grades on one lock and spends them one
    at a time, instead of firing five reflect calls into the same session at
    once. The field is declared on AgentSession but left None there: an
    asyncio.Lock binds to the loop that creates it, and sessions are built off
    the event loop.
    """
    lock = getattr(session_obj, "_deferred_grade_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        session_obj._deferred_grade_lock = lock
    return lock


def _next_user_message(snap: _DeferredGrade) -> str:
    """The user's first message after the graded turn, or "" if they never replied.

    Harness-authored user rows (the worker-resume injection) are skipped: the
    point of this text is that a HUMAN wrote it after reading the response.
    """
    from sessions.manager import _WORKER_RESUME_PREFIX

    anchor = snap.turn_user_msg_id if snap.turn_user_msg_id is not None else snap.turn_last_msg_id
    if anchor is None:
        return ""
    try:
        rows = db.user_messages_after(snap.session_id, int(anchor), limit=4)
    except Exception as e:
        logger.debug("Next-message lookup failed for %s: %s", snap.session_id, e)
        return ""
    for row in rows:
        content = (row.get("content") or "").strip()
        if content and not content.startswith(_WORKER_RESUME_PREFIX):
            return content
    return ""


async def _await_deferred_grade(session_obj, snap: _DeferredGrade) -> str:
    """Wait for this turn's grading moment; return the user's next message.

    Two triggers, whichever comes first: the quiet period elapses (the turn
    the user never answered), or a real turn N+1 starts (the turn they did).
    Either way the reply is looked up afterwards, so a message that landed in
    the last second of the window is still evidence.
    """
    delay = max(0, int(settings.reflect_defer_idle_s))
    if not settings.reflect_next_turn_grading:
        await asyncio.sleep(delay)
        return ""
    deadline = time.monotonic() + delay
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or _real_turn_started(session_obj, snap):
            break
        await asyncio.sleep(min(_GRADE_POLL_S, remaining))
    return await asyncio.to_thread(_next_user_message, snap)


async def _schedule_deferred_reflect(session_id: str, session: dict, session_obj, gate_results, emit=None) -> None:
    """Hand this turn's grade to a background task and return immediately."""
    from sessions.manager import get_manager

    try:
        termination_history = await asyncio.to_thread(db.recent_termination_reasons, session_id, 3)
    except Exception as e:
        logger.debug("Failed to fetch termination history for %s: %s", session_id, e)
        termination_history = []

    # Closing bound of the turn's message window, read HERE rather than at
    # grading time: by then the next turn may already have appended to it.
    try:
        turn_last_msg_id = await asyncio.to_thread(db.last_message_id, session_id)
    except Exception as e:
        logger.debug("Failed to read the turn's last message id for %s: %s", session_id, e)
        turn_last_msg_id = None

    session_obj._deferred_reflect_seq += 1
    snap = _DeferredGrade(
        session_id=session_id,
        ticket=session_obj._deferred_reflect_seq,
        turn_id=getattr(session_obj, "_turn_id", 0),
        turn_user_msg_id=getattr(session_obj, "current_turn_user_msg_id", None),
        attempt=session_obj.turn.reflect_count + 1,
        tool_summary=dict(session_obj.turn.tool_summary or {}),
        tool_summary_attempts=[dict(a) for a in (session_obj.turn.tool_summary_attempts or [])],
        scout_report=session_obj.last_scout_report,
        termination_reason=getattr(session_obj, "termination_reason", None),
        prior_termination_reasons=termination_history[1:] if termination_history else [],
        gate_results=list(gate_results or []),
        turn_last_msg_id=turn_last_msg_id,
    )

    delay = max(0, int(settings.reflect_defer_idle_s))
    # Fire-and-forget by design: if the process dies before the grade runs the
    # turn is simply ungraded. _spawn_detached only supplies the strong task
    # ref and the failure log — nothing here is awaited by the turn.
    get_manager()._spawn_detached(
        _deferred_reflect_task(session_obj, snap),
        f"deferred-reflect:{session_id[:12]}",
    )
    logger.info(
        "Reflect deferred for session %s: grading in %ds (observe-only, ticket=%d)",
        session_id,
        delay,
        snap.ticket,
    )
    if emit:
        emit({"type": "reflect.deferred_scheduled", "delay_s": delay})


async def _deferred_reflect_task(session_obj, snap: _DeferredGrade) -> None:
    """Wait for the grading moment, then grade the turn — once per turn.

    The wait ends when the user replies or the quiet period runs out, and the
    grade itself is serialized behind the session's single in-flight lock, so
    a rapid-fire burst costs one reflect call at a time rather than one per
    turn simultaneously.
    """
    next_user_message = await _await_deferred_grade(session_obj, snap)

    async with _grade_lock(session_obj):
        stale = _deferred_grade_superseded(session_obj, snap)
        if stale and not _grade_despite(stale):
            logger.info("Deferred reflect skipped for %s: %s", snap.session_id, stale)
            # Durable marker so the gap in graded turns is explainable later.
            # "notice" rows are filtered from LLM context by the compiler.
            try:
                await asyncio.to_thread(
                    db.add_message,
                    snap.session_id,
                    "notice",
                    "[reflect deferred — superseded by a newer turn, this turn ungraded]",
                )
            except Exception as e:
                logger.debug("Deferred-reflect supersede notice insert skipped: %s", e)
            return
        if stale:
            logger.info(
                "Deferred reflect grading %s anyway (%s)%s",
                snap.session_id,
                stale,
                " with the user's next message as evidence" if next_user_message else "",
            )

        await _run_deferred_reflect(session_obj, snap, next_user_message=next_user_message)


def _deferred_verdict_notification(session_id: str, result) -> None:
    """Surface a non-pass deferred verdict — the grade alone has no effector.

    Deferred reflect is observe-only by design: it can conclude "retry" with a
    concrete finish strategy, but nothing acts on it (field case 17683100ecf8:
    a correct 3-round finish plan sat unread in post_mortems while the session
    idled). This turns that dead end into a notification carrying the strategy;
    the same strategy still reaches the next scout via build_retry_context when
    the user replies in the session.
    """
    if result.verdict not in ("retry", "escalate"):
        return
    strategy = (result.strategy or result.missing or result.reasoning or "").strip()
    if len(strategy) > 700:
        strategy = strategy[:700] + "…"
    try:
        session = db.get_session(session_id) or {}
        _broadcast_reflect_notification(
            session_id,
            session,
            kind="followup",
            title=f"Turn graded '{result.verdict}' after delivery",
            body=(
                "The background grade found the last turn incomplete "
                "(observe-only — no retry ran). Suggested finish: "
                f"{strategy} — reply in the session to have the agent act on it."
            ),
        )
    except Exception as e:
        logger.warning("Deferred verdict notification failed for %s: %s", session_id, e)


async def _run_deferred_reflect(session_obj, snap: _DeferredGrade, next_user_message: str = "") -> None:
    """Grade the snapshotted turn, observe-only.

    Writes exactly what the synchronous path writes — reflect row, post-mortem,
    lessons, experience, user observations — and nothing else. No retry flag,
    no state transition, no write to session.turn: by the time this runs the
    turn is finished, and a new one may already own the session.

    Observe-only is now load-bearing in a second way: with next-turn grading
    this can run WHILE turn N+1 is in flight. Nothing here may reach the live
    turn, and the evidence is pinned to the snapshotted turn's message window
    so the grade cannot drift forward into work it never saw.
    """
    import json

    session_id = snap.session_id
    # The ref covers the grade itself, not the quiet period — a sleeping task
    # is nothing to protect from the reaper, and holding it for 5 minutes would
    # make every graded session look busy to snooze.
    session_obj.add_background_ref()
    try:
        from core.adaptive import trial
        from core.reflect import reflect_on_session

        messages = await asyncio.to_thread(db.get_messages, session_id, last=REFLECT_TAIL_MESSAGES)
        # Lesson recall keys off "the last user message", which after a reply
        # would be the NEXT turn's ask — clamp to this turn's window first.
        if snap.turn_last_msg_id is not None:
            messages = [m for m in messages if (m.get("id") or 0) <= snap.turn_last_msg_id]
        extra_evidence = await _recall_lesson_evidence(session_id, messages)

        result = await reflect_on_session(
            session_id,
            emit=None,  # reflect.start/.done describe a blocking grade; this one isn't
            attempt=snap.attempt,
            tool_summary=snap.tool_summary or None,
            scout_report=snap.scout_report,
            extra_evidence=extra_evidence,
            turn_user_msg_id=snap.turn_user_msg_id,
            termination_reason=snap.termination_reason,
            prior_termination_reasons=snap.prior_termination_reasons,
            gate_results=snap.gate_results or None,
            reflect_mode="deferred",
            tool_summary_attempts=snap.tool_summary_attempts or None,
            turn_msg_id_range=(snap.turn_user_msg_id, snap.turn_last_msg_id),
            next_user_message=next_user_message,
            # From the snapshot, not the session: with next-turn grading this
            # runs while turn N+1 is in flight, and the live session's key has
            # already moved to it. The arm the grade records has to be the one
            # the graded turn's prompts were built from (W6).
            turn_key=trial.turn_key(snap.session_id, snap.turn_id),
        )

        reflect_event = {
            "verdict": result.verdict,
            "reasoning": result.reasoning,
            "diagnostic": result.diagnostic,
            "what_worked": result.what_worked,
            "what_failed": result.what_failed,
            "strategy": result.strategy,
            "missing": result.missing,
            "failure_cause": result.failure_cause,
            "confidence": result.confidence,
            # Retry disposition and verification state are separate channels
            # (H11): a downgraded non-pass is `verdict=pass` for control flow
            # and `verification=unknown` for everyone who has to trust it.
            "verification": result.verification,
            "verification_reason": result.verification_reason,
            "latency_ms": result.reflect_latency_ms,
            "reflect_model": result.reflect_model,
            # Regime marker: this verdict never had the power to retry the
            # turn, so verdict rates across the two modes are not comparable.
            "reflect_mode": "deferred",
            # What the verdict rests on: the grader alone, or the grader plus
            # the user's own reply to this turn.
            "outcome_source": "next_turn" if next_user_message else "llm",
        }
        await asyncio.to_thread(db.add_message, session_id, "reflect", json.dumps(reflect_event))

        session_obj.emit_event({"type": "reflect.deferred", **reflect_event})
        # The notification's whole ask is "reply in the session so the agent
        # can act on this". When the grade already read the user's reply, they
        # have; the strategy reaches the next scout through build_retry_context
        # either way, and pushing them to answer a message they already
        # answered is pure noise.
        if not next_user_message:
            _deferred_verdict_notification(session_id, result)
        logger.info(
            "Deferred reflect verdict=%s (%s) for session %s (observe-only): %s",
            result.verdict,
            reflect_event["outcome_source"],
            session_id,
            result.reasoning,
        )
    except Exception as e:
        # Observe-only: a failed background grade costs a record, not a turn.
        logger.warning("Deferred reflect failed for %s: %s", session_id, e)
    finally:
        session_obj.remove_background_ref()


async def _maybe_reflect(session_id: str, session: dict, emit=None, session_obj=None, gate_results=None) -> None:
    """Run Reflect verification if session qualifies."""
    if not session_obj:
        return

    # Skip if the agent suspended waiting for user input. Running reflect here
    # produces false negatives: the "final assistant message" is always the
    # procedural ask_user acknowledgment ("I've asked you a question…"), not
    # the real completion report, and any tool errors from earlier in the turn
    # are still visible in the summary. The turn is either complete (agent asked
    # a courtesy confirmation) or genuinely blocked on user input — either way
    # reflect can't make a useful determination, and retrying would be wrong.
    from sessions import state_v2 as sv2

    if sv2._current_state(session_obj) is sv2.SessionStateV2.AWAITING_USER:
        logger.debug("Skipping reflect for %s: session is AWAITING_USER", session_id)
        return

    # Skip if session errored (incomplete evidence, unreliable verdict)
    if session_obj.error:
        return

    # Workers retry more conservatively than main sessions — fan-out cost
    # (scout + agent + reflect per worker per retry) adds up quickly.
    max_retries = (
        settings.reflect_max_retries_worker if session.get("session_type") == "worker" else settings.reflect_max_retries
    )

    # Skip if already at max retries
    if session_obj.turn.reflect_count >= max_retries:
        if emit:
            # Notify user that reflect retries are exhausted
            last_reflect = None
            try:
                # Reflect rows land at the end of a turn — tail read suffices.
                messages = await asyncio.to_thread(db.get_messages, session_id, last=100)
                for msg in reversed(messages):
                    if msg["role"] == "reflect":
                        last_reflect = msg.get("content", "")
                        break
            except Exception:
                pass
            emit(
                {
                    "type": "reflect.exhausted",
                    "attempts": session_obj.turn.reflect_count,
                    "max": max_retries,
                    "last_result": last_reflect or "",
                }
            )
        # Broadcast notification so the user gets a push alert
        _broadcast_reflect_notification(
            session_id,
            session,
            title="Retries exhausted",
            body=f"Reflect gave up after {session_obj.turn.reflect_count} attempt(s).",
        )
        return

    # Quality gate: need enough substance to verify. Scoped to THIS turn —
    # counting the whole session meant any session with history passed the
    # gate, so trivial follow-up turns ("thanks") paid a full reflect LLM
    # call and could draw spurious retry verdicts. Reflect itself is already
    # turn-scoped via turn_user_msg_id; the gate now matches. Turn membership
    # comes from the parent_user_msg_id tag that _save_turn_msg stamps on
    # every assistant/tool row.
    #
    # Tail-bounded: everything read from `messages` below is turn-local (the
    # gate pool, the last user message for lesson recall, the active-skill
    # probe), and a turn lives at the end of the transcript. The bound also
    # caps the no-turn-id fallback's session-wide count, which is harmless —
    # the gate only compares against reflect_min_messages.
    messages = await asyncio.to_thread(db.get_messages, session_id, last=REFLECT_TAIL_MESSAGES)
    turn_msg_id = getattr(session_obj, "current_turn_user_msg_id", None)
    if turn_msg_id is not None:
        import json as _gate_json

        def _in_turn(m: dict) -> bool:
            if m.get("id") == turn_msg_id:
                return True
            try:
                meta = _gate_json.loads(m.get("metadata") or "{}")
            except (ValueError, TypeError):
                return False
            return meta.get("parent_user_msg_id") == turn_msg_id

        gate_pool = [m for m in messages if _in_turn(m)]
    else:
        # No turn id recorded (legacy path / synthetic resume) — fall back to
        # the historical session-wide count rather than skipping reflect.
        gate_pool = messages
    substantive = [m for m in gate_pool if m["role"] in ("user", "assistant", "tool")]
    if len(substantive) < settings.reflect_min_messages:
        # Surface the skip so the UI can render a small marker — without
        # this, a short turn finalizes silently and looks like reflect is
        # missing/broken when it actually skipped on purpose.
        logger.info(
            "Reflect skipped for %s: too few messages (%d/%d)",
            session_id,
            len(substantive),
            settings.reflect_min_messages,
        )
        # Persist a transcript-visible marker so the skip is durable across
        # page reloads (the live SSE event below only updates an open tab).
        # "notice" role is filtered from LLM context by the compiler.
        try:
            await asyncio.to_thread(
                db.add_message,
                session_id,
                "notice",
                f"[reflect skipped — {len(substantive)}/{settings.reflect_min_messages} messages, too short to verify]",
            )
        except Exception as _e:
            logger.debug("Reflect-skipped notice insert skipped: %s", _e)
        if emit:
            emit(
                {
                    "type": "reflect.skipped",
                    "reason": "too-few-messages",
                    "count": len(substantive),
                    "min": settings.reflect_min_messages,
                }
            )
        # Reflect skipped, but gates still enforce (plan 3a): a failing
        # deterministic check requests the retry directly.
        if gate_results:
            _apply_gate_retry_fallback(session_id, session, session_obj, gate_results, emit=emit)
        return

    # Deferred grading (interactive sessions): the verdict is worth having, but
    # not worth holding a human at FINALIZING for it — and with retries off the
    # table for this path, nothing about the turn can change once it lands.
    # Gates already ran synchronously above; their clamp is now the only
    # mechanical retry path for these turns, which is the intent: a failing
    # deterministic check is material by construction, an LLM strictness
    # artifact is not.
    if _reflect_is_deferred(session):
        if gate_results:
            _apply_gate_retry_fallback(session_id, session, session_obj, gate_results, emit=emit)
        # A worker-resume turn synthesizes results the graded turn produced.
        # Grading it would bump _deferred_reflect_seq and cancel the pending
        # grade of the turn that did the work (session 3dc5a307d751).
        if getattr(session_obj, "_turn_id", 0) in (getattr(session_obj, "_synthetic_turn_ids", None) or set()):
            logger.info("Deferred reflect skipped for %s: synthetic worker-resume turn", session_id)
            return
        await _schedule_deferred_reflect(session_id, session, session_obj, gate_results, emit=emit)
        return

    # Pre-reflect enrichment: lessons recall + (when stuck) trial-hint peek at
    # pending skill proposals. extra_evidence is appended to reflect's prompt
    # context, never written into SKILL.md. Stuck = we've already retried at
    # least once on this turn (reflect_count >= 1).
    extra_evidence_parts: list[str] = []
    injected_trial_proposals: list[str] = []
    is_stuck = session_obj.turn.reflect_count >= 1

    lesson_block = await _recall_lesson_evidence(session_id, messages)
    if lesson_block:
        extra_evidence_parts.append(lesson_block)

    if is_stuck:
        try:
            from core.refine import _identify_active_skill

            active_skill = _identify_active_skill(messages)
            if active_skill:
                pending = db.get_pending_proposals_for_skill(active_skill, limit=3)
                if pending:
                    lines = [
                        f"## TRIAL HINTS (unapproved skill proposals for '{active_skill}' "
                        f"— use with caution; report back what helped)"
                    ]
                    for p in pending:
                        pid = p["id"]
                        conf = p.get("confidence", 0.0)
                        problem = (p.get("problem") or "").strip()
                        change = (p.get("proposed_change") or "").strip()
                        lines.append(
                            f"- [proposal {pid[:8]}, confidence {conf:.2f}] "
                            f"Problem: {problem}\n  Proposed fix: {change}"
                        )
                        db.record_proposal_trial_use(pid)
                        injected_trial_proposals.append(pid)
                    extra_evidence_parts.append("\n".join(lines))
        except Exception as e:
            logger.debug("Reflect stuck-mode peek failed for %s: %s", session_id, e)

    extra_evidence = "\n\n".join(extra_evidence_parts)
    # Track on session_obj so the post-verdict success bump can find them.
    session_obj.turn.injected_trial_proposals = injected_trial_proposals

    try:
        from core.reflect import build_retry_context, reflect_on_session

        # Termination history lets reflect detect ceiling-loops (same hard wall
        # hit on multiple consecutive turns). Index 0 is this turn's reason
        # (just logged); [1:] is genuinely prior.
        termination_history: list[str] = []
        try:
            termination_history = await asyncio.to_thread(db.recent_termination_reasons, session_id, limit=3)
        except Exception as e:
            logger.debug("Failed to fetch termination history for %s: %s", session_id, e)

        current_reason = getattr(session_obj, "termination_reason", None)
        prior_reasons = termination_history[1:] if termination_history else []

        result = await reflect_on_session(
            session_id,
            emit=emit,
            attempt=session_obj.turn.reflect_count + 1,
            tool_summary=session_obj.turn.tool_summary or None,
            scout_report=session_obj.last_scout_report,
            extra_evidence=extra_evidence,
            turn_user_msg_id=session_obj.current_turn_user_msg_id,
            termination_reason=current_reason,
            prior_termination_reasons=prior_reasons,
            gate_results=gate_results,
            tool_summary_attempts=session_obj.turn.tool_summary_attempts or None,
        )

        # If trial hints were injected and reflect now reports pass, count
        # those proposals as having helped — weak signal toward approval, never
        # an auto-approval.
        if result.verdict == "pass" and injected_trial_proposals:
            for pid in injected_trial_proposals:
                try:
                    db.record_proposal_trial_success(pid)
                except Exception as e:
                    logger.debug("record_proposal_trial_success failed for %s: %s", pid, e)

        # Persist reflect result as a message for visibility
        import json

        reflect_event = {
            "verdict": result.verdict,
            "reasoning": result.reasoning,
            "diagnostic": result.diagnostic,
            "what_worked": result.what_worked,
            "what_failed": result.what_failed,
            "strategy": result.strategy,
            "missing": result.missing,
            "failure_cause": result.failure_cause,
            "confidence": result.confidence,
            # See the deferred writer above: verdict is the retry disposition,
            # verification is whether anything was actually checked.
            "verification": result.verification,
            "verification_reason": result.verification_reason,
            "latency_ms": result.reflect_latency_ms,
            "reflect_model": result.reflect_model,
            # Regime marker — this verdict ran on the critical path and can
            # still retry the turn. Deferred grades stamp "deferred".
            "reflect_mode": "sync",
        }
        await asyncio.to_thread(db.add_message, session_id, "reflect", json.dumps(reflect_event))

        if result.verdict == "retry":
            session_obj.turn.reflect_count += 1
            session_obj.turn.reflect_lessons = build_retry_context(
                result,
                session_obj.turn.reflect_count,
                max_retries,
                tool_summary=session_obj.turn.tool_summary or None,
            )
            # Failed-gate output rides the lessons channel — the only path
            # the retry attempt's scout message actually reads (plan 3a).
            if gate_results and any(not g.passed for g in gate_results):
                from core.gates import format_retry_guidance

                session_obj.turn.reflect_lessons = (
                    session_obj.turn.reflect_lessons + "\n\n" + format_retry_guidance(gate_results)
                ).strip()
            # Budget guard: refuse a retry if the LLM session-time budget
            # cannot accommodate at least one scout + one agent turn floor.
            # Without this, reflect-retry would push past llm_session_timeout
            # and cascade through scout (180s) → fallback (180s) → first agent
            # acquire, all failing with LLMSessionTimeoutError — exactly the
            # 15ms agent-error after a 220s scout pause we saw on session
            # 7b97cf7ef84a. We need enough headroom for scout's primary attempt
            # plus a minimal agent round; anything tighter is wishful thinking.
            try:
                from core.llm.client import session_seconds_remaining

                remaining = session_seconds_remaining(session_id)
                # Worst-case scout cost = primary attempt + one primary retry on
                # first timeout (runner.py: attempt==1 retries) + fallback model
                # attempt = 3× scout_timeout. Plus 30s for the first agent
                # acquire. Anything tighter and the retry can land past the
                # cap mid-scout and cascade to LLMSessionTimeoutError on the
                # agent — exactly what session 4b184273f4b5 hit (remaining 420s,
                # old guard 390s let it through, scout consumed 420s).
                raw_needed = float(settings.scout_timeout) * 3 + 30.0
                min_needed = min(raw_needed, float(settings.reflect_retry_budget_cap_s))
            except Exception:
                remaining = float("inf")
                min_needed = 0.0
            if remaining < min_needed:
                logger.info(
                    "Reflect retry blocked for session %s: "
                    "%.0fs of LLM budget remain, need ~%.0fs for retry. "
                    "Surfacing as escalate instead.",
                    session_id,
                    remaining,
                    min_needed,
                )
                # Convert verdict from retry → escalate-style termination so
                # the user sees a real reason rather than a mysterious
                # mid-scout failure.
                if emit:
                    emit(
                        {
                            "type": "reflect.budget_exhausted",
                            "remaining_s": int(remaining),
                            "needed_s": int(min_needed),
                            "reasoning": result.reasoning,
                        }
                    )
                _broadcast_reflect_notification(
                    session_id,
                    session,
                    title="Retry skipped — budget exhausted",
                    body=f"Reflect wanted to retry but only {int(remaining)}s " f"of LLM session time remain.",
                )
                # Don't request retry; let the turn end. session_obj.turn.reflect_count
                # has already been incremented so the next run will see it.
                return

            # Cross-retry circuit breaker (audit P1f): when the last two
            # attempts of THIS turn failed with the same signature, a third
            # identical attempt is spend without a plan-change. Stop retrying
            # and surface the repeat instead of amplifying it.
            if session_obj.turn.reflect_count >= 2:
                _turn_started = None
                try:
                    _turn_msg_id = getattr(session_obj, "current_turn_user_msg_id", None)
                    if _turn_msg_id:
                        _turn_row = await asyncio.to_thread(db.get_message, _turn_msg_id)
                        _turn_started = (_turn_row or {}).get("created_at")
                except Exception:
                    _turn_started = None
                repeat_sig = await asyncio.to_thread(_same_failure_repeating, session_id, _turn_started)
                if repeat_sig:
                    logger.warning(
                        "Reflect circuit breaker tripped for session %s after %d attempts: %s",
                        session_id,
                        session_obj.turn.reflect_count,
                        repeat_sig,
                    )
                    if emit:
                        emit(
                            {
                                "type": "reflect.circuit_breaker",
                                "attempts": session_obj.turn.reflect_count,
                                "signature": repeat_sig,
                                "reasoning": result.reasoning,
                            }
                        )
                    _broadcast_reflect_notification(
                        session_id,
                        session,
                        title="Retry stopped — same failure repeating",
                        body=(
                            f"Reflect requested another retry, but the last two attempts "
                            f"failed identically ({repeat_sig[:180]}). Stopping after "
                            f"{session_obj.turn.reflect_count} attempts — this needs a different "
                            f"plan or your input."
                        ),
                    )
                    try:
                        await asyncio.to_thread(
                            db.add_message,
                            session_id,
                            "notice",
                            f"[reflect circuit breaker: last two attempts failed identically "
                            f"({repeat_sig[:180]}) — retries stopped after "
                            f"{session_obj.turn.reflect_count} attempts]",
                        )
                    except Exception as _e:
                        logger.debug("Circuit-breaker notice insert skipped: %s", _e)
                    return

            # Mechanical lesson effector: reflect may name tools to disable on
            # the retry attempt (retry_without_tools). Validate against the
            # registry so a hallucinated name can't silently no-op the filter.
            #
            # Cleared first: the manager resets this at turn start but never
            # between retries, so a tool excluded by retry #1's verdict stayed
            # excluded for retry #2 even when that verdict named nothing. Each
            # retry runs with exactly the exclusions its own verdict asked for.
            session_obj.turn.retry_excluded_tools = set()
            if result.retry_without_tools:
                try:
                    from core.tools.registry import get_registry

                    reg = get_registry()
                    excluded = {t for t in result.retry_without_tools if reg.exists(t)}
                except Exception:
                    excluded = set(result.retry_without_tools)
                if excluded:
                    session_obj.turn.retry_excluded_tools = excluded
                    logger.info(
                        "Retry for session %s will run without tools: %s",
                        session_id,
                        ", ".join(sorted(excluded)),
                    )

            # Only request a retry if the outer loop's gate will honor it.
            # The gate in manager._run_agent_safe is `reflect_count < cap`; with
            # reflect_count just incremented, emit retry iff that check still
            # holds. Otherwise this was the terminal verdict — emit exhausted
            # (matching the top-of-function branch shape) and leave retry_requested
            # False so the outer loop drops cleanly.
            if session_obj.turn.reflect_count < max_retries:
                session_obj.turn.reflect_retry_requested = True
                if emit:
                    emit(
                        {
                            "type": "reflect.retry",
                            "attempt": session_obj.turn.reflect_count,
                            "max": max_retries,
                            "reasoning": result.reasoning,
                            "strategy": result.strategy,
                        }
                    )
                logger.info(
                    "Reflect requesting retry #%d for session %s: %s",
                    session_obj.turn.reflect_count,
                    session_id,
                    result.reasoning,
                )
            else:
                if emit:
                    emit(
                        {
                            "type": "reflect.exhausted",
                            "attempts": session_obj.turn.reflect_count,
                            "max": max_retries,
                            "last_result": json.dumps(reflect_event),
                        }
                    )
                _broadcast_reflect_notification(
                    session_id,
                    session,
                    title="Retries exhausted",
                    body=f"Reflect gave up after {session_obj.turn.reflect_count} attempt(s).",
                )
                logger.info(
                    "Reflect retry requested but cap reached for session %s " "(count=%d, max=%d): %s",
                    session_id,
                    session_obj.turn.reflect_count,
                    max_retries,
                    result.reasoning,
                )

        elif result.verdict == "escalate":
            if emit:
                emit(
                    {
                        "type": "reflect.escalate",
                        "reasoning": result.reasoning,
                        "missing": result.missing,
                    }
                )
            # Broadcast notification so the user gets a push alert
            _broadcast_reflect_notification(
                session_id,
                session,
                title="Needs attention",
                body=result.reasoning[:200],
            )
            logger.info("Reflect escalating session %s: %s", session_id, result.reasoning)

    except Exception as e:
        # The reflect block above is wide — anything from reflect_on_session,
        # the LLM call inside it, db.add_message, or the verdict-handling
        # branches can land here. Two failure modes have actually surfaced
        # in production:
        #   * reflect crashed mid-flight (LLM error, asyncio cancel propagated
        #     as Exception, transient DB lock during add_message)
        #   * the verdict handler itself raised (notification broadcast bug)
        # Either way, leaving the worker with NO reflect row is what trips
        # up an orchestrator — _latest_reflect() returns None, its bookkeeping
        # records verdict='unknown', and downstream steps short-circuit. Write
        # a sentinel reflect row so the engine knows reflect was attempted but
        # failed, distinct from "reflect never ran". logger.exception captures
        # the traceback so we can actually diagnose this next time.
        logger.exception("Reflect failed for %s: %s", session_id, e)
        try:
            import json as _json

            sentinel = {
                "verdict": "error",
                "reasoning": f"reflect crashed: {type(e).__name__}: {str(e)[:200]}",
                "diagnostic": "",
                "what_worked": "",
                "what_failed": "",
                "strategy": "",
                "missing": "",
                "failure_cause": "env",
                "confidence": 0.0,
                "verification": "unknown",
                "verification_reason": "reflect crashed before it could check anything",
                "latency_ms": 0,
                "_sentinel": True,
            }
            await asyncio.to_thread(db.add_message, session_id, "reflect", _json.dumps(sentinel))
        except Exception as persist_err:
            logger.error(
                "Could not persist reflect-failure sentinel for %s: %s",
                session_id,
                persist_err,
            )
