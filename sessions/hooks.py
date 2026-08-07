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
        gate_results = await _run_turn_gates(session_id, session_obj, emit=emit)

    # Reflect: post-execution verification
    if settings.reflect_enabled and session_obj:
        await _maybe_reflect(session_id, session, emit=emit, session_obj=session_obj, gate_results=gate_results)
    elif gate_results and session_obj:
        _apply_gate_retry_fallback(session_id, session, session_obj, gate_results, emit=emit)

    # Evaluation: feature QA against acceptance criteria
    if settings.eval_auto and session_obj:
        await _maybe_evaluate(session_id, session, emit=emit, session_obj=session_obj)

    # Candor: feed this turn's operational outcomes to the add-on store.
    # Runs after reflect so the verdict is available. Mechanical, no LLM.
    if settings.candor_enabled and session_obj:
        await _maybe_candor(session_id, session, session_obj=session_obj)

    # TELOS: trace the turn and mint questions from anomalies. Runs after
    # Candor so this turn's outcomes are already in the reliability record
    # the surprise priors read from. Mechanical, no LLM.
    if settings.telos_enabled and session_obj:
        await _maybe_telos(session_id, session, session_obj=session_obj)


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

        response = await client.chat(
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


async def _maybe_candor(session_id: str, session: dict, session_obj=None) -> None:
    """Emit this turn's operational outcomes to the Candor add-on store.

    Delta-tracked against session_obj._candor_emitted (keyed by turn id) so a
    reflect-retry re-entry — post-hooks run once per attempt — never
    double-observes the earlier attempt's tool calls. Failure is never fatal:
    a Candor problem logs a warning and the turn completes normally.
    """
    # Canary isolation (plan §5): deliberately-hard synthetic tasks would
    # poison the reliability ledger Phase 4 consumes. §10.9 revisits a
    # separate ledger namespace for calibration.
    if session.get("session_type") == "canary":
        return

    import time as _time

    try:
        from core.extensions.candor.bridge import get_candor_bridge
        from core.extensions.candor.emit import build_turn_observations

        turn_id = getattr(session_obj, "current_turn_user_msg_id", None)
        prev = getattr(session_obj, "_candor_emitted", None)
        if not isinstance(prev, dict) or prev.get("turn") != turn_id:
            prev = {"turn": turn_id, "tools": {}}

        verdict = failure_cause = None
        stash = getattr(session_obj, "_candor_reflect", None)
        if stash and stash[0] == turn_id:
            _, verdict, failure_cause = stash

        model = getattr(session_obj, "model_override", None) or settings.llm_model or "default"
        observations, emitted = build_turn_observations(
            tool_summary=session_obj.last_tool_summary or {},
            already_emitted=prev["tools"],
            termination_reason=getattr(session_obj, "termination_reason", None),
            reflect_verdict=verdict,
            failure_cause=failure_cause,
            model=model,
            session_kind=session.get("session_type") or "normal",
            is_retry=bool(session_obj.reflect_count),
            ts_ms=int(_time.time() * 1000),
            max_obs=settings.candor_max_obs_per_turn,
        )
        session_obj._candor_emitted = {"turn": turn_id, "tools": emitted}
        if len(observations) >= settings.candor_max_obs_per_turn:
            logger.warning(
                "Candor emission hit the per-turn cap (%d) — excess dropped", settings.candor_max_obs_per_turn
            )
        if observations:
            # Bounded wait: post-hooks block turn completion. If the bridge
            # executor is busy (e.g. a gate sweep is finishing), the job still
            # runs to completion after we stop waiting — data is late, not lost.
            await asyncio.wait_for(get_candor_bridge().record(observations), timeout=10)
    except asyncio.TimeoutError:
        logger.warning("Candor record still queued after 10s — continuing without waiting")
    except Exception as e:
        logger.warning("Candor emission failed for %s: %s", session_id, e)


async def _maybe_telos(session_id: str, session: dict, session_obj=None) -> None:
    """Feed this turn to the TELOS layer: trace append + anomaly->question
    minting. Delta-tracked per turn inside on_post_task. Failure is never
    fatal — a TELOS problem logs a warning and the turn completes normally."""
    try:
        from core.telos.anomaly import on_post_task

        await asyncio.wait_for(on_post_task(session_id, session, session_obj), timeout=10)
    except asyncio.TimeoutError:
        logger.warning("TELOS post-task hook still running after 10s — continuing without waiting")
    except Exception as e:
        logger.warning("TELOS post-task hook failed for %s: %s", session_id, e)


def _broadcast_reflect_notification(
    session_id: str,
    session: dict,
    title: str,
    body: str,
) -> None:
    """Broadcast a dialog.notification for reflect events so push/webhook fire."""
    from core.events import get_event_bus
    from sessions.manager import get_manager

    session_title = session.get("title", "")
    label = f"{session_title}: {title}" if session_title else title

    notification = {
        "type": "dialog.notification",
        "title": label,
        "body": body,
        "urgency": "high",
        "source_session_id": session_id,
    }

    # Persist so the bell panel can display it
    nid = db.add_notification(
        session_id=session_id,
        title=label,
        body=body,
        urgency="high",
    )
    notification["notification_id"] = nid

    get_manager().broadcast(notification)
    get_event_bus().emit({**notification, "session_id": session_id})


async def _run_turn_gates(session_id: str, session_obj, emit=None) -> list:
    """Execute this attempt's gates (blocking runner via to_thread), persist
    a transcript-visible eval row, emit SSE. Never raises."""
    import json as _json

    from core.gates import run_gates_for_turn

    try:
        attempt = session_obj.reflect_count + 1
        results = await asyncio.to_thread(run_gates_for_turn, session_id, session_obj, attempt)
    except Exception as e:
        logger.warning("Gate execution failed for %s: %s", session_id, e)
        return []
    if not results:
        return []
    failed = [r for r in results if not r.passed]
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
            }
        )
    return results


