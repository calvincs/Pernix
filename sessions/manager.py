"""Pernix — Session manager: lifecycle, prompt routing, event broadcasting."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Awaitable, Callable

from config import settings
from db import models as db
from sessions import state_v2 as sv2
from sessions.state import AgentSession, PendingMessage

logger = logging.getLogger("pernix.sessions")

# Rapid-fire window: when a message lands while the session is busy AND the
# previous queued message arrived within this many seconds, treat them as one
# burst and combine into a single combined queue entry. Outside the window the
# new message becomes a fresh queue entry, so legitimate "do this NEXT after
# the current turn" still produces its own turn. Mid-turn injection has its
# own dedicated endpoint (POST /api/chat/inject) and is unaffected.
RAPID_FIRE_WINDOW_SECONDS = 3.0
_COMBINED_PREFIX = "[Combined rapid-fire messages]"
_COMBINED_ITEM_RE = re.compile(r"^(\d+)\.\s", re.MULTILINE)


def _combine_rapid_fire(existing: str, addition: str) -> str:
    """Fold a follow-up message into a prior queue entry's content.

    First combine: existing="hi", addition="oops" -> the prefix block with
    items 1 and 2. Subsequent combines append the next-numbered item to the
    block. Format is human-readable so it's obvious in the transcript what
    happened.
    """
    if existing.startswith(_COMBINED_PREFIX):
        nums = [int(m.group(1)) for m in _COMBINED_ITEM_RE.finditer(existing)]
        next_num = (max(nums) if nums else 0) + 1
        return f"{existing}\n\n{next_num}. {addition}"
    return f"{_COMBINED_PREFIX}\n\n1. {existing}\n\n2. {addition}"


def _check_session_budget_or_raise(session_id: str) -> None:
    """Raise LLMSessionTimeoutError if the session's LLM time budget is gone.

    Called at the top of scout phases (initial turn and reflect/eval retry)
    to fail a turn cleanly via the existing scout-error path rather than
    letting run_scout's primary + fallback + the agent's first acquire each
    raise LLMSessionTimeoutError, producing three ERROR log lines and a
    confusing 0-content agent-error finalize for the same turn.
    """
    try:
        from core.llm.client import session_seconds_remaining

        remaining = session_seconds_remaining(session_id)
    except Exception:
        return
    if remaining <= 0.0:
        from core.llm.semaphore import LLMSessionTimeoutError

        raise LLMSessionTimeoutError(
            f"Session {session_id[:12]} LLM time budget "
            f"exhausted (>{settings.llm_session_timeout:.0f}s) — "
            f"turn aborted before scout"
        )


def _broadcast_session_timeout_notification(session) -> None:
    """Notify the user that a session ran out of LLM time budget.

    Recovery is one user message away: SessionManager.prompt() resets the
    wall-clock budget on every fresh turn (and on every answer to an
    ask_user). The notification surfaces *which* session needs that nudge
    so the user doesn't have to guess from a silent UI.

    Best-effort. Never re-raises — failure to notify must not mask the
    underlying error path. Fires on the same channels as reflect
    notifications: db.notifications (bell panel), session SSE
    (dialog.notification event), and global event bus (push subscribers).
    """
    try:
        session_id = session.session_id
        # AgentSession is the in-memory shape; the human title lives on
        # the DB row. Fall back to the short id if the lookup fails.
        try:
            row = db.get_session(session_id) or {}
            session_title = (row.get("title") or "").strip() or session_id[:8]
        except Exception:
            session_title = session_id[:8]
        title = "Session ran out of LLM time"
        body = (
            f'"{session_title}" hit its wall-clock LLM budget. '
            "Send a new message to give it a fresh time window — your reply "
            "resets the clock."
        )
        notification = {
            "type": "dialog.notification",
            "title": title,
            "body": body,
            "urgency": "high",
            "source_session_id": session_id,
        }
        try:
            nid = db.add_notification(
                session_id=session_id,
                title=title,
                body=body,
                urgency="high",
            )
            notification["notification_id"] = nid
        except Exception as _e:
            logger.debug(
                "Persisting session-timeout notification for %s failed: %s",
                session_id,
                _e,
            )
        # Session-scoped event for SSE clients on this session's stream.
        try:
            session.emit_event(notification)
        except Exception as _e:
            logger.debug("emit_event for session timeout failed: %s", _e)
        # Global event bus for push subscribers + global notification stream.
        try:
            from core.events import get_event_bus

            get_event_bus().emit({**notification, "session_id": session_id})
        except Exception as _e:
            logger.debug("event_bus emit for session timeout failed: %s", _e)
    except Exception as _outer:
        logger.warning(
            "Broadcasting session-timeout notification failed: %s",
            _outer,
        )


def _map_termination_to_v2_reason(tr: str | None) -> tuple[str, sv2.TerminationReason]:
    """Translate agent.py's legacy termination_reason string into a v2
    (reason, TerminationReason) pair for the PROCESSING → FINALIZING edge."""
    mapping = {
        "complete": ("loop-complete", sv2.TerminationReason.COMPLETE),
        "round_ceiling": ("round-ceiling", sv2.TerminationReason.ROUND_CEILING),
        "compaction_failed": ("compaction-failed", sv2.TerminationReason.COMPACTION_FAILED),
        # agent.py uses `return` (not `raise`) for stream/failover errors, so no
        # exception propagates to _run_agent_safe's except block. The finally's
        # PROCESSING branch uses this mapping — without "error" here it would
        # fall through to the default and log the turn as "loop-complete/complete".
        "error": ("agent-error", sv2.TerminationReason.ERROR),
        # Soft-land for LLM session-time budget exhaustion. Treat as a clean
        # transition (loop-complete) so the turn ends in IDLE_READY rather
        # than going through the error path, but record BUDGET_EXHAUSTED in
        # the state log so post-mortem queries can distinguish "agent ran
        # out of LLM time mid-turn" from a genuine "complete" outcome.
        "budget_exhausted": ("loop-complete", sv2.TerminationReason.BUDGET_EXHAUSTED),
    }
    key = tr or "complete"
    if key not in mapping:
        # A termination reason added in agent.py but not mapped here would
        # silently log the turn as a clean "complete" — make the drift loud.
        logger.warning(
            "Unknown termination_reason %r — defaulting to loop-complete/complete; update _map_termination_to_v2_reason",
            tr,
        )
    return mapping.get(key, ("loop-complete", sv2.TerminationReason.COMPLETE))


class SessionManager:
    """Manages in-memory session state and routes prompts to the agent loop."""

    def __init__(self):
        self._sessions: dict[str, AgentSession] = {}
        self._agent_runner: Callable | None = None  # set by agent module on init
        self._global_subscribers: list[asyncio.Queue] = []  # global notification listeners
        # Strong refs for detached recovery tasks — see _spawn_detached.
        self._detached_tasks: set[asyncio.Task] = set()

    def _spawn_detached(self, coro, label: str) -> asyncio.Task:
        """Schedule a fire-and-forget task, retaining a reference to it.

        asyncio holds only a WEAK reference to a running task, so a bare
        create_task() whose result is discarded can be garbage-collected
        mid-flight. An exception inside one is also never retrieved: it
        surfaces at most as an "exception was never retrieved" warning at
        interpreter exit, long after the fact.

        Every caller here is a recovery path — draining the queue after a
        reaper unstick, resuming a parent whose watch-set just emptied — so
        a silent failure leaves a session idle on queued work until the next
        5-minute reaper tick happens to notice, with nothing in the log to
        explain the pause. Mirrors MaintenanceRunner.track_task.
        """
        task = asyncio.create_task(coro)
        self._detached_tasks.add(task)

        def _done(t: asyncio.Task) -> None:
            self._detached_tasks.discard(t)
            if t.cancelled():
                logger.debug("Detached task %s cancelled", label)
                return
            exc = t.exception()
            if exc is not None:
                logger.error("Detached task %s failed: %s", label, exc, exc_info=exc)

        task.add_done_callback(_done)
        return task

    def set_agent_runner(self, runner: Callable[..., Awaitable]) -> None:
        """Register the agent loop function. Called once at startup."""
        self._agent_runner = runner

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def get_or_create(self, session_id: str) -> AgentSession:
        """Get existing in-memory session or create from DB.

        Restores `_state_v2` and `_watched_worker_ids` from the DB row so
        a restart can resume sessions suspended in AWAITING_WORKERS — the
        legacy `state` column maps that v2 state to "idle" and would
        otherwise drop the watch-set entirely."""
        if session_id in self._sessions:
            return self._sessions[session_id]

        db_session = db.get_session(session_id)
        if not db_session:
            raise ValueError(f"Session {session_id} not found in database")

        session = AgentSession(
            session_id=session_id,
            session_type=db_session.get("session_type", "normal"),
            parent_session_id=db_session.get("parent_session_id"),
        )

        # Restore v2 state if persisted (migration v16+).
        persisted_v2 = db_session.get("state_v2")
        if persisted_v2:
            try:
                session._state_v2 = sv2.SessionStateV2(persisted_v2)
            except ValueError:
                logger.warning(
                    "Unknown state_v2 value %r for session %s; " "falling back to legacy mirror",
                    persisted_v2,
                    session_id,
                )

        # Restore _turn_id from state_log so post-restart code that uses
        # `_turn_id > 0` as a "has started" signal still works. Without
        # this, reconcile_awaiting_workers can't tell completed workers
        # apart from never-started ones after rehydrate.
        try:
            session._turn_id = db.latest_turn_id(session_id)
        except Exception as e:
            logger.debug("latest_turn_id lookup failed for %s: %s", session_id, e)

        # Restore watch-set if persisted.
        watched_json = db_session.get("watched_worker_ids") or "[]"
        try:
            import json as _json

            ids = _json.loads(watched_json)
            if isinstance(ids, list):
                session._watched_worker_ids = set(ids)
        except (ValueError, TypeError) as e:
            logger.warning("Could not parse watched_worker_ids for %s: %s", session_id, e)

        self._sessions[session_id] = session
        return session

    async def reconcile_awaiting_workers(self) -> int:
        """Boot-time sweep: hydrate every session persisted in
        AWAITING_WORKERS and resume any whose watched workers already
        finished while the server was down.

        Returns the number of sessions resumed. Sessions whose workers
        are still running stay in AWAITING_WORKERS — the reaper's
        existing safety net + worker-timeout backstop will catch them.
        """
        try:
            rows = db.get_sessions_in_state_v2(
                sv2.SessionStateV2.AWAITING_WORKERS.value,
            )
        except Exception as e:
            logger.error("reconcile_awaiting_workers: DB sweep failed: %s", e)
            return 0
        resumed = 0
        for row in rows:
            sid = row["id"]
            try:
                parent = self.get_or_create(sid)
            except ValueError:
                continue  # row was deleted between sweep and hydrate
            watched = set(parent._watched_worker_ids)
            if not watched:
                # Boot reconcile finds an AWAITING_WORKERS session with an
                # empty watch-set — broken state. Push to IDLE_READY.
                logger.warning(
                    "Boot reconcile: %s in AWAITING_WORKERS with empty " "watch-set; force-resuming",
                    sid,
                )
                try:
                    sv2.transition(parent, sv2.SessionStateV2.IDLE_READY, "reaper-unstick")
                except Exception as _e:
                    logger.error("reconcile force-resume failed: %s", _e)
                continue
            # Check whether any of the watched workers already finished.
            stale: set[str] = set()
            for wid in list(watched):
                # Worker may not be in memory after restart — hydrate it.
                try:
                    w = self.get_or_create(wid)
                except ValueError:
                    stale.add(wid)  # worker deleted while server was down
                    continue
                w_v2 = sv2._current_state(w)
                has_started = w.task is not None or getattr(w, "_turn_id", 0) > 0
                if w_v2 is sv2.SessionStateV2.IDLE_READY:
                    if has_started or w.error or w.termination_reason:
                        stale.add(wid)
            if stale:
                watched -= stale
                parent._watched_worker_ids = watched
                self._persist_watched(parent)
                logger.info(
                    "Boot reconcile: purged %d completed/missing worker(s) " "from %s's watch-set",
                    len(stale),
                    sid,
                )
            if not watched:
                logger.info(
                    "Boot reconcile: all watched workers for %s have " "completed; resuming",
                    sid,
                )
                try:
                    await self._resume_from_workers(parent)
                    resumed += 1
                except Exception as e:
                    logger.error("reconcile resume failed for %s: %s", sid, e)
        return resumed

    async def reconcile_processing_sessions(self) -> int:
        """Boot-time sweep: find sessions persisted in PROCESSING and reset to IDLE_READY.

        Any session in PROCESSING at startup has a dead agent task (the server
        restarted before the turn could complete). Reset them immediately so users
        can re-prompt rather than waiting up to 5 minutes for the reaper to fire.
        Mirrors reconcile_awaiting_workers() which handles the AWAITING_WORKERS case.
        Returns the count of sessions reset.
        """
        try:
            rows = db.get_sessions_in_state_v2(sv2.SessionStateV2.PROCESSING.value)
        except Exception as e:
            logger.error("reconcile_processing_sessions: DB sweep failed: %s", e)
            return 0
        # Also catch sessions where state_v2 was never persisted (server crashed
        # before the DB write completed) but legacy state='processing' was written.
        try:
            legacy_rows = db.get_sessions_in_legacy_processing_only()
            seen = {r["id"] for r in rows}
            rows = rows + [r for r in legacy_rows if r["id"] not in seen]
        except Exception:
            pass  # best-effort; the state_v2 sweep is the primary path

        reset = 0
        for row in rows:
            sid = row["id"]
            try:
                session = self.get_or_create(sid)
            except ValueError:
                continue
            current = sv2._current_state(session)
            is_legacy_null = row.get("state_v2") in (None, "")

            if current is sv2.SessionStateV2.PROCESSING:
                # Normal v2 path: state_v2='processing' was persisted and restored.
                logger.warning("Boot reconcile: resetting stuck PROCESSING session %s", sid[:12])
                try:
                    sv2.transition(session, sv2.SessionStateV2.IDLE_READY, "reaper-unstick")
                except Exception as _e:
                    logger.error("reconcile_processing reset failed for %s: %s", sid, _e)
                    continue
            elif is_legacy_null and current is sv2.SessionStateV2.IDLE_READY:
                # Legacy path: state_v2 was NULL (crash before the v2 column was
                # written). get_or_create already defaults to IDLE_READY in memory
                # (correct end state), but the DB still has state='processing'.
                # Fix the stale DB row so subsequent restarts don't re-detect it.
                logger.warning(
                    "Boot reconcile: fixing stale legacy processing state for session %s (state_v2 was NULL)",
                    sid[:12],
                )
                try:
                    db.set_session_state_v2(sid, sv2.SessionStateV2.IDLE_READY.value)
                except Exception as _e:
                    logger.error("reconcile_processing legacy fix failed for %s: %s", sid, _e)
                    continue
            else:
                continue  # already recovered or in an unexpected state

            session.emit_event(
                {
                    "type": "system",
                    "content": (
                        "Session was stuck in processing (server restarted) and has been reset. "
                        "You can send a new message."
                    ),
                }
            )
            reset += 1
        return reset

    async def reconcile_interrupted_sessions(self) -> int:
        """Boot-time sweep for the remaining non-terminal v2 states.

        reconcile_processing_sessions/reconcile_awaiting_workers cover
        PROCESSING and AWAITING_WORKERS, but a crash during scout,
        compaction, cancel, pause, or finalize persists those states too.
        No asyncio task survives a restart, so resetting to IDLE_READY is
        always safe — without this, a new prompt queues behind a phantom
        turn (or is rejected for CANCELLING) until the reaper's 5-minute
        cadence catches up, 5-15 minutes of silent unresponsiveness.

        AWAITING_USER is deliberately NOT swept: a pending question
        legitimately survives restarts and the /answer endpoint
        transitions it out.
        """
        # Per-state reason that the transition table accepts toward IDLE_READY.
        sweep: list[tuple[sv2.SessionStateV2, str]] = [
            (sv2.SessionStateV2.SCOUTING, "reaper-unstick"),
            (sv2.SessionStateV2.PAUSE_REQUESTED, "reaper-unstick"),
            (sv2.SessionStateV2.PAUSED, "reaper-unstick"),
            (sv2.SessionStateV2.CANCELLING, "cancel-timeout"),
            (sv2.SessionStateV2.FINALIZING, "finalize-error"),
            (sv2.SessionStateV2.COMPACTING, "compaction-failed"),  # routes via FINALIZING
        ]
        reset = 0
        for target_state, reason in sweep:
            try:
                rows = db.get_sessions_in_state_v2(target_state.value)
            except Exception as e:
                logger.error("reconcile_interrupted: DB sweep failed for %s: %s", target_state.value, e)
                continue
            for row in rows:
                sid = row["id"]
                try:
                    session = self.get_or_create(sid)
                except ValueError:
                    continue
                if sv2._current_state(session) is not target_state:
                    continue  # already recovered
                logger.warning(
                    "Boot reconcile: resetting stuck %s session %s",
                    target_state.value,
                    sid[:12],
                )
                try:
                    session.cancel_requested = False
                    session.pause_event.set()  # IDLE_READY invariant; no task exists to wake
                    if reason == "compaction-failed":
                        sv2.transition(
                            session,
                            sv2.SessionStateV2.FINALIZING,
                            reason,
                            termination_reason=sv2.TerminationReason.COMPACTION_FAILED,
                        )
                    else:
                        sv2.transition(session, sv2.TRANSITIONS[(target_state, reason)], reason)
                    # COMPACTING lands in FINALIZING; finish the route home.
                    if sv2._current_state(session) is sv2.SessionStateV2.FINALIZING:
                        sv2.transition(session, sv2.SessionStateV2.IDLE_READY, "finalize-error")
                except Exception as _e:
                    logger.error("reconcile_interrupted reset failed for %s: %s", sid, _e)
                    continue
                session.emit_event(
                    {
                        "type": "system",
                        "content": (
                            "Session was interrupted by a server restart and has been reset. "
                            "You can send a new message."
                        ),
                    }
                )
                reset += 1
        return reset

    def _persist_watched(self, session: AgentSession) -> None:
        """Centralized helper: persist the watch-set after every mutation.

        Called from every site that mutates `_watched_worker_ids` so the
        DB stays in sync with memory. Without this, a restart in
        AWAITING_WORKERS loses the list of workers the parent was
        awaiting on, leaving it stuck (or relying on the reaper's empty-
        set safety net which doesn't fire while stale IDs remain)."""
        try:
            db.set_watched_workers(
                session.session_id,
                list(session._watched_worker_ids),
            )
        except Exception as e:
            logger.error("watched_worker_ids persist failed for %s: %s", session.session_id, e)

    def get(self, session_id: str) -> AgentSession | None:
        return self._sessions.get(session_id)

    def create_session(
        self,
        title: str = "New session",
        system_prompt: str = "",
        session_type: str = "normal",
        parent_session_id: str | None = None,
    ) -> str:
        """Create a new session in both DB and memory."""
        sid = db.create_session(
            title=title,
            system_prompt=system_prompt,
            session_type=session_type,
            parent_session_id=parent_session_id,
        )
        session = AgentSession(
            session_id=sid,
            session_type=session_type,
            parent_session_id=parent_session_id,
        )
        self._sessions[sid] = session
        logger.info("Created session %s (type=%s)", sid, session_type)
        from core.snooze import SNOOZE_TRANSPARENT_TYPES, get_snooze

        if session_type not in SNOOZE_TRANSPARENT_TYPES:
            get_snooze().notify_activity()
        return sid

    async def _maybe_enqueue_goal_continuation(self, session: AgentSession) -> None:
        """Auto-continue a live goal after a qualifying turn end (plan 3b).

        Triggers on termination_reason ∈ {complete, round_ceiling,
        budget_exhausted} — the latter two ARE the long-running case (the
        turn ran out of rounds or LLM session budget mid-goal, not out of
        work). Never on cancelled/error/compaction_failed (a human is
        needed), never in AWAITING_USER (waiting on a human is a legitimate
        block — push notifications alert them), never over queued user
        messages (the user's words outrank the machine's).
        """
        from sessions import state_v2 as sv2
        from sessions.state import PendingMessage

        if sv2._current_state(session) is not sv2.SessionStateV2.FINALIZING:
            return
        if session.termination_reason not in ("complete", "round_ceiling", "budget_exhausted"):
            return
        if session.pending_messages:
            return

        goal = await asyncio.to_thread(db.get_active_goal, session.session_id)
        if not goal or goal["status"] != "active":
            return

        # Token/time budget enforcement: exhausted -> budget_limited + loud
        # notification, never a silent continuation.
        if goal.get("token_budget"):
            used = await asyncio.to_thread(db.goal_token_usage, goal["id"])
            if used >= int(goal["token_budget"]):
                await self._limit_goal(session, goal, f"token budget spent ({used:,}/{int(goal['token_budget']):,})")
                return
        if goal.get("time_budget_s") and goal.get("started_at"):
            from datetime import datetime, timezone

            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(goal["started_at"])).total_seconds()
            if elapsed >= int(goal["time_budget_s"]):
                await self._limit_goal(
                    session, goal, f"time budget spent ({int(elapsed)}s/{int(goal['time_budget_s'])}s)"
                )
                return

        budget = int(goal.get("continuation_budget") or 0)
        used_cont = int(goal.get("continuations_used") or 0)
        if used_cont >= budget:
            return  # opt-in exhausted (or never granted) — goal stays live, user drives

        # A budget_exhausted turn's continuation would inherit the exhausted
        # LLM session clock and die immediately — synthetic messages don't
        # get the reset real user messages get. Extend deliberately; the
        # goal's own budgets are the governing limit now.
        if session.termination_reason == "budget_exhausted":
            try:
                from core.llm.client import extend_session_budget

                extend_session_budget(session.session_id, float(settings.llm_session_timeout))
            except Exception as _e:
                logger.warning("Session budget extension failed for goal continuation: %s", _e)

        ordinal = used_cont + 1
        await asyncio.to_thread(db.update_goal, goal["id"], continuations_used=ordinal)
        prompt = (
            f"[goal continuation {ordinal}/{budget}] The goal is still active:\n"
            f"{goal['objective']}\n\n"
            f"Continue working toward it, or report blockage with host-observable "
            f"evidence — do not end the goal yourself. Use goal_complete only when "
            f"the objective is met and its gates pass."
        )
        session.pending_messages.append(PendingMessage(prompt, None, False))
        session.emit_event({"type": "goal.continuation", "goal_id": goal["id"], "ordinal": ordinal, "budget": budget})
        logger.info(
            "Goal #%d: auto-continuation %d/%d enqueued for %s (termination=%s)",
            goal["id"],
            ordinal,
            budget,
            session.session_id[:12],
            session.termination_reason,
        )

    async def _limit_goal(self, session: AgentSession, goal: dict, reason: str) -> None:
        await asyncio.to_thread(db.update_goal, goal["id"], status="budget_limited")
        try:
            nid = await asyncio.to_thread(
                db.add_notification,
                session_id=session.session_id,
                title=f"Goal #{goal['id']} budget-limited",
                body=f"{reason}. The goal is paused at budget_limited — raise its budget "
                f"(goal_update) or complete it to proceed.",
                urgency="high",
            )
            self.broadcast(
                {
                    "type": "dialog.notification",
                    "notification_id": nid,
                    "title": f"Goal #{goal['id']} budget-limited",
                    "body": reason,
                    "urgency": "high",
                    "source_session_id": session.session_id,
                }
            )
        except Exception as _e:
            logger.warning("Goal budget notification failed: %s", _e)
        logger.info("Goal #%d budget-limited for %s: %s", goal["id"], session.session_id[:12], reason)

    def remove(self, session_id: str) -> None:
        """Remove session from memory (does NOT delete from DB)."""
        session = self._sessions.pop(session_id, None)
        if session and session.task and not session.task.done():
            session.task.cancel()
        from core.llm.client import get_llm_client as _get_client

        _get_client().purge_session(session_id)
        # Kernel shutdown rides a daemon thread (plan 2b): the snapshot is
        # seconds of blocking IO and remove() runs on the event loop. The
        # snapshot preserves the namespace for revival if the session comes
        # back. No-op when the session never had a kernel.
        try:
            from core.kernel import get_kernel_registry

            get_kernel_registry().shutdown_session_detached(session_id, snapshot=True)
        except Exception as e:
            logger.warning("Kernel shutdown scheduling failed for %s: %s", session_id[:12], e)

    def delete_session(self, session_id: str) -> None:
        """Delete session from both memory and DB (cascades workers)."""
        # Cancel any running task
        session = self._sessions.get(session_id)
        if session:
            if session.task and not session.task.done():
                session.task.cancel()
            # Clean up workers first
            for wid in list(session.worker_ids):
                self.delete_session(wid)

        # Clean up per-worker summary file if this session was a worker
        from pathlib import Path as _P

        worker_summary = _P(settings.workspace_dir) / f".worker_{session_id[:12]}_summary.md"
        worker_summary.unlink(missing_ok=True)

        self._sessions.pop(session_id, None)
        # Same scheduler cleanup remove() does. Without it the per-provider
        # wall-clock budget maps keep an entry for a session that no longer
        # exists, for the life of the process.
        from core.llm.client import get_llm_client as _get_client

        _get_client().purge_session(session_id)
        self._purge_rlm_artifacts(session_id)
        # Session deletion purges kernel state entirely — no snapshot; there
        # is no session left to revive into (plan 2b).
        try:
            from core.kernel import get_kernel_registry

            get_kernel_registry().shutdown_session_detached(session_id, snapshot=False, purge_state=True)
        except Exception as e:
            logger.warning("Kernel purge scheduling failed for %s: %s", session_id[:12], e)
        db.delete_session(session_id)
        logger.info("Deleted session %s", session_id)

    def _purge_rlm_artifacts(self, session_id: str) -> None:
        """Deleting an RLM view session (or a parent holding some) also removes
        the backing run dir + rows — the session is just the sidebar anchor, and
        without this the run would linger headless until retention.

        Must run BEFORE db.delete_session (it needs the child rows to find the
        runs). Running runs are skipped: the engine holds the dir open and the
        row is still being written; retention reaps them later. Grandchildren
        (a worker's own RLM runs) are handled when the worker itself passes
        through delete_session; non-resident workers deleted via the DB cascade
        leave their dirs to the retention sweep — acceptable residue.
        """
        import shutil
        from pathlib import Path as _P

        try:
            row = db.get_session(session_id)
            candidates = []
            if row and row.get("session_type") == "rlm":
                candidates.append(session_id)
            candidates.extend(
                child["id"] for child in db.get_worker_sessions(session_id) if child.get("session_type") == "rlm"
            )
            for view_sid in candidates:
                run = db.get_rlm_run_by_ui_session(view_sid)
                if not run or run.get("status") == "running":
                    continue
                run_dir = _P(settings.workspace_dir) / run["run_dir"]
                if run_dir.exists():
                    shutil.rmtree(run_dir, ignore_errors=True)
                db.delete_rlm_run(run["run_id"])
        except Exception:
            logger.exception("RLM artifact purge failed for session %s", session_id)

    # ------------------------------------------------------------------
    # Prompt routing
    # ------------------------------------------------------------------

    async def prompt(
        self,
        session_id: str,
        message: str,
        system_prompt: str = "",
        idempotency_key: str | None = None,
    ) -> None:
        """Send a message to a session.

        Events are delivered via the persistent SSE connection.
        If the session is busy, message is queued and 'session.queued' event emitted.

        idempotency_key, when provided, is persisted on the user message row
        so a concurrent re-submission with the same key is caught by the
        chat-router dedup check (api/routers/chat.py).
        """
        # Cancel Snooze if running (user work takes priority). Snooze-
        # transparent sessions (canary sweeps) skip both signals — a 3am
        # sweep must not cancel the very snooze cycle it coexists with.
        from core.snooze import SNOOZE_TRANSPARENT_TYPES, get_snooze

        _pre = self.get(session_id)
        if _pre is None or _pre.session_type not in SNOOZE_TRANSPARENT_TYPES:
            get_snooze().request_cancel()
            get_snooze().notify_activity()

        session = self.get_or_create(session_id)

        async with session.lock:
            # v2 state-aware prompt acceptance. IDLE_READY / AWAITING_USER
            # accept immediately (AWAITING_USER routes the prompt as
            # "answer-received" via _run_agent_safe's start-state detection).
            # CANCELLING rejects outright — the queue is cleared during cancel,
            # re-prompting should be an explicit user act. Other mid-turn
            # states queue, subject to max_pending_messages.
            current_v2 = sv2._current_state(session)
            if current_v2 is sv2.SessionStateV2.CANCELLING:
                session.emit_event(
                    {
                        "type": "session.prompt_rejected",
                        "reason": "cancelling",
                    }
                )
                logger.info("Session %s prompt rejected: cancelling", session_id)
                return
            if current_v2 not in (sv2.SessionStateV2.IDLE_READY, sv2.SessionStateV2.AWAITING_USER):
                # Backpressure: reject if queue is full
                if len(session.pending_messages) >= settings.max_pending_messages:
                    session.emit_event(
                        {
                            "type": "session.queue_full",
                            "pending": len(session.pending_messages),
                            "max": settings.max_pending_messages,
                        }
                    )
                    session.emit_event(
                        {
                            "type": "session.prompt_rejected",
                            "reason": "queue_full",
                        }
                    )
                    logger.warning(
                        "Session %s queue full (%d), rejecting message", session_id, len(session.pending_messages)
                    )
                    return
                # Session is busy. If the most recent user message (running
                # OR queued) landed within the rapid-fire window, fold this
                # message into that DB row instead of opening a new turn.
                # session.last_user_msg_id tracks the running turn's user msg
                # too — so three messages within the window collapse to one
                # turn even when the first one started running before the
                # rest arrived.
                now = time.monotonic()
                if (
                    message
                    and session.last_user_msg_id is not None
                    and (now - session.last_user_msg_at) <= RAPID_FIRE_WINDOW_SECONDS
                ):
                    target_id = session.last_user_msg_id
                    existing = db.get_message(target_id)
                    if existing is not None and self._can_absorb_rapid_fire(session, target_id):
                        combined = _combine_rapid_fire(existing.get("content", "") or "", message)
                        db.update_message_content(target_id, combined)
                        session.last_user_msg_at = now
                        # If the target is a queued entry (not the running one),
                        # update its in-memory tuple too so the agent runs with
                        # the combined content if it ever pops.
                        for i, entry in enumerate(session.pending_messages):
                            entry = PendingMessage.coerce(entry)
                            if entry.msg_id == target_id:
                                session.pending_messages[i] = entry._replace(message=combined, queued_at=now)
                                break
                        session.emit_event(
                            {
                                "type": "session.message_combined",
                                "message_id": target_id,
                                # The running turn's scout planned against the
                                # pre-combine text; only the agent loop re-reads
                                # the row. Surfaced so the UI can say so.
                                "scout_stale": target_id == session.current_turn_user_msg_id,
                            }
                        )
                        logger.debug(
                            "Rapid-fire combined into msg %d for session %s",
                            target_id,
                            session_id,
                        )
                        return
                    # Fall through: the running turn can no longer pick this up.
                    # Queue it as its own turn below rather than writing into a
                    # row nothing will re-read.

                # Persist the message immediately so it's visible in the UI
                # while waiting, then queue for later processing.
                msg_id = None
                if message:
                    msg_id = await asyncio.to_thread(
                        db.add_message,
                        session_id,
                        "user",
                        message,
                        idempotency_key=idempotency_key,
                    )
                    session.last_user_msg_id = msg_id
                    session.last_user_msg_at = now
                session.pending_messages.append(PendingMessage(message, system_prompt, True, now, msg_id))
                session.emit_event(
                    {
                        "type": "session.queued",
                        "queue_depth": len(session.pending_messages),
                        # Lets the UI tag the queued bubble so it can offer
                        # removal via DELETE /pending/{message_id}.
                        "message_id": msg_id,
                    }
                )
                logger.debug("Message queued for busy session %s (depth=%d)", session_id, len(session.pending_messages))
                return

            # Start agent task — reset state for new user turn.
            # post_hooks_complete and waiting_for_input are now derived from
            # the v2 state machine (state.py properties), so no explicit
            # resets are needed here — the SCOUTING transition inside
            # _run_agent_safe will auto-flip them.
            session.touch()
            session.cancel_requested = False
            session.reflect_count = 0
            session.reflect_lessons = ""
            session.reflect_retry_requested = False
            session.retry_excluded_tools = set()
            session.last_tool_summary = {}
            session.eval_count = 0
            session.eval_retry_requested = False
            # Clear stale error / termination_reason from a prior turn —
            # otherwise _finalize_worker's fallback branch can mis-classify
            # a clean turn as errored based on last-turn state.
            session.error = None
            session.termination_reason = None
            # Reset the LLM wall-clock budget for this fresh user turn.
            # Without this, the budget tracks wall-clock time from the
            # session's FIRST EVER acquire and never resets, so a user
            # who chats for >llm_session_timeout seconds (default 1800)
            # gets "LLM time budget exhausted — turn aborted before scout"
            # on every subsequent message even though most of that time
            # was them typing. New user message = fresh budget window.
            try:
                from core.llm.client import reset_session_budget as _reset_budget

                _reset_budget(session_id)
            except Exception as _e:
                logger.debug("Budget reset failed for %s: %s", session_id, _e)
            if self._agent_runner is None:
                session.emit_event(
                    {
                        "type": "stream.error",
                        "error": "Agent runner not initialized",
                    }
                )
                return

            # Window B recovery: before dispatching the new message, check the
            # DB for any user message that was orphaned by a prior restart (i.e.,
            # the server restarted after IDLE_READY was written but before
            # _process_pending ran for the orphan's turn). If found, queue the
            # orphan(s) first so they are processed in chronological order, then
            # queue the incoming message behind them. _process_pending handles
            # the whole sequence after the lock is released.
            #
            # Skip when there's a live agent task: that task is currently
            # processing the "orphan" candidate — it's not actually orphaned,
            # just mid-turn with no assistant message written yet.
            #
            # Skip when AWAITING_USER: this is an ask_user answer resuming the
            # session, not a Window B restart scenario. Running _find_db_orphans
            # here incorrectly detects rapid-fire prior answers as orphans, then
            # routes via _process_pending which immediately returns (state ≠
            # IDLE_READY), leaving the answer message queued but never run.
            import time as _time

            _task_alive = session.task is not None and not session.task.done()
            _orphans = (
                [] if _task_alive or current_v2 is sv2.SessionStateV2.AWAITING_USER else self._find_db_orphans(session)
            )
            # Filter orphans already swept — same guard as _sweep_db_pending.
            _orphans = [_o for _o in _orphans if _o["id"] not in session.swept_orphan_ids]
            if _orphans:
                _now_m = _time.monotonic()
                for _o in _orphans:
                    _oid = _o["id"]
                    session.swept_orphan_ids.add(_oid)
                    if not any(PendingMessage.coerce(_e).msg_id == _oid for _e in session.pending_messages):
                        session.pending_messages.append(PendingMessage(_o.get("content", ""), "", True, _now_m, _oid))
                        logger.warning(
                            "Session %s: Window B recovered orphaned message %d",
                            session_id[:12],
                            _oid,
                        )
                if message:
                    _new_id = await asyncio.to_thread(
                        db.add_message,
                        session.session_id,
                        "user",
                        message,
                        idempotency_key=idempotency_key,
                    )
                    session.last_user_msg_id = _new_id
                    session.last_user_msg_at = _time.monotonic()
                    session.pending_messages.append(
                        PendingMessage(message, system_prompt, True, _time.monotonic(), _new_id)
                    )
                # Dispatch via _process_pending (pops oldest entry — the orphan)
                self._spawn_detached(self._process_pending(session), "process-pending")
                return

            # Pre-save the user message inline so a concurrent re-submission
            # with the same idempotency_key sees the row and the chat router
            # short-circuits to "duplicate". Without inline persistence, the
            # second call's SELECT runs before _run_agent_safe writes the row.
            if message:
                _new_id = await asyncio.to_thread(
                    db.add_message,
                    session.session_id,
                    "user",
                    message,
                    idempotency_key=idempotency_key,
                )
                session.last_user_msg_id = _new_id
                session.last_user_msg_at = _time.monotonic()
                session.current_turn_user_msg_id = _new_id
            session.task = asyncio.create_task(
                self._run_agent_safe(session, message, system_prompt, pre_saved=bool(message))
            )

    def _can_absorb_rapid_fire(self, session: AgentSession, target_id: int) -> bool:
        """Can a follow-up still be folded into message `target_id`?

        Combining rewrites an existing user row in place and returns without
        queueing anything. That is only safe while something will still READ
        that row:

          * a queued entry — read when it eventually pops. Always safe.
          * the running turn's row — read by compile_context at the top of
            each tool round, so safe only while the agent loop has rounds
            left. Once it has emitted its final text answer, nothing re-reads
            the row and the appended text is lost silently: the combined row
            has an assistant message after it, so get_orphaned_user_messages
            never flags it either.

        Returns False for that last case so the caller queues a fresh turn
        instead. Errors resolve to False — an extra turn is recoverable, a
        dropped message is not.
        """
        if target_id != session.current_turn_user_msg_id:
            return True  # a queued entry; it has not been consumed yet
        try:
            if db.turn_has_final_answer(session.session_id, target_id):
                logger.info(
                    "Session %s: rapid-fire follow-up arrived after turn %d answered "
                    "— queueing a new turn instead of combining",
                    session.session_id[:12],
                    target_id,
                )
                session.emit_event(
                    {
                        "type": "session.message_combine_skipped",
                        "message_id": target_id,
                        "reason": "turn_already_answered",
                    }
                )
                return False
        except Exception as e:
            logger.warning(
                "Session %s: rapid-fire absorb check failed (%s) — queueing a new turn",
                session.session_id[:12],
                e,
            )
            return False
        return True

    async def _run_agent_safe(
        self,
        session: AgentSession,
        message: str,
        system_prompt: str,
        pre_saved: bool = False,
    ) -> None:
        """Wrapper: runs scout → agent loop → post-task hooks, routed through
        the v2 state machine. State mutations go through sv2.transition() which
        writes a session_state_log row and emits session.state_changed SSE for
        every transition."""
        _was_cancelled = False
        try:
            # Defense in depth: a turn launched directly by
            # _resume_from_workers (synthesis turn after AWAITING_WORKERS)
            # can race with the suspended turn's finally block that's
            # still settling post-hooks on a separate task. If reflect on
            # the suspended truncated transcript sets reflect_retry_requested
            # AFTER _resume_from_workers' lock-protected reset, the stale
            # True leaks into this fresh turn and causes a spurious retry
            # despite reflect=pass. Reset here at the start of every turn
            # so no flag can survive across turn boundaries.
            session.reflect_retry_requested = False
            session.eval_retry_requested = False
            # Also clear reflect_lessons / reflect_count so a verdict=retry
            # from the prior turn that the gate refused (because a queued user
            # message was waiting) cannot leak its "previous attempt failed"
            # lessons into the queued-popped turn's scout. submit_message
            # already clears these for the immediate-acceptance path; queued-
            # popped turns enter via _process_pending → _run_agent_safe and
            # bypass submit_message, so without this reset the next turn's
            # scout sees stale [REFLECT — Retry] context appended to a fresh
            # user message.
            session.reflect_lessons = ""
            session.reflect_count = 0
            session.retry_excluded_tools = set()

            # --- Scout phase ---
            # Distinguish an answer-resumed turn from a fresh user prompt.
            # Both land here with legacy state==IDLE, but v2 encodes the
            # difference explicitly (AWAITING_USER vs IDLE_READY) so the
            # state log records the true cause and can chain turns via
            # parent_turn_id.
            start_state = sv2._current_state(session)
            start_reason = (
                "answer-received"
                if start_state is sv2.SessionStateV2.AWAITING_USER
                else "workers-complete" if start_state is sv2.SessionStateV2.AWAITING_WORKERS else "prompt-arrived"
            )

            # Persist the user message immediately so it's visible in the DB
            # during the scout phase — navigating away before scouting finishes
            # would otherwise show an empty chat on return.
            # Only for fresh IDLE_READY turns not already persisted by the caller.
            _pre_saved = pre_saved
            if start_state is sv2.SessionStateV2.IDLE_READY and message and not pre_saved:
                _new_id = await asyncio.to_thread(db.add_message, session.session_id, "user", message)
                # Track for the rapid-fire combiner — if more messages arrive
                # within RAPID_FIRE_WINDOW_SECONDS of this turn starting, they
                # fold into this row rather than queue as a new turn.
                session.last_user_msg_id = _new_id
                session.last_user_msg_at = time.monotonic()
                # Lock in this turn's user msg id NOW, before any later prompt
                # can overwrite session.last_user_msg_id. compile_context and
                # reflect read this to scope to this turn only.
                session.current_turn_user_msg_id = _new_id
                _pre_saved = True

            sv2.transition(session, sv2.SessionStateV2.SCOUTING, start_reason)

            await self._run_scout_and_process(
                session,
                message,
                pre_saved=_pre_saved,
                # Answer-resumed turns continue the same task — reuse the
                # suspended turn's scout report instead of re-scouting.
                reuse_scout=start_reason == "answer-received",
            )

        except asyncio.CancelledError:
            _was_cancelled = True
            session.termination_reason = "cancelled"
            current = sv2._current_state(session)
            if current != sv2.SessionStateV2.CANCELLING:
                # Use "cancel-during-pause" for the two paused states — that's
                # the reason name the graph defines for those edges. All other
                # states use the standard "cancel-requested".
                _cancel_reason = (
                    "cancel-during-pause"
                    if current in (sv2.SessionStateV2.PAUSED, sv2.SessionStateV2.PAUSE_REQUESTED)
                    else "cancel-requested"
                )
                try:
                    sv2.transition(
                        session,
                        sv2.SessionStateV2.CANCELLING,
                        _cancel_reason,
                        termination_reason=sv2.TerminationReason.CANCELLED,
                    )
                except Exception as _e:
                    logger.error("Cancel transition failed for %s: %s", session.session_id, _e)
            # Cascade cancel to any workers this session is watching. They
            # have no other reason to stop, and leaving them running would
            # both waste tokens and orphan their entries in any other
            # parent's watch-set.
            watched = list(getattr(session, "_watched_worker_ids", set()))
            if watched:
                logger.info(
                    "Parent %s cancelled — cascading to %d watched worker(s)",
                    session.session_id,
                    len(watched),
                )
                for wid in watched:
                    w = self._sessions.get(wid)
                    if w and w.task and not w.task.done():
                        w.task.cancel()
                session._watched_worker_ids.clear()
                self._persist_watched(session)
            logger.info("Session %s agent task cancelled", session.session_id)
        except Exception as e:
            logger.error("Agent error in session %s: %s", session.session_id, e, exc_info=True)
            session.error = str(e)
            current = sv2._current_state(session)
            if current == sv2.SessionStateV2.SCOUTING:
                session.termination_reason = "scout_error"
                try:
                    sv2.transition(
                        session,
                        sv2.SessionStateV2.FINALIZING,
                        "scout-error",
                        termination_reason=sv2.TerminationReason.SCOUT_ERROR,
                    )
                except Exception as _e:
                    logger.error("Scout-error transition failed: %s", _e)
            else:
                session.termination_reason = "error"
                try:
                    sv2.transition(
                        session,
                        sv2.SessionStateV2.FINALIZING,
                        "agent-error",
                        termination_reason=sv2.TerminationReason.ERROR,
                    )
                except Exception as _e:
                    logger.error("Agent-error transition failed: %s", _e)
            session.emit_event({"type": "stream.error", "error": str(e)})

            # Budget-exhaustion is a special class of error: the user can
            # recover by sending a new message (which resets the wall-clock
            # budget). Notify them so they know which session needs a nudge,
            # rather than leaving them to wonder why their conversation went
            # quiet. Skip for worker sessions — the orchestrator handles
            # those internally; firing on every worker would spam the user.
            try:
                from core.llm.semaphore import LLMSessionTimeoutError

                is_budget_exhausted = isinstance(e, LLMSessionTimeoutError)
            except Exception:
                is_budget_exhausted = False
            if is_budget_exhausted and session.session_type != "worker":
                _broadcast_session_timeout_notification(session)
        finally:
            await self._finalize_turn(session, message, system_prompt, _was_cancelled)

    async def _finalize_turn(
        self,
        session: AgentSession,
        message: str,
        system_prompt: str,
        was_cancelled: bool,
    ) -> None:
        """Terminal phase of every agent turn — extracted verbatim from
        _run_agent_safe's finally block. Routes the session from its
        post-loop state through CANCELLING/FINALIZING to IDLE_READY, runs
        post-hooks plus the reflect/eval retry loop, restores model
        overrides, stamps worker summaries, and drains the pending queue.
        Always invoked from the finally of _run_agent_safe."""
        session.touch()

        # If cancelled, close out via CANCELLING → IDLE_READY, skip post-hooks.
        if was_cancelled or session.cancel_requested:
            current = sv2._current_state(session)
            cancel_reason = "cancel-complete"
            # Clear the cancel flag before every IDLE_READY transition —
            # the invariant check fires if cancel_requested is True on entry.
            session.cancel_requested = False
            if current == sv2.SessionStateV2.CANCELLING:
                try:
                    sv2.transition(
                        session,
                        sv2.SessionStateV2.IDLE_READY,
                        "cancel-complete",
                    )
                except Exception as _e:
                    logger.error("Cancel-complete transition failed: %s", _e)
            elif current != sv2.SessionStateV2.IDLE_READY:
                # Race: cancel flag set but we never reached CANCELLING.
                # Use an explicit cancel-timeout edge to IDLE_READY.
                cancel_reason = "cancel-timeout"
                try:
                    sv2.transition(
                        session,
                        sv2.SessionStateV2.IDLE_READY,
                        "cancel-timeout",
                    )
                except Exception as _e:
                    logger.error("Cancel-timeout fallback failed: %s", _e)
            # Record a transcript-visible marker so readers see the turn
            # ended in cancellation (not silently dropped). The "notice"
            # role is filtered from LLM context by core/context/compiler.py
            # so it doesn't leak into the next turn's prompt.
            try:
                db.add_message(
                    session.session_id,
                    "notice",
                    f"[turn cancelled — {cancel_reason}]",
                )
            except Exception as _e:
                logger.debug("Cancel notice insert skipped: %s", _e)
            # The /cancel API endpoint already drains pending_messages and
            # writes a "[N queued message(s) dropped]" notice when the
            # queue had entries. We don't redo that here — by the time the
            # agent task's CancelledError reaches this finally, the queue
            # has already been cleared.
            # Clear the turn-scoped user-msg id on cancel.
            session.current_turn_user_msg_id = None
            session.emit_event({"type": "turn.complete"})
            # Even on cancel, if this is a worker, the parent may be
            # watching us via _watched_worker_ids. Without firing the
            # callback here, a parent waiting on a single cancelled
            # worker deadlocks — _on_watched_worker_done is the only
            # path that empties the watch-set and resumes the parent.
            if session.session_type == "worker" and session.parent_session_id:
                self.emit(
                    session.parent_session_id,
                    {
                        "type": "worker.done",
                        "worker_id": session.session_id,
                        "termination_reason": session.termination_reason,
                        "error": session.error,
                    },
                )
                try:
                    await self._on_watched_worker_done(session)
                except Exception as _e:
                    logger.error("Cancel-path watcher notify failed: %s", _e)
            return

        # Normal path: if the agent loop returned cleanly we're still in
        # PROCESSING. Transition to FINALIZING with a termination reason
        # derived from what agent.py left in session.termination_reason.
        # If we ended inside COMPACTING (unexpected — compact paths always
        # transition back), route via compaction-failed so reflect
        # classifies honestly.
        current = sv2._current_state(session)
        if current == sv2.SessionStateV2.PROCESSING:
            v2_reason, v2_term = _map_termination_to_v2_reason(session.termination_reason)
            try:
                sv2.transition(
                    session,
                    sv2.SessionStateV2.FINALIZING,
                    v2_reason,
                    termination_reason=v2_term,
                )
            except Exception as _e:
                logger.error("Loop-complete transition failed: %s", _e)
        elif current == sv2.SessionStateV2.COMPACTING:
            try:
                sv2.transition(
                    session,
                    sv2.SessionStateV2.FINALIZING,
                    "compaction-failed",
                    termination_reason=sv2.TerminationReason.COMPACTION_FAILED,
                )
            except Exception as _e:
                logger.error("COMPACTING→FINALIZING fallback failed: %s", _e)

        # Post-hooks + Reflect retry loop (same control flow as before)
        reflect_retry_cap = (
            settings.reflect_max_retries_worker if session.session_type == "worker" else settings.reflect_max_retries
        )
        try:
            while True:
                await self._run_post_hooks(session)

                # A user message queued mid-reflect pre-empts any retry (user
                # work takes priority) and the next turn deliberately clears
                # reflect_lessons. Don't let the failed-verification signal
                # vanish silently: drop the retry here with a transcript-
                # visible notice ("notice" rows are filtered from LLM context
                # by the compiler, so nothing leaks into the next turn).
                if (session.reflect_retry_requested or session.eval_retry_requested) and session.pending_messages:
                    dropped_kind = "reflect" if session.reflect_retry_requested else "eval"
                    session.reflect_retry_requested = False
                    session.eval_retry_requested = False
                    try:
                        db.add_message(
                            session.session_id,
                            "notice",
                            f"[{dropped_kind} verdict was 'retry', but a queued message pre-empted the retry "
                            "— this turn's outcome is unverified; see the reflect entry above for details]",
                        )
                    except Exception as _e:
                        logger.debug("Dropped-retry notice insert skipped: %s", _e)
                    logger.info(
                        "%s retry for session %s dropped — queued message takes priority",
                        dropped_kind,
                        session.session_id,
                    )

                in_finalizing = sv2._current_state(session) == sv2.SessionStateV2.FINALIZING
                if (
                    session.reflect_retry_requested
                    and session.reflect_count < reflect_retry_cap
                    and not session.pending_messages
                    and in_finalizing
                ):
                    session.reflect_retry_requested = False
                    logger.info("Reflect retry #%d for session %s", session.reflect_count, session.session_id)
                    await self._run_agent_retry(session, message, system_prompt, retry_kind="reflect-retry")
                    continue

                # Eval retry (only if reflect didn't retry)
                in_finalizing = sv2._current_state(session) == sv2.SessionStateV2.FINALIZING
                if (
                    session.eval_retry_requested
                    and session.eval_count < settings.eval_max_retries
                    and not session.pending_messages
                    and in_finalizing
                ):
                    session.eval_retry_requested = False
                    logger.info("Eval retry #%d for session %s", session.eval_count, session.session_id)
                    await self._run_agent_retry(session, message, system_prompt, retry_kind="eval-retry")
                    continue
                break
        except asyncio.CancelledError:
            # A cancel arrived during a reflect/eval retry (_run_agent_retry
            # already transitioned the session to CANCELLING and re-raised).
            # Complete the CANCELLING → IDLE_READY arc here so the session
            # doesn't linger in CANCELLING waiting for the 30s reaper timeout.
            _cur = sv2._current_state(session)
            if _cur == sv2.SessionStateV2.CANCELLING:
                try:
                    session.cancel_requested = False  # satisfy IDLE_READY invariant
                    sv2.transition(session, sv2.SessionStateV2.IDLE_READY, "cancel-complete")
                except Exception as _e:
                    logger.error("Cancel-complete (retry cancel) failed for %s: %s", session.session_id, _e)
            elif _cur != sv2.SessionStateV2.IDLE_READY:
                try:
                    session.cancel_requested = False  # satisfy IDLE_READY invariant
                    sv2.transition(session, sv2.SessionStateV2.IDLE_READY, "cancel-timeout")
                except Exception as _e:
                    logger.error("Cancel-timeout (retry cancel) failed for %s: %s", session.session_id, _e)
            session.emit_event({"type": "turn.complete"})
            # Mirror the primary cancel branch: notify any watching parent
            # so it doesn't deadlock waiting on this cancelled worker.
            if session.session_type == "worker" and session.parent_session_id:
                self.emit(
                    session.parent_session_id,
                    {
                        "type": "worker.done",
                        "worker_id": session.session_id,
                        "termination_reason": session.termination_reason,
                        "error": session.error,
                    },
                )
                try:
                    await self._on_watched_worker_done(session)
                except Exception as _e:
                    logger.error("Retry-cancel watcher notify failed: %s", _e)
            return

        # Restore per-session model override AFTER all retries complete.
        if session._model_before_agent_switch is not None:
            before = session._model_before_agent_switch
            session._model_before_agent_switch = None
            # Capture the model that was active during this turn BEFORE restoring
            active_during_turn = session.model_override or settings.llm_model
            session.model_override = before if before != "" else None
            before_budget = session._budget_before_agent_switch
            session._budget_before_agent_switch = None
            session.context_budget_override = before_budget if before_budget not in (None, -1) else None
            restored_to = session.model_override or settings.llm_model
            logger.info("Restored per-session model override after agent turn and retries (was: %s)", before or "none")
            session.emit_event(
                {
                    "type": "model.override",
                    "from": active_during_turn,
                    "to": session.model_override,
                    "active": session.model_override is not None,
                }
            )
            # Persist a model_divider so the switch-back is visible after reload.
            # Only write it if the model actually changed (skip no-op restores).
            if active_during_turn != restored_to:
                import json as _json

                try:
                    db.add_message(
                        session.session_id,
                        "model_divider",
                        "",
                        metadata=_json.dumps(
                            {
                                "from": active_during_turn,
                                "to": restored_to,
                                "active": False,
                                "baseline": settings.llm_model,
                            }
                        ),
                    )
                except Exception as _e:
                    logger.debug("model_divider restore persist failed: %s", _e)

        # Stamp the worker's terminal summary file AFTER all reflect retries.
        # Skip when the worker is paused on ask_user — stamping now would
        # write the "I've asked you a question" placeholder as the
        # terminal summary, and the second pass (after the user answers)
        # short-circuits because the file already exists. The real final
        # answer would never make it into the summary file.
        if session.session_type == "worker" and sv2._current_state(session) is not sv2.SessionStateV2.AWAITING_USER:
            try:
                await self._finalize_worker(session)
            except Exception as _fin_err:
                logger.error("Worker finalize failed for %s: %s", session.session_id, _fin_err)

        # Goal continuation (plan 3b): enqueued ONLY here — after the reflect
        # retry loop broke, while still FINALIZING, before pending dispatch.
        # An earlier enqueue would suppress reflect (_run_post_hooks early-
        # returns on pending) or cancel a requested retry.
        if settings.goals_enabled and session.session_type == "normal":
            try:
                await self._maybe_enqueue_goal_continuation(session)
            except Exception as _e:
                logger.warning("Goal continuation check failed for %s: %s", session.session_id[:12], _e)

        # FINALIZING → IDLE_READY — turn fully done.
        if sv2._current_state(session) == sv2.SessionStateV2.FINALIZING:
            try:
                sv2.transition(
                    session,
                    sv2.SessionStateV2.IDLE_READY,
                    "turn-complete",
                )
            except Exception as _e:
                logger.error("Turn-complete transition failed: %s", _e)

        # Clear the turn-scoped user-msg id; the next turn will set its own.
        # Capture it first — the sweep uses it to exclude the just-completed
        # turn's user message from the orphan list (agents that don't write
        # assistant messages, e.g. stubs, would otherwise look like orphans).
        _completed_turn_msg_id = session.current_turn_user_msg_id
        session.current_turn_user_msg_id = None
        session.emit_event({"type": "turn.complete"})

        # Window A recovery: if pending_messages is empty, check the DB for
        # any user message that arrived during post-hooks but was lost from
        # the in-memory queue (e.g., server restarted between the
        # FINALIZING→IDLE_READY write and _process_pending running).
        if not session.pending_messages:
            await self._sweep_db_pending(session, exclude_msg_id=_completed_turn_msg_id)

        # Worker-specific: notify the parent session that this worker
        # turn has fully settled. Frontend listens for `worker.done`
        # to close progress indicators on the parent's worker panel.
        # Skip for AWAITING_USER — the worker is paused on ask_user, not
        # done. Without this guard, the parent's _on_watched_worker_done
        # auto-resumes it prematurely, then get_worker_result returns the
        # worker's "I've asked you a question" suspension placeholder
        # instead of the real final answer once the user replies.
        cur_v2 = sv2._current_state(session)
        if (
            session.session_type == "worker"
            and session.parent_session_id
            and cur_v2 is not sv2.SessionStateV2.AWAITING_USER
        ):
            self.emit(
                session.parent_session_id,
                {
                    "type": "worker.done",
                    "worker_id": session.session_id,
                    "termination_reason": session.termination_reason,
                    "error": session.error,
                },
            )
            # Gap 1+2: wake parent if it's watching this worker.
            await self._on_watched_worker_done(session)

        # Process pending messages.
        await self._process_pending(session)

    # Cap on the last-assistant-message slice we persist in an auto-stamp.
    # Set to 2–3× `get_worker_result`'s 3000-char read cap so a future reader-side
    # bump doesn't require re-stamping. Values above this are truncated with a
    # trailing marker to keep the file from ballooning for verbose workers.
    _STAMP_MAX_CHARS = 8000

    async def _finalize_worker(self, session: AgentSession) -> None:
        """Ensure a worker produces a stamped summary file on completion.

        If the worker wrote its own `.worker_{id[:12]}_summary.md`, leave it alone.
        Otherwise write a sentinel-prefixed file containing the last assistant
        message so `get_worker_result` has something reliable to return.
        """
        from pathlib import Path as _P

        workspace = _P(settings.workspace_dir)
        summary_path = workspace / f".worker_{session.session_id[:12]}_summary.md"
        if summary_path.exists():
            return

        # Grab the last assistant message as the best-available content.
        last_text = ""
        try:
            messages = db.get_messages(session.session_id, last=100)
            for m in reversed(messages):
                if m["role"] == "assistant" and m.get("content"):
                    last_text = m["content"]
                    break
        except Exception as e:
            logger.debug("Worker finalize: could not read messages for %s: %s", session.session_id, e)

        # Classify from termination_reason, which survives the post-turn
        # force-reset of session.state to IDLE. Falling back on session.state
        # here was dead code — the finally block sets state=IDLE before we
        # reach this point.
        reason = session.termination_reason
        # Pull the most recent reflect verdict (if any) to embed in the header.
        # Reflect is the quality gate: when verdict != 'pass' (or reflect never
        # ran) the stamped output should clearly say so, not just "auto-stamped"
        # which reads as success.
        reflect_verdict: str | None = None
        reflect_reason: str = ""
        try:
            msgs = db.get_messages(session.session_id, last=100)
            for _m in reversed(msgs):
                if _m.get("role") == "reflect":
                    import json as _json

                    try:
                        _r = _json.loads(_m.get("content") or "{}")
                        reflect_verdict = _r.get("verdict")
                        reflect_reason = _r.get("reasoning", "")
                    except (ValueError, TypeError):
                        pass
                    break
        except Exception as _e:
            logger.debug("Worker finalize: reflect lookup failed: %s", _e)

        if reason in ("round_ceiling", "compaction_failed"):
            header = "# INCOMPLETE (worker hit round ceiling / compaction failed)\n"
        elif reason == "cancelled":
            header = "# CANCELLED (worker was cancelled before completion)\n"
        elif reason == "error" or (reason is None and session.error):
            err = session.error or "unknown"
            header = f"# ERROR (worker exited with error: {err})\n"
        elif reflect_verdict == "escalate":
            header = (
                f"# ESCALATED (reflect verdict: escalate)\n"
                f"# Reason: {reflect_reason or '(no reasoning provided)'}\n"
                f"# Output below is the last assistant message — may not be the "
                f"actual deliverable. Use get_worker_transcript for the full stream.\n"
            )
        elif reflect_verdict == "retry":
            header = (
                f"# UNVERIFIED (reflect verdict: retry, retries exhausted)\n"
                f"# Reason: {reflect_reason or '(no reasoning provided)'}\n"
            )
        elif reflect_verdict == "pass":
            header = "# AUTO-STAMPED (reflect=pass; worker did not write an explicit summary)\n"
        else:
            # No reflect verdict recorded (reflect disabled or hook never ran).
            header = "# UNVERIFIED (no reflect verdict recorded — quality not gated)\n"

        truncated = len(last_text) > self._STAMP_MAX_CHARS
        body = last_text[: self._STAMP_MAX_CHARS] if last_text else "(no assistant output)"
        if truncated:
            body += "\n[truncated]"

        def _write() -> None:
            workspace.mkdir(parents=True, exist_ok=True)
            summary_path.write_text(header + body)

        try:
            await asyncio.to_thread(_write)
            logger.info(
                "Auto-stamped worker summary for %s (reason=%s, truncated=%s)",
                session.session_id,
                reason,
                truncated,
            )
        except Exception as e:
            logger.error("Failed to auto-stamp worker summary for %s: %s", session.session_id, e)

    async def _run_scout_and_process(
        self,
        session: AgentSession,
        message: str,
        *,
        pre_saved: bool = False,
        is_retry: bool = False,
        reuse_scout: bool = False,
    ) -> None:
        """Shared scout → PROCESSING → agent runner pipeline.

        Caller must have already transitioned the session to SCOUTING. This
        coroutine handles everything from emitting scout.start through calling
        _agent_runner. Exceptions propagate to the caller's except/finally
        block unchanged.

        reuse_scout: skip the scout run and reuse session.last_scout_report
        (answer-resumed turns — same task, same tool surface; a full re-scout
        only adds latency to the most interactive path). Falls back to a
        normal scout when no prior report exists (e.g. server restarted while
        the question was pending).
        """
        session.emit_event({"type": "scout.start"})
        _check_session_budget_or_raise(session.session_id)

        from core.scout.runner import build_session_brief, run_scout

        reused_prior = False
        if reuse_scout and session.last_scout_report is not None:
            scout_report = session.last_scout_report
            reused_prior = True
        else:
            effective_budget = session.context_budget_override or settings.context_budget
            # Off-loop: the brief loads and tokenizes the full message history.
            brief = await asyncio.to_thread(build_session_brief, session.session_id, context_budget=effective_budget)

            scout_message = message
            if session.reflect_lessons:
                scout_message = f"{message}\n\n{session.reflect_lessons}"

            scout_report = await run_scout(
                session.session_id,
                scout_message,
                brief,
                emit=session.emit_event,
            )
            session.last_scout_report = scout_report

        import json

        scout_event = {
            "type": "scout.done",
            "reused_prior": reused_prior,
            "tools": scout_report.recommended_tools,
            "tool_rationale": scout_report.tool_rationale,
            "approach": scout_report.approach_guidance,
            "memory": scout_report.memory_context[:500] if scout_report.memory_context else "",
            "model": scout_report.recommended_model,
            "model_rationale": scout_report.model_rationale,
            "identity": scout_report.identity[:200] if scout_report.identity else "",
            "skills": scout_report.recommended_skills,
            "skill_rationale": scout_report.skill_rationale,
            "injected_skill_name": scout_report.injected_skill_name,
            "from_cache": scout_report.from_cache,
            "from_fallback": scout_report.from_fallback,
            "latency_ms": scout_report.scout_latency_ms,
            "scout_model": scout_report.scout_model,
        }
        session.emit_event(scout_event)
        db.add_message(session.session_id, "scout", json.dumps(scout_event))

        sv2.transition(session, sv2.SessionStateV2.PROCESSING, "scout-done")

        await self._agent_runner(
            session_id=session.session_id,
            message=message,
            session=session,
            pre_saved=pre_saved,
            is_retry=is_retry,
        )

    async def _run_agent_retry(
        self,
        session: AgentSession,
        message: str,
        system_prompt: str,
        *,
        retry_kind: str = "reflect-retry",
    ) -> None:
        """Run scout → agent for a Reflect or Eval retry. Entered while the
        caller's session is in FINALIZING; exits back in FINALIZING so the
        caller's post-hooks loop can re-run. retry_kind ∈ {reflect-retry, eval-retry}."""
        session.error = None  # Clear previous error for fresh retry
        try:
            # FINALIZING → SCOUTING (new retry_index for the state log)
            sv2.transition(session, sv2.SessionStateV2.SCOUTING, retry_kind)

            await self._run_scout_and_process(session, message, is_retry=True)

        except asyncio.CancelledError:
            session.termination_reason = "cancelled"
            logger.info("Reflect retry cancelled for session %s", session.session_id)
            # Route through CANCELLING — outer _run_agent_safe cancel-finally
            # will terminate cleanly. COMPACTING is included because the retry's
            # agent runner can enter that sub-state during context compaction.
            # PAUSED/PAUSE_REQUESTED use "cancel-during-pause" (the graph's
            # reason name for those from-states).
            current = sv2._current_state(session)
            if current in (
                sv2.SessionStateV2.SCOUTING,
                sv2.SessionStateV2.PROCESSING,
                sv2.SessionStateV2.COMPACTING,
            ):
                try:
                    sv2.transition(
                        session,
                        sv2.SessionStateV2.CANCELLING,
                        "cancel-requested",
                        termination_reason=sv2.TerminationReason.CANCELLED,
                    )
                except Exception as _e:
                    logger.error("Retry cancel transition failed: %s", _e)
            elif current in (sv2.SessionStateV2.PAUSED, sv2.SessionStateV2.PAUSE_REQUESTED):
                try:
                    sv2.transition(
                        session,
                        sv2.SessionStateV2.CANCELLING,
                        "cancel-during-pause",
                        termination_reason=sv2.TerminationReason.CANCELLED,
                    )
                except Exception as _e:
                    logger.error("Retry cancel (pause) transition failed: %s", _e)
            raise  # outer finally handles the terminal IDLE_READY transition
        except Exception as e:
            logger.error("Reflect retry error in session %s: %s", session.session_id, e)
            session.error = str(e)
            current = sv2._current_state(session)
            try:
                if current == sv2.SessionStateV2.SCOUTING:
                    session.termination_reason = "scout_error"
                    sv2.transition(
                        session,
                        sv2.SessionStateV2.FINALIZING,
                        "scout-error",
                        termination_reason=sv2.TerminationReason.SCOUT_ERROR,
                    )
                else:
                    session.termination_reason = "error"
                    sv2.transition(
                        session,
                        sv2.SessionStateV2.FINALIZING,
                        "agent-error",
                        termination_reason=sv2.TerminationReason.ERROR,
                    )
            except Exception as _e:
                logger.error("Retry error transition failed: %s", _e)
            session.emit_event({"type": "stream.error", "error": str(e)})
        finally:
            # Caller expects us to end in FINALIZING so post-hooks can re-run.
            current = sv2._current_state(session)
            if current == sv2.SessionStateV2.PROCESSING:
                v2_reason, v2_term = _map_termination_to_v2_reason(session.termination_reason)
                try:
                    sv2.transition(
                        session,
                        sv2.SessionStateV2.FINALIZING,
                        v2_reason,
                        termination_reason=v2_term,
                    )
                except Exception as _e:
                    logger.error("Retry→FINALIZING transition failed: %s", _e)

    async def _on_watched_worker_done(self, worker_session: AgentSession) -> None:
        """Called when a worker completes its turn. If the parent is watching
        this worker, removes it from the watch-set and resumes the parent when
        the set empties."""
        parent_id = worker_session.parent_session_id
        if not parent_id:
            return
        parent = self.get(parent_id)
        if parent is None:
            return
        watched: set = getattr(parent, "_watched_worker_ids", set())
        if worker_session.session_id not in watched:
            return
        watched.discard(worker_session.session_id)
        self._persist_watched(parent)
        if watched:
            # Purge any stale IDs for workers that already completed but were
            # added to the watch-set after they fired (race condition). Without
            # this, a non-empty set of stale IDs permanently blocks resume.
            from sessions import state_v2 as sv2

            stale = set()
            for wid in list(watched):
                w = self.get(wid)
                if w is None:
                    stale.add(wid)  # reaped = done
                    continue
                w_v2 = sv2._current_state(w)
                has_started = w.task is not None or getattr(w, "_turn_id", 0) > 0
                if w_v2 is sv2.SessionStateV2.IDLE_READY:
                    # Started-then-settled OR a never-started worker that
                    # was marked errored by spawn-time cleanup. Either way
                    # it will never fire its own callback, so purge it.
                    if has_started or w.error or w.termination_reason:
                        stale.add(wid)
            if stale:
                watched -= stale
                self._persist_watched(parent)
            if watched:
                return  # other workers still outstanding
        # Hand off to resume — extends LLM budget and starts/queues the
        # synthesis turn.
        await self._resume_from_workers(parent)

    def _build_resume_message(self, parent: AgentSession) -> str:
        """Compose the synthetic message a parent receives on resume.

        Includes a per-worker terminal status (reflect verdict, error,
        cancellation) so the LLM is forced to acknowledge non-pass outcomes
        instead of having to discover them by reading get_worker_result()."""
        lines = [
            f"[Watched workers have completed — {len(parent.worker_ids)} total]",
        ]
        problem_workers: list[str] = []
        for wid in parent.worker_ids:
            w = self.get(wid)
            if w is None:
                # Full id — agent will copy this verbatim into get_worker_result.
                lines.append(f"  - {wid}: (no longer in memory)")
                continue
            verdict: str | None = None
            try:
                import json as _json

                for m in reversed(db.get_messages(wid, last=100)):
                    if m.get("role") == "reflect":
                        try:
                            verdict = _json.loads(m.get("content") or "{}").get("verdict")
                        except (ValueError, TypeError):
                            pass
                        break
            except Exception:
                pass
            tr = w.termination_reason or "unknown"
            # Use the full worker id in the listing — earlier versions truncated
            # to wid[:8] for readability, but the agent then passed that short
            # form to get_worker_result and matched no row, returning a bogus
            # "no output" for workers that had real transcripts.
            if w.error:
                lines.append(f"  - {wid}: ERROR — {w.error[:80]}")
                problem_workers.append(wid)
            elif tr == "cancelled":
                lines.append(f"  - {wid}: CANCELLED")
                problem_workers.append(wid)
            elif verdict == "escalate":
                lines.append(f"  - {wid}: ESCALATED — needs review")
                problem_workers.append(wid)
            elif verdict == "retry":
                lines.append(f"  - {wid}: UNVERIFIED (retries exhausted)")
                problem_workers.append(wid)
            elif verdict == "pass":
                lines.append(f"  - {wid}: pass")
            elif tr == "complete":
                lines.append(f"  - {wid}: complete (no reflect verdict)")
            else:
                lines.append(f"  - {wid}: {tr}")
        lines.append(
            "Call get_worker_result(worker_id) for each worker (use the full id "
            "exactly as listed above) to read its full output."
        )
        if problem_workers:
            lines.append(f"⚠ Inspect transcripts before proceeding for: {', '.join(problem_workers)}.")
        lines.append(
            "\n⚠ TASK PRIORITY: This message resumes your active task. You MUST call "
            "get_worker_result() for every worker listed above before addressing any "
            "other messages in context. Complete the original task first — any injected "
            "messages are secondary and should be handled only after the primary "
            "deliverable is done."
        )
        return "\n".join(lines)

    async def _resume_from_workers(self, parent: AgentSession) -> None:
        """Re-enter the parent session after all watched workers have completed.
        Handles two cases: parent suspended in AWAITING_WORKERS (Gap 2), or
        parent already returned to IDLE_READY and the result arrives async (Gap 1).

        If the parent has cancel_requested set, we skip the resume entirely.
        The cancel cascade fires worker cancels which then trigger this callback
        via _on_watched_worker_done — without this guard, the parent would
        re-enter SCOUTING and start a NEW turn (commonly spawning fresh workers
        to redo the cancelled work), defeating the user's cancel intent."""
        if parent.cancel_requested:
            logger.info(
                "Resume-from-workers skipped for %s — cancel_requested is set",
                parent.session_id,
            )
            # Honor the cancel: drive the parent back to IDLE_READY if it's
            # still suspended in AWAITING_WORKERS (no other path will).
            current_v2 = sv2._current_state(parent)
            if current_v2 is sv2.SessionStateV2.AWAITING_WORKERS:
                async with parent.lock:
                    try:
                        sv2.transition(
                            parent,
                            sv2.SessionStateV2.CANCELLING,
                            "cancel-requested",
                            termination_reason=sv2.TerminationReason.CANCELLED,
                        )
                        sv2.transition(
                            parent,
                            sv2.SessionStateV2.IDLE_READY,
                            "cancel-complete",
                        )
                    except Exception as _e:
                        logger.error(
                            "Cancel-after-workers transition failed for %s: %s",
                            parent.session_id,
                            _e,
                        )
                    parent.emit_event({"type": "turn.complete"})
                    try:
                        db.add_message(
                            parent.session_id,
                            "notice",
                            "[turn cancelled — workers cascade-cancelled by user]",
                        )
                    except Exception as _e:
                        logger.debug("Cascade-cancel notice insert skipped: %s", _e)
            return

        # Workers may have run for a long time, draining the parent's LLM
        # budget. Extend it before the synthesis turn so scout doesn't
        # immediately raise LLMSessionTimeoutError. Mirrors the spawn-time
        # extension in core/extensions/orchestration/spawn_worker.
        try:
            from core.llm.client import extend_session_budget as _extend

            base = float(settings.llm_session_timeout) if settings.llm_session_timeout > 0 else 0.0
            if base > 0:
                _extend(parent.session_id, base * 2)
        except Exception as _ext_err:
            logger.debug("Resume-from-workers budget extend failed: %s", _ext_err)

        async with parent.lock:
            current_v2 = sv2._current_state(parent)
            resume_msg = self._build_resume_message(parent)
            if current_v2 is sv2.SessionStateV2.AWAITING_WORKERS:
                # Parent is suspended waiting — start a new agent turn directly.
                # _run_agent_safe detects AWAITING_WORKERS and uses reason="workers-complete".
                parent.cancel_requested = False
                parent.reflect_count = 0
                parent.reflect_lessons = ""
                parent.reflect_retry_requested = False
                parent.eval_count = 0
                parent.eval_retry_requested = False
                parent.error = None
                parent.termination_reason = None
                parent.task = asyncio.create_task(self._run_agent_safe(parent, resume_msg, None))
            elif current_v2 is sv2.SessionStateV2.IDLE_READY:
                # Parent already returned to idle; push as a pending message so
                # it's processed on the next available slot (Gap 1 auto-resume).
                parent.pending_messages.append(PendingMessage(resume_msg, None, False))
                # _process_pending acquires lock itself, so release first.

        # Outside the lock: drain pending for the IDLE_READY case.
        current_v2 = sv2._current_state(parent)
        if current_v2 is sv2.SessionStateV2.IDLE_READY:
            await self._process_pending(parent)

    async def _process_pending(self, session: AgentSession) -> None:
        """Process queued messages after agent completes."""
        async with session.lock:
            if not session.pending_messages:
                return
            # Use v2 state: legacy mirrors AWAITING_USER/FINALIZING/CANCELLING as
            # "idle", so the old `session.state != SessionState.IDLE` check would
            # pass while the session isn't truly ready, causing queued messages to
            # be dispatched (and incorrectly routed as "answer-received" in the
            # AWAITING_USER case).
            if sv2._current_state(session) is not sv2.SessionStateV2.IDLE_READY:
                return
            if session.session_id not in self._sessions:
                return
            entry = PendingMessage.coerce(session.pending_messages.popleft())

            # Lock in this turn's user msg id from the queue entry (the
            # manager pre-saved it at queue-add time and stored its id on
            # the entry). compile_context and reflect scope to this id.
            # Synthetic entries (worker resume, worker timeout) have none.
            if entry.msg_id is not None:
                session.current_turn_user_msg_id = entry.msg_id
                # A queued user message is still a new user turn: give it the
                # same fresh LLM budget window prompt() grants on immediate
                # dispatch (manager.py "Reset the LLM wall-clock budget"
                # block). Without this, a message queued behind a long turn —
                # or one that pre-empted a reflect retry — runs on the prior
                # turn's clock; session a45fa830cef9 lost three RLM runs to a
                # cap whose window opened two turns earlier. Synthetic entries
                # keep the running clock on purpose: they continue the same
                # piece of work, and a self-triggered turn must not be able to
                # refresh its own budget.
                try:
                    from core.llm.client import reset_session_budget as _reset_budget

                    _reset_budget(session.session_id)
                except Exception as _e:
                    logger.debug("Budget reset failed for %s: %s", session.session_id, _e)

            # Start a new agent task for the pending message while lock is held
            session.task = asyncio.create_task(
                self._run_agent_safe(
                    session,
                    entry.message,
                    entry.system_prompt,
                    pre_saved=entry.pre_saved,
                )
            )

    def _find_db_orphans(self, session: AgentSession) -> list[dict]:
        """Return DB user messages that have no subsequent assistant response.

        Uses db.get_orphaned_user_messages which walks the message sequence and
        returns any user message immediately followed by another user message (or
        end-of-session) with no assistant message between them. Worker sessions
        never receive user-initiated follow-ups, so they are skipped.
        """
        if session.session_type == "worker":
            return []
        try:
            return db.get_orphaned_user_messages(session.session_id)
        except Exception as e:
            logger.warning("_find_db_orphans error for %s: %s", session.session_id[:12], e)
            return []

    async def _sweep_db_pending(
        self,
        session: AgentSession,
        exclude_msg_id: int | None = None,
    ) -> None:
        """Re-queue any DB-orphaned user messages into pending_messages.

        Called at turn finalization when pending_messages is empty (Window A:
        server restarted between FINALIZING→IDLE_READY and _process_pending).
        The duplicate-ID guard prevents double-queuing if the in-memory path
        already captured the same message.

        exclude_msg_id: the user message ID for the turn that just completed.
        Excluded so that agents which don't write an assistant message (e.g.
        test stubs) don't cause the just-processed message to re-queue itself.
        """
        orphans = self._find_db_orphans(session)
        if exclude_msg_id is not None:
            orphans = [o for o in orphans if o["id"] != exclude_msg_id]
        # Skip IDs already swept in this server run. Consecutive ask_user
        # answer chains produce user→user→user rows with no assistant between
        # them, so get_orphaned_user_messages finds them every sweep. Without
        # this guard the sweep loops forever re-queuing the same messages.
        orphans = [o for o in orphans if o["id"] not in session.swept_orphan_ids]
        if not orphans:
            return
        logger.warning(
            "Session %s: sweep recovered %d orphaned message(s) — re-queueing",
            session.session_id[:12],
            len(orphans),
        )
        now = time.monotonic()
        for o in orphans:
            msg_id = o["id"]
            session.swept_orphan_ids.add(msg_id)
            if any(PendingMessage.coerce(e).msg_id == msg_id for e in session.pending_messages):
                continue
            session.pending_messages.append(PendingMessage(o.get("content", ""), "", True, now, msg_id))
        # Only advance the watermark — never regress it. A concurrent prompt()
        # running without the lock may have already set last_user_msg_id to a
        # higher value (a freshly-queued normal message). Overwriting it with a
        # lower orphan ID would corrupt the rapid-fire combiner, causing new
        # user messages to be folded into an already-processed orphan DB row.
        if orphans[-1]["id"] > (session.last_user_msg_id or 0):
            session.last_user_msg_id = orphans[-1]["id"]
            session.last_user_msg_at = now

    async def _run_post_hooks(self, session: AgentSession) -> None:
        """Run post-task hooks (auto-title, distillation, reflect)."""
        # Only run when the session is in FINALIZING — that's the only state
        # where the agent loop has completed and post-hooks are appropriate.
        # All other states (SCOUTING, PROCESSING, AWAITING_USER, AWAITING_WORKERS,
        # IDLE_READY) must not trigger post-hooks. Using the v2 state directly
        # avoids the fragility of the legacy "idle" mapping, which collapses
        # FINALIZING, AWAITING_USER, and AWAITING_WORKERS into the same value.
        if sv2._current_state(session) is not sv2.SessionStateV2.FINALIZING:
            return
        if session.pending_messages:
            return  # Don't distill if more work queued

        from sessions.hooks import run_post_task_hooks

        session.add_background_ref()
        try:
            await run_post_task_hooks(
                session.session_id,
                emit=session.emit_event,
                session_obj=session,
            )
        except Exception as e:
            logger.error("Post-task hooks failed for %s: %s", session.session_id, e)
        finally:
            session.remove_background_ref()

    # ------------------------------------------------------------------
    # Event helpers
    # ------------------------------------------------------------------

    def emit(self, session_id: str, event: dict) -> None:
        """Emit an event to a session's subscribers."""
        session = self._sessions.get(session_id)
        if session:
            session.emit_event(event)

    def broadcast(self, event: dict) -> int:
        """Emit an event to ALL global notification subscribers AND all
        sessions with active SSE subscribers.

        Returns the number of clients reached.
        """
        from core.events import run_on_loop

        reached = 0

        # Global notification subscribers (connected regardless of session).
        # Delivery is marshaled to the event loop — broadcast() is called
        # from tool threads (ask_user, notify_user) and asyncio.Queue is not
        # thread-safe. The reached count is computed from the snapshot since
        # delivery may complete after we return.
        reached += len(self._global_subscribers)
        run_on_loop(self._deliver_global, dict(event, _global=True))

        # Session-specific subscribers (emit_event marshals internally).
        # Snapshot: broadcast() is called from tool threads (ask_user,
        # notify_user) while spawn_worker — also on a tool thread — inserts
        # into _sessions via create_session. Iterating the live dict raced
        # that insert and raised "dictionary changed size during iteration",
        # which surfaced to the user as a bogus error from ask_user.
        for session in list(self._sessions.values()):
            if session.subscribers:
                session.emit_event(dict(event))
                reached += 1
        return reached

    def _deliver_global(self, event: dict) -> None:
        """Push an event to global subscriber queues. Must run on the loop."""
        dead = []
        for q in self._global_subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            if q in self._global_subscribers:
                self._global_subscribers.remove(q)

    def subscribe(self, session_id: str) -> asyncio.Queue:
        """Subscribe to a session's events."""
        session = self.get_or_create(session_id)
        return session.subscribe()

    def subscribe_global(self) -> asyncio.Queue:
        """Subscribe to global notification events (no session required)."""
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._global_subscribers.append(q)
        return q

    def unsubscribe_global(self, q: asyncio.Queue) -> None:
        try:
            self._global_subscribers.remove(q)
        except ValueError:
            pass

    def unsubscribe(self, session_id: str, queue: asyncio.Queue) -> None:
        session = self._sessions.get(session_id)
        if session:
            session.unsubscribe(queue)

    # ------------------------------------------------------------------
    # Status and listing
    # ------------------------------------------------------------------

    def get_status(self, session_id: str) -> dict:
        session = self._sessions.get(session_id)
        if not session:
            return {"status": "unknown", "session_id": session_id}
        v2_state = sv2._current_state(session)
        return {
            "session_id": session_id,
            # Legacy "status" field — preserved for CLI/external clients.
            # Maps v2 state to the old 3-value set via COMPAT_STATUS.
            "status": sv2.compat_status(v2_state),
            # New authoritative v2 state (one of 9 values).
            "state": v2_state.value,
            "compat_status": sv2.compat_status(v2_state),
            "session_type": session.session_type,
            "error": session.error,
            "termination_reason": session.termination_reason,
            "turn_id": getattr(session, "_turn_id", 0),
            "retry_index": getattr(session, "_retry_index", 0),
            "idle_seconds": int(session.idle_seconds),
            "pending_messages": len(session.pending_messages),
            "subscribers": len(session.subscribers),
            "worker_ids": list(session.worker_ids),
            "has_background_tasks": session.has_background_tasks,
            # Kept for backwards compat (now computed properties).
            "waiting_for_input": session.waiting_for_input,
            "post_hooks_complete": session.post_hooks_complete,
            "event_seq": session.event_seq,
            "model_override": session.model_override,
        }

    def active_session_ids(self) -> list[str]:
        return list(self._sessions.keys())

    def active_count(self) -> int:
        return len(self._sessions)

    def has_active_work(self) -> bool:
        """Return True if any in-memory session is non-idle.

        Uses the v2 state machine directly. AWAITING_USER and AWAITING_WORKERS
        are excluded (agent genuinely suspended); FINALIZING is caught by the
        has_background_tasks check below (post-hooks hold a ref).
        Used by snooze to skip cycles while real work is happening.
        """
        _idle_v2 = (
            sv2.SessionStateV2.IDLE_READY,
            sv2.SessionStateV2.AWAITING_USER,
            sv2.SessionStateV2.AWAITING_WORKERS,
        )
        # Snapshot — a tool thread can insert into _sessions (spawn_worker ->
        # create_session) while this runs on the event loop. Snooze-transparent
        # types (canary) never count as active work (plan §5).
        from core.snooze import SNOOZE_TRANSPARENT_TYPES

        for session in list(self._sessions.values()):
            if session.session_type in SNOOZE_TRANSPARENT_TYPES:
                continue
            if sv2._current_state(session) not in _idle_v2:
                return True
            if session.has_background_tasks:
                return True
        return False

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def reap_idle_sessions(self, max_idle: int = 1800, protected_ids: set | None = None) -> int:
        """Remove truly-idle sessions, unstick stuck PROCESSING/COMPACTING,
        force-terminate stuck CANCELLING/FINALIZING, and apply state-specific
        timeouts per the plan's reaper rules table (9-state version).

        Rules (from the state-machine migration plan):
          - IDLE_READY:        reap if idle > max_idle, no subscribers, no background refs
          - SCOUTING:          reap if idle > 2*max_idle, no subscribers
          - PROCESSING:        unstick if idle > 300s and no background refs (agent died)
          - COMPACTING:        unstick (to FINALIZING via compaction-failed) at 120s, only if task dead
          - PAUSE_REQUESTED:   unstick at 60s, only if task dead (live loop pauses at next round gate)
          - PAUSED:            safety net at 24h OR parent deleted (orphan pause)
          - CANCELLING:        force unstick (cancel-timeout) at 30s
          - FINALIZING:        force unstick (finalize-error) at 120s
          - AWAITING_USER:     never reap for inactivity
        """
        protected = protected_ids or set()
        to_reap = []
        unstuck = 0
        processing_timeout = min(max_idle, 300)
        for sid, session in list(self._sessions.items()):
            if sid in protected:
                continue

            v2 = sv2._current_state(session)
            idle = session.idle_seconds

            if v2 is sv2.SessionStateV2.PROCESSING:
                # Only unstick if the agent task is actually gone. A single
                # round (LLM stream + tool execution) can legitimately exceed
                # processing_timeout without touching the session — slow local
                # models and 300s tool timeouts both cross the reaper tick.
                # Force-idling a live turn lets a second prompt spawn a
                # concurrent turn that interleaves writes into the same
                # transcript (same guard the SCOUTING branch uses below).
                task_alive = session.task is not None and not session.task.done()
                if task_alive:
                    continue
                if idle >= processing_timeout and not session.has_background_tasks:
                    logger.warning("Unsticking stuck PROCESSING session %s (idle=%ds)", sid, int(idle))
                    try:
                        sv2.transition(session, sv2.SessionStateV2.IDLE_READY, "reaper-unstick")
                    except Exception as _e:
                        logger.error("reaper-unstick failed for %s: %s", sid, _e)
                    session.touch()
                    session.emit_event(
                        {
                            "type": "system",
                            "content": "Session was stuck in processing and has been reset. You can send a new message.",
                        }
                    )
                    unstuck += 1
                continue

            if v2 is sv2.SessionStateV2.COMPACTING:
                # Same live-task guard as PROCESSING: compact_with_llm summarizes
                # a large transcript and only touch()es afterward, so on a slow
                # model the session can sit "idle" >120s while compaction is
                # genuinely in flight. Unsticking a live task here races its
                # eventual compact-done transition (FINALIZING → PROCESSING
                # invariant violation) and can let a second prompt start a
                # concurrent turn on the same transcript.
                task_alive = session.task is not None and not session.task.done()
                if task_alive:
                    continue
                if idle >= 120:
                    logger.warning("Unsticking stuck COMPACTING session %s (idle=%ds)", sid, int(idle))
                    # Route through FINALIZING(compaction-failed) — matches what
                    # agent.py does when compaction hard-fails, so post-hooks
                    # classify it honestly.
                    try:
                        sv2.transition(
                            session,
                            sv2.SessionStateV2.FINALIZING,
                            "compaction-failed",
                            termination_reason=sv2.TerminationReason.COMPACTION_FAILED,
                        )
                    except Exception as _e:
                        logger.error("compaction-failed unstick failed for %s: %s", sid, _e)
                    session.touch()
                    unstuck += 1
                continue

            if v2 is sv2.SessionStateV2.PAUSE_REQUESTED:
                # Live-task guard: the pause checkpoint only exists at the top
                # of a tool round, so a slow round (LLM stream on a local
                # model, or a tool call running up to its 300s timeout) can
                # legitimately keep the loop away from the checkpoint for
                # minutes. The user asked to PAUSE — cancelling their live
                # turn here destroys exactly the work they wanted to keep.
                # While the task is alive, the loop WILL observe the pause at
                # its next pre-round gate; only a dead task needs unsticking.
                task_alive = session.task is not None and not session.task.done()
                if task_alive:
                    continue
                if idle >= 60:
                    logger.warning("Unsticking stuck PAUSE_REQUESTED session %s (idle=%ds)", sid, int(idle))
                    # Mirror the PAUSED unstick pattern: set cancel_requested so the
                    # agent loop exits cooperatively if it wakes normally, set
                    # pause_event so the task unblocks from pause_event.wait()
                    # (satisfying the IDLE_READY invariant), then cancel the task
                    # as a backstop in case the loop doesn't reach its checkpoint.
                    session.cancel_requested = True
                    session.pause_event.set()
                    if session.task and not session.task.done():
                        session.task.cancel()
                    try:
                        sv2.transition(session, sv2.SessionStateV2.IDLE_READY, "reaper-unstick")
                    except Exception as _e:
                        logger.error("pause-requested unstick failed for %s: %s", sid, _e)
                    session.touch()
                    unstuck += 1
                continue

            if v2 is sv2.SessionStateV2.CANCELLING:
                if idle >= 30:
                    logger.warning("Force-unsticking stuck CANCELLING session %s (idle=%ds)", sid, int(idle))
                    try:
                        sv2.transition(
                            session,
                            sv2.SessionStateV2.IDLE_READY,
                            "cancel-timeout",
                            termination_reason=sv2.TerminationReason.CANCELLED,
                        )
                    except Exception as _e:
                        logger.error("cancel-timeout unstick failed for %s: %s", sid, _e)
                    session.touch()
                    session.emit_event(
                        {
                            "type": "system",
                            "content": "Session cancel timed out and has been force-reset.",
                        }
                    )
                    unstuck += 1
                continue

            if v2 is sv2.SessionStateV2.FINALIZING:
                if idle >= 120 and not session.has_background_tasks:
                    logger.warning("Force-unsticking stuck FINALIZING session %s (idle=%ds)", sid, int(idle))
                    try:
                        sv2.transition(session, sv2.SessionStateV2.IDLE_READY, "finalize-error")
                    except Exception as _e:
                        logger.error("finalize-error unstick failed for %s: %s", sid, _e)
                    session.touch()
                    unstuck += 1
                continue

            if v2 is sv2.SessionStateV2.PAUSED:
                # Never reap for inactivity while intentionally paused.
                # Safety net: > 24h OR parent session deleted (orphan).
                parent_gone = session.parent_session_id is not None and session.parent_session_id not in self._sessions
                if idle >= 86400 or parent_gone:
                    logger.warning(
                        "Force-cancelling orphan/stale PAUSED session %s " "(idle=%ds, parent_gone=%s)",
                        sid,
                        int(idle),
                        parent_gone,
                    )
                    # Set cancel_requested first so the agent exits on resume.
                    session.cancel_requested = True
                    # Wake the pause gate so the running task can observe the cancel.
                    session.pause_event.set()
                    try:
                        sv2.transition(session, sv2.SessionStateV2.IDLE_READY, "reaper-unstick")
                    except Exception as _e:
                        logger.error("paused unstick failed for %s: %s", sid, _e)
                    # Also cancel the asyncio task so it terminates even if the
                    # agent loop doesn't reach the cancel_requested check quickly.
                    if session.task and not session.task.done():
                        session.task.cancel()
                    session.touch()
                    unstuck += 1
                continue

            if v2 is sv2.SessionStateV2.AWAITING_USER:
                # Invariant check: a question row must exist for this session.
                try:
                    questions = db.get_questions(sid)
                except Exception:
                    questions = []
                if not questions and idle >= max_idle:
                    logger.warning(
                        "AWAITING_USER session %s has no question row " "(idle=%ds); force unstuck", sid, int(idle)
                    )
                    try:
                        sv2.transition(session, sv2.SessionStateV2.IDLE_READY, "reaper-unstick")
                    except Exception as _e:
                        logger.error("awaiting-user unstick failed for %s: %s", sid, _e)
                    session.touch()
                    unstuck += 1
                continue

            if v2 is sv2.SessionStateV2.AWAITING_WORKERS:
                # Never reap for inactivity alone — workers can take a long time.
                watched = getattr(session, "_watched_worker_ids", set())

                # Purge stale IDs from non-empty watch-sets every tick:
                # workers that have completed (or errored) but never fired
                # the watch callback (cancelled, deleted, or whose _start()
                # raised before they reached PROCESSING). Without this,
                # stale IDs both block the resume callback and hide the
                # parent from the empty-set safety net below.
                if watched:
                    stale = set()
                    for wid in list(watched):
                        w = self.get(wid)
                        if w is None:
                            stale.add(wid)  # reaped from memory = done
                            continue
                        w_v2 = sv2._current_state(w)
                        has_started = w.task is not None or getattr(w, "_turn_id", 0) > 0
                        if w_v2 is sv2.SessionStateV2.IDLE_READY:
                            # IDLE_READY-after-start = ran and settled.
                            # IDLE_READY without start but with error/term
                            # reason = spawn-time failure that already
                            # cleaned up. Either way, no longer pending.
                            if has_started or w.error or w.termination_reason:
                                stale.add(wid)
                    if stale:
                        watched -= stale
                        self._persist_watched(session)
                        logger.info(
                            "Reaper purged %d stale watched worker(s) from %s",
                            len(stale),
                            sid,
                        )
                        # If purge emptied the set, fire resume directly
                        # rather than waiting for the timeout. Mirrors
                        # _on_watched_worker_done's resume path.
                        if not watched:
                            self._spawn_detached(self._resume_from_workers(session), "resume-from-workers")
                            session.touch()
                            unstuck += 1
                            continue

                # Empty watch-set safety net (kept from the original logic).
                if not watched and idle >= max_idle:
                    logger.warning(
                        "AWAITING_WORKERS session %s has no watched workers " "(idle=%ds); force unstuck",
                        sid,
                        int(idle),
                    )
                    try:
                        sv2.transition(session, sv2.SessionStateV2.IDLE_READY, "reaper-unstick")
                    except Exception as _e:
                        logger.error("awaiting-workers unstick failed for %s: %s", sid, _e)
                    session.touch()
                    unstuck += 1
                    continue

                # Wall-clock timeout. The blocking-poll path of await_workers
                # caps at 1800s; suspend mode had no equivalent (worker-timeout
                # was a dead transition until this fix). Use 2x max_idle to
                # give legitimately long-running orchestrations more headroom
                # than the blocking variant, but still bound the wait.
                workers_timeout = max_idle * 2
                if watched and idle >= workers_timeout:
                    logger.warning(
                        "AWAITING_WORKERS session %s timed out (idle=%ds, " "watched=%d); firing worker-timeout",
                        sid,
                        int(idle),
                        len(watched),
                    )
                    try:
                        sv2.transition(session, sv2.SessionStateV2.IDLE_READY, "worker-timeout")
                    except Exception as _e:
                        logger.error("worker-timeout transition failed for %s: %s", sid, _e)
                    # Without a synthetic prompt, the parent silently resumes
                    # IDLE_READY and the LLM has no idea its workers timed out.
                    # Queue a message so the next turn (whoever triggers it)
                    # has the timeout context.
                    timeout_msg = (
                        f"[Worker wait timed out after {int(idle)}s — "
                        f"{len(watched)} worker(s) did not complete in time]\n"
                        "Use check_workers() to inspect their state and call "
                        "get_worker_result(worker_id) for any that did finish. "
                        "Decide whether to retry, cancel, or proceed without them."
                    )
                    session.pending_messages.append(PendingMessage(timeout_msg, None, False))
                    session._watched_worker_ids.clear()
                    self._persist_watched(session)
                    self._spawn_detached(self._process_pending(session), "process-pending")
                    session.touch()
                    unstuck += 1
                continue

            if v2 is sv2.SessionStateV2.SCOUTING:
                # Unstick if the in-memory task is gone — typically means the
                # server restarted while scout was running. Without this the
                # session sits in SCOUTING forever and any new prompt the user
                # sends just queues behind a phantom turn that will never
                # finish. The same processing_timeout used for PROCESSING is a
                # reasonable bound — scout itself is capped at 180s by config.
                task_alive = session.task is not None and not session.task.done()
                if not task_alive and idle >= processing_timeout:
                    logger.warning(
                        "Unsticking stuck SCOUTING session %s (idle=%ds, no live task)",
                        sid,
                        int(idle),
                    )
                    try:
                        sv2.transition(session, sv2.SessionStateV2.IDLE_READY, "reaper-unstick")
                    except Exception as _e:
                        logger.error("scouting unstick failed for %s: %s", sid, _e)
                    session.touch()
                    session.emit_event(
                        {
                            "type": "system",
                            "content": "Session was stuck in scouting (likely a server restart) and has been reset. You can send a new message.",
                        }
                    )
                    unstuck += 1
                    # Drain any queued prompts that piled up behind the phantom turn.
                    if session.pending_messages:
                        self._spawn_detached(self._process_pending(session), "process-pending")
                    continue
                # Long-stuck scouts with no subscribers fall through to the
                # original reap path so we don't keep dead sessions forever.
                if idle >= max_idle * 2 and not session.subscribers:
                    logger.warning("Reaping stuck SCOUTING session %s (idle=%ds)", sid, int(idle))
                    to_reap.append(sid)
                continue

            # IDLE_READY — truly idle, eligible for reap
            if v2 is sv2.SessionStateV2.IDLE_READY:
                if session.subscribers:
                    continue
                if session.has_background_tasks:
                    continue
                if idle < max_idle:
                    continue
                to_reap.append(sid)

        for sid in to_reap:
            self.remove(sid)
            logger.debug("Reaped idle session %s", sid)

        return len(to_reap) + unstuck

    def reap_dead_subscribers(self) -> int:
        """Remove subscriber queues that are full (dead clients)."""
        reaped = 0
        for session in list(self._sessions.values()):
            dead = [q for q in session.subscribers if q.full()]
            for q in dead:
                session.subscribers.remove(q)
                reaped += 1
        return reaped


# Module singleton
_manager: SessionManager | None = None


def get_manager() -> SessionManager:
    global _manager
    if _manager is None:
        _manager = SessionManager()
    return _manager