def _same_failure_repeating(session_id: str) -> str | None:
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
    """When Reflect doesn't run (disabled, or skipped for a short turn), a
    failing gate still requests the retry — subject to the same cap Reflect
    honors. AWAITING_USER and errored turns deliberately get no fallback:
    waiting on a human is a legitimate block, and error turns lack reliable
    evidence."""
    from core.gates import failing, format_retry_guidance

    bad = failing(gate_results)
    if not bad:
        return
    max_retries = (
        settings.reflect_max_retries_worker if session.get("session_type") == "worker" else settings.reflect_max_retries
    )
    if session_obj.reflect_count >= max_retries:
        logger.info("Gates failing for %s but retry cap reached (%d)", session_id, session_obj.reflect_count)
        return
    session_obj.reflect_count += 1
    guidance = format_retry_guidance(gate_results)
    session_obj.reflect_lessons = ((session_obj.reflect_lessons or "") + "\n\n" + guidance).strip()
    session_obj.reflect_retry_requested = True
    logger.info(
        "Gate retry fallback: requesting retry #%d for %s (%s failing, reflect skipped)",
        session_obj.reflect_count,
        session_id,
        ", ".join(g.name for g in bad),
    )
    if emit:
        emit(
            {
                "type": "reflect.retry",
                "attempt": session_obj.reflect_count,
                "max": max_retries,
                "reasoning": f"deterministic gate failure ({', '.join(g.name for g in bad)}); reflect skipped",
                "strategy": "",
            }
        )


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
    if session_obj.reflect_count >= max_retries:
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
                    "attempts": session_obj.reflect_count,
                    "max": max_retries,
                    "last_result": last_reflect or "",
                }
            )
        # Broadcast notification so the user gets a push alert
        _broadcast_reflect_notification(
            session_id,
            session,
            title="Retries exhausted",
            body=f"Reflect gave up after {session_obj.reflect_count} attempt(s).",
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

    # Pre-reflect enrichment: lessons recall + (when stuck) trial-hint peek at
    # pending skill proposals. extra_evidence is appended to reflect's prompt
    # context, never written into SKILL.md. Stuck = we've already retried at
    # least once on this turn (reflect_count >= 1).
    extra_evidence_parts: list[str] = []
    injected_trial_proposals: list[str] = []
    is_stuck = session_obj.reflect_count >= 1

    try:
        last_user_msg = next(
            (m["content"] for m in reversed(messages) if m["role"] == "user" and m.get("content")),
            "",
        )
        if last_user_msg:
            from core.memory.store import get_memory_store

            store = get_memory_store()
            if store:
                lessons = await asyncio.to_thread(store.search_lessons, last_user_msg, limit=3)
                if lessons:
                    import time as _t

                    _now_ts = int(_t.time())
                    lines = ["## Past lessons that may apply (verify current relevance — codebase moves fast)"]
                    for r in lessons:
                        _age_d = max(0, (_now_ts - int(r.entry.epoch or _now_ts)) // 86400)
                        _age_s = f"{_age_d}d ago" if _age_d > 0 else "today"
                        lines.append(f"- [{r.entry.file_name}, {_age_s}] {r.entry.content[:400]}")
                    extra_evidence_parts.append("\n".join(lines))
    except Exception as e:
        logger.debug("Reflect lesson recall failed for %s: %s", session_id, e)

    if is_stuck:
        try:
            from core.snooze_reflect import _identify_active_skill

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
    session_obj._injected_trial_proposals = injected_trial_proposals

    try:
        from core.reflect import build_retry_context, reflect_on_session

        # Termination history lets reflect detect ceiling-loops (same hard wall
        # hit on multiple consecutive turns). Index 0 is this turn's reason
        # (just logged); [1:] is genuinely prior.
        termination_history: list[str] = []
        try:
            termination_history = db.recent_termination_reasons(session_id, limit=3)
        except Exception as e:
            logger.debug("Failed to fetch termination history for %s: %s", session_id, e)

        current_reason = getattr(session_obj, "termination_reason", None)
        prior_reasons = termination_history[1:] if termination_history else []

        result = await reflect_on_session(
            session_id,
            emit=emit,
            attempt=session_obj.reflect_count + 1,
            tool_summary=session_obj.last_tool_summary or None,
            scout_report=session_obj.last_scout_report,
            extra_evidence=extra_evidence,
            turn_user_msg_id=session_obj.current_turn_user_msg_id,
            termination_reason=current_reason,
            prior_termination_reasons=prior_reasons,
            gate_results=gate_results,
        )

        # Stash the verdict for _maybe_candor (which runs after reflect).
        # Keyed by turn id so a turn where reflect is skipped can't inherit a
        # stale verdict from an earlier turn.
        session_obj._candor_reflect = (
            session_obj.current_turn_user_msg_id,
            result.verdict,
            result.failure_cause,
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
            "latency_ms": result.reflect_latency_ms,
            "reflect_model": result.reflect_model,
        }
        await asyncio.to_thread(db.add_message, session_id, "reflect", json.dumps(reflect_event))

        if result.verdict == "retry":
            session_obj.reflect_count += 1
            session_obj.reflect_lessons = build_retry_context(
                result,
                session_obj.reflect_count,
                max_retries,
                tool_summary=session_obj.last_tool_summary or None,
            )
            # Failed-gate output rides the lessons channel — the only path
            # the retry attempt's scout message actually reads (plan 3a).
            if gate_results and any(not g.passed for g in gate_results):
                from core.gates import format_retry_guidance

                session_obj.reflect_lessons = (
                    session_obj.reflect_lessons + "\n\n" + format_retry_guidance(gate_results)
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
                # Don't request retry; let the turn end. session_obj.reflect_count
                # has already been incremented so the next run will see it.
                return

            # Cross-retry circuit breaker (audit P1f): when the last two
            # attempts of THIS turn failed with the same signature, a third
            # identical attempt is spend without a plan-change. Stop retrying
            # and surface the repeat instead of amplifying it.
            if session_obj.reflect_count >= 2:
                repeat_sig = await asyncio.to_thread(_same_failure_repeating, session_id)
                if repeat_sig:
                    logger.warning(
                        "Reflect circuit breaker tripped for session %s after %d attempts: %s",
                        session_id,
                        session_obj.reflect_count,
                        repeat_sig,
                    )
                    if emit:
                        emit(
                            {
                                "type": "reflect.circuit_breaker",
                                "attempts": session_obj.reflect_count,
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
                            f"{session_obj.reflect_count} attempts — this needs a different "
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
                            f"{session_obj.reflect_count} attempts]",
                        )
                    except Exception as _e:
                        logger.debug("Circuit-breaker notice insert skipped: %s", _e)
                    return

            # Mechanical lesson effector: reflect may name tools to disable on
            # the retry attempt (retry_without_tools). Validate against the
            # registry so a hallucinated name can't silently no-op the filter.
            if result.retry_without_tools:
                try:
                    from core.tools.registry import get_registry

                    reg = get_registry()
                    excluded = {t for t in result.retry_without_tools if reg.exists(t)}
                except Exception:
                    excluded = set(result.retry_without_tools)
                if excluded:
                    session_obj.retry_excluded_tools = excluded
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
            if session_obj.reflect_count < max_retries:
                session_obj.reflect_retry_requested = True
                if emit:
                    emit(
                        {
                            "type": "reflect.retry",
                            "attempt": session_obj.reflect_count,
                            "max": max_retries,
                            "reasoning": result.reasoning,
                            "strategy": result.strategy,
                        }
                    )
                logger.info(
                    "Reflect requesting retry #%d for session %s: %s",
                    session_obj.reflect_count,
                    session_id,
                    result.reasoning,
                )
            else:
                if emit:
                    emit(
                        {
                            "type": "reflect.exhausted",
                            "attempts": session_obj.reflect_count,
                            "max": max_retries,
                            "last_result": json.dumps(reflect_event),
                        }
                    )
                _broadcast_reflect_notification(
                    session_id,
                    session,
                    title="Retries exhausted",
                    body=f"Reflect gave up after {session_obj.reflect_count} attempt(s).",
                )
                logger.info(
                    "Reflect retry requested but cap reached for session %s " "(count=%d, max=%d): %s",
                    session_id,
                    session_obj.reflect_count,
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
        # up the workflow engine — _latest_reflect() returns None, the manifest
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


async def _maybe_evaluate(session_id: str, session: dict, emit=None, session_obj=None) -> None:
    """Run feature evaluation if registry exists and has pending features."""
    if not session_obj:
        return

    # Skip workers (parent evaluates)
    if session.get("session_type") == "worker":
        return

    # Skip if session errored
    if session_obj.error:
        return

    # Skip if already at max retries
    if session_obj.eval_count >= settings.eval_max_retries:
        if emit:
            emit(
                {
                    "type": "eval.exhausted",
                    "attempts": session_obj.eval_count,
                    "max": settings.eval_max_retries,
                }
            )
        return

    # Check for feature registry — no registry means nothing to evaluate
    import json
    from pathlib import Path

    registry_path = Path("data/registry.json")
    if not registry_path.exists():
        return

    try:
        features = json.loads(registry_path.read_text())
    except (json.JSONDecodeError, OSError):
        return

    # Scope to features THIS session registered. data/registry.json is a
    # single global file, so without this filter every session's post-hooks
    # evaluated every other session's pending features — against its own
    # unrelated transcript, which fails by construction. A failing feature
    # then re-evaluated forever, in every session, burning an eval LLM call
    # and writing an `eval` row per turn, and could drive spurious eval
    # retries. (Observed: a "Neo Flappy Bird" feature registered by session
    # 505639e37185 evaluating inside an unrelated weather session.)
    #
    # Features written before this filter existed have no session_id; treat
    # those as belonging to whoever is running so they still get a chance to
    # pass and retire, rather than being stranded pending forever.
    pending = [f for f in features if not f.get("passes") and f.get("session_id") in (session_id, None, "")]
    if not pending:
        return

    try:
        from core.extensions.evaluation import evaluate_single_async

        if emit:
            emit({"type": "eval.start", "features": len(pending)})

        results = []
        any_failed = False
        feedback_parts = []

        for feat in pending:
            result = await evaluate_single_async(feat, session_id)
            passed = result.get("passed", False)
            results.append(
                {
                    "feature": feat.get("id", ""),
                    "title": feat.get("title", ""),
                    "passed": passed,
                    "scores": result.get("scores", {}),
                    "feedback": result.get("feedback", ""),
                }
            )
            if not passed:
                any_failed = True
                if result.get("feedback"):
                    feedback_parts.append(f"{feat.get('title', feat.get('id', '?'))}: {result['feedback']}")

        # Persist as eval message
        eval_event = {"results": results, "all_passed": not any_failed}
        await asyncio.to_thread(db.add_message, session_id, "eval", json.dumps(eval_event))

        # Emit a typed event so the UI can render the same card live that it
        # builds from the persisted eval row on history reload. Without this
        # the only feedback during a turn was the unstructured eval.pass /
        # eval.retry / eval.exhausted notices.
        if emit:
            emit({"type": "eval.done", **eval_event})

        # Close the auto-eval loop: when the judge passes a feature, mark
        # it passed in the registry. Without this, the feature stays
        # "pending" and the post-hook re-evaluates it on every subsequent
        # turn — burning eval LLM calls and eventually hitting eval_max_retries.
        # Observed: a single passing palindrome_check feature got re-evaluated
        # across 5 sessions before this fix.
        if results:
            try:
                from core.extensions.planning import mark_feature_passed as _mark

                for r in results:
                    if r.get("passed") and r.get("feature"):
                        _mark(r["feature"])
            except Exception as _mark_err:
                logger.debug("Auto-mark-passed skipped: %s", _mark_err)

        if any_failed and session_obj.eval_count < settings.eval_max_retries:
            session_obj.eval_count += 1
            session_obj.eval_retry_requested = True
            if emit:
                emit(
                    {
                        "type": "eval.retry",
                        "attempt": session_obj.eval_count,
                        "max": settings.eval_max_retries,
                        "feedback": "\n".join(feedback_parts),
                    }
                )
            logger.info("Eval requesting retry #%d for session %s", session_obj.eval_count, session_id)
        elif not any_failed:
            if emit:
                emit({"type": "eval.pass", "features": len(results)})
            logger.info("All %d features passed evaluation for session %s", len(results), session_id)

    except Exception as e:
        logger.warning("Evaluation failed for %s: %s", session_id, e)
