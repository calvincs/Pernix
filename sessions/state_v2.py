"""Pernix — True session state machine (v2, successor to state.py).

Migration status: COMPLETE. This module is the only state machine. The
pre-v2 5-state enum, its in-memory mirror field, the legacy↔v2 bridge
tables and the per-transition UPDATE of the legacy `sessions.state`
column are all deleted. Every state
mutation in sessions/manager.py, core/agent.py and core/extensions/ goes
through transition(); reads go through _current_state(). The legacy DB
column still exists (dropping it would mean a table rebuild for no gain)
but nothing here writes it — see db/database.py's migration v16 note.

Design summary:

- 10 named states describe lifecycle; cooperative pause/cancel signals remain.
- Every transition has an explicit edge in TRANSITIONS with a bounded
  vocabulary of reasons. There is no force-state escape hatch; if you need
  to "force," add the edge and give the reason a name (e.g. cancel-timeout).
- Valid transitions atomically persist the snapshot and session_state_log
  (migration v13), update memory, then emit session.state_changed.
- Undeclared edges are logged and rejected without changing state.
- Invariants per state are checked on entry. Violations are logged loudly
  but remain diagnostic; graph validation is enforced.

Cross-pollination writes directly to session_messages as a memory write,
not a prompt, and does not go through transition().
"""

from __future__ import annotations

import logging
import time
from enum import Enum
from typing import TYPE_CHECKING

from db import models as db

if TYPE_CHECKING:
    from sessions.state import AgentSession

logger = logging.getLogger("pernix.session.state_v2")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class SessionStateV2(str, Enum):
    """The ten lifecycle states, with separate cooperative control signals."""

    IDLE_READY = "idle_ready"
    SCOUTING = "scouting"
    PROCESSING = "processing"
    COMPACTING = "compacting"
    PAUSE_REQUESTED = "pause_requested"
    PAUSED = "paused"
    CANCELLING = "cancelling"
    FINALIZING = "finalizing"
    AWAITING_USER = "awaiting_user"
    AWAITING_WORKERS = "awaiting_workers"


class TerminationReason(str, Enum):
    """Classification of a turn's terminal outcome. Copied into
    session_state_log on FINALIZING/CANCELLING entry so post-mortem
    queries can distinguish 'error during scout' from 'error during
    agent loop' without re-reading message history."""

    COMPLETE = "complete"
    ROUND_CEILING = "round_ceiling"  # the round budget ran out
    STUCK_LOOP = "stuck_loop"  # stuck detector force-broke a repetition loop
    COMPACTION_FAILED = "compaction_failed"
    CANCELLED = "cancelled"
    ERROR = "error"
    SCOUT_ERROR = "scout_error"
    BUDGET_EXHAUSTED = "budget_exhausted"  # LLM session-time budget hit mid-turn
    INTERRUPTED = "interrupted"  # the process died mid-turn; boot reconcile found the wreck


# ---------------------------------------------------------------------------
# Transition graph
# ---------------------------------------------------------------------------
# Map (from_state, reason) → to_state. Exhaustive: any (from, reason) not
# in this table is rejected and logged as invariant-violation.
#
# Reason vocabulary (also mirrored in migration v13 documentation; keep
# the two in sync):
#
#   prompt-arrived, scout-done, scout-error, compact-proactive,
#   compact-critical, compact-overflow, compact-done, compaction-failed,
#   ask-user, pause-requested, pause-observed, resume, cancel-requested,
#   cancel-during-pause, cancel-complete, cancel-timeout, loop-complete,
#   round-ceiling, stuck-loop, agent-error, reflect-retry, eval-retry, turn-complete,
#   finalize-error, answer-received, question-dismissed,
#   invariant-violation, reaper-unstick

S = SessionStateV2

TRANSITIONS: dict[tuple[S, str], S] = {
    # IDLE_READY → SCOUTING on any new prompt (initial or queued-drain)
    (S.IDLE_READY, "prompt-arrived"): S.SCOUTING,
    # Scout phase outcomes
    (S.SCOUTING, "scout-done"): S.PROCESSING,
    (S.SCOUTING, "scout-error"): S.FINALIZING,
    (S.SCOUTING, "cancel-requested"): S.CANCELLING,
    # Compaction round-trip (can repeat within one PROCESSING phase)
    (S.PROCESSING, "compact-proactive"): S.COMPACTING,
    (S.PROCESSING, "compact-critical"): S.COMPACTING,
    (S.PROCESSING, "compact-overflow"): S.COMPACTING,
    (S.COMPACTING, "compact-done"): S.PROCESSING,
    (S.COMPACTING, "compact-done-paused"): S.PAUSE_REQUESTED,
    (S.PAUSE_REQUESTED, "compact-proactive"): S.COMPACTING,
    (S.PAUSE_REQUESTED, "compact-critical"): S.COMPACTING,
    (S.PAUSE_REQUESTED, "compact-overflow"): S.COMPACTING,
    (S.COMPACTING, "compaction-failed"): S.FINALIZING,
    # Agent loop exits + interrupts
    (S.PROCESSING, "ask-user"): S.AWAITING_USER,
    (S.PROCESSING, "pause-requested"): S.PAUSE_REQUESTED,
    (S.PROCESSING, "cancel-requested"): S.CANCELLING,
    (S.PROCESSING, "loop-complete"): S.FINALIZING,
    (S.PROCESSING, "round-ceiling"): S.FINALIZING,
    (S.PROCESSING, "stuck-loop"): S.FINALIZING,
    (S.PROCESSING, "agent-error"): S.FINALIZING,
    # Pause/resume
    (S.PAUSE_REQUESTED, "pause-observed"): S.PAUSED,
    (S.PAUSE_REQUESTED, "resume"): S.PROCESSING,
    (S.PAUSE_REQUESTED, "loop-complete"): S.FINALIZING,
    (S.PAUSE_REQUESTED, "round-ceiling"): S.FINALIZING,
    (S.PAUSE_REQUESTED, "stuck-loop"): S.FINALIZING,
    (S.PAUSE_REQUESTED, "agent-error"): S.FINALIZING,
    (S.PAUSE_REQUESTED, "ask-user"): S.AWAITING_USER,
    (S.PAUSE_REQUESTED, "workers-dispatched"): S.AWAITING_WORKERS,
    (S.PAUSE_REQUESTED, "cancel-during-pause"): S.CANCELLING,
    (S.PAUSED, "resume"): S.PROCESSING,
    (S.PAUSED, "cancel-during-pause"): S.CANCELLING,
    # Cancellation terminal
    (S.CANCELLING, "cancel-complete"): S.IDLE_READY,
    (S.CANCELLING, "cancel-timeout"): S.IDLE_READY,
    # Finalizing — either loop back for reflect/eval retry, or terminate the turn
    (S.FINALIZING, "cancel-requested"): S.CANCELLING,
    (S.FINALIZING, "reflect-retry"): S.SCOUTING,
    (S.FINALIZING, "eval-retry"): S.SCOUTING,
    (S.FINALIZING, "turn-complete"): S.IDLE_READY,
    (S.FINALIZING, "finalize-error"): S.IDLE_READY,
    # Awaiting user
    (S.AWAITING_USER, "answer-received"): S.SCOUTING,
    (S.AWAITING_USER, "cancel-requested"): S.CANCELLING,
    (S.AWAITING_USER, "question-dismissed"): S.IDLE_READY,
    # Awaiting workers — parent suspends until watched workers complete
    (S.PROCESSING, "workers-dispatched"): S.AWAITING_WORKERS,
    (S.AWAITING_WORKERS, "workers-complete"): S.SCOUTING,
    (S.AWAITING_WORKERS, "worker-timeout"): S.IDLE_READY,
    (S.AWAITING_WORKERS, "cancel-requested"): S.CANCELLING,
    (S.AWAITING_WORKERS, "reaper-unstick"): S.IDLE_READY,
    # Compaction phase: cancellation and error escape hatches
    (S.COMPACTING, "cancel-requested"): S.CANCELLING,
    (S.COMPACTING, "agent-error"): S.FINALIZING,
    # Reaper escape hatches (kept explicit rather than a generic force)
    (S.PROCESSING, "reaper-unstick"): S.IDLE_READY,
    (S.SCOUTING, "reaper-unstick"): S.IDLE_READY,
    (S.PAUSE_REQUESTED, "reaper-unstick"): S.IDLE_READY,
    (S.PAUSED, "reaper-unstick"): S.IDLE_READY,  # 24h safety net / orphaned parent
    (S.AWAITING_USER, "reaper-unstick"): S.IDLE_READY,
    # A crash can persist either of these too, and the boot reconcile walks
    # every state a dead turn can be found in — not only the ones the reaper
    # used to reach.
    (S.FINALIZING, "reaper-unstick"): S.IDLE_READY,
    (S.COMPACTING, "reaper-unstick"): S.IDLE_READY,
    # cancel-timeout: emergency exit when cancel was requested but the session
    # never reached CANCELLING (race between the cancel request and the agent
    # returning normally, or a failed transition to CANCELLING). One edge per
    # state that the cancel-finally handler in manager.py can realistically
    # encounter. Mirrors the "reaper-unstick" coverage pattern.
    (S.SCOUTING, "cancel-timeout"): S.IDLE_READY,
    (S.PROCESSING, "cancel-timeout"): S.IDLE_READY,
    (S.COMPACTING, "cancel-timeout"): S.IDLE_READY,
    (S.PAUSE_REQUESTED, "cancel-timeout"): S.IDLE_READY,
    (S.PAUSED, "cancel-timeout"): S.IDLE_READY,
    (S.AWAITING_USER, "cancel-timeout"): S.IDLE_READY,
    (S.AWAITING_WORKERS, "cancel-timeout"): S.IDLE_READY,
    (S.FINALIZING, "cancel-timeout"): S.IDLE_READY,
}

# Reasons that trigger a new turn_id (vs. reusing the current one).
_NEW_TURN_REASONS: frozenset[str] = frozenset(
    {
        "prompt-arrived",
        "answer-received",
        "workers-complete",
    }
)

# Reasons that increment retry_index (reflect/eval retry within same turn).
_RETRY_REASONS: frozenset[str] = frozenset({"reflect-retry", "eval-retry"})

# Reasons that increment compaction_count (PROCESSING↔COMPACTING loops).
_COMPACT_ENTER_REASONS: frozenset[str] = frozenset(
    {
        "compact-proactive",
        "compact-critical",
        "compact-overflow",
    }
)


# ---------------------------------------------------------------------------
# State → compat-status mapping
# ---------------------------------------------------------------------------
# The one surviving piece of API compatibility: get_status() reports a
# coarse 3-value `compat_status` (idle | scouting | processing) alongside
# the real v2 `state`, so external consumers written against the pre-v2
# vocabulary keep working. Nothing reads it back into the state machine.

COMPAT_STATUS: dict[S, str] = {
    S.IDLE_READY: "idle",
    S.SCOUTING: "scouting",
    S.PROCESSING: "processing",
    S.COMPACTING: "processing",
    S.PAUSE_REQUESTED: "processing",
    S.PAUSED: "processing",
    S.CANCELLING: "processing",
    S.FINALIZING: "processing",
    S.AWAITING_USER: "idle",
    S.AWAITING_WORKERS: "idle",
}


# ---------------------------------------------------------------------------
# Invariants (checked on entry; violations logged, not raised)
# ---------------------------------------------------------------------------


def _check_invariants(session: "AgentSession", to: S) -> list[str]:
    """Return a list of invariant-violation messages (empty if all hold).

    Violations are logged as warnings but do NOT abort the transition — the
    point is forensics, not crash-on-bug. The reaper cross-checks some
    invariants more expensively at tick time (e.g. AWAITING_USER → question
    row must exist)."""
    violations: list[str] = []

    pause_set = session.pause_event.is_set()
    task = session.task
    task_done = task is None or task.done()

    if to is S.IDLE_READY:
        if not pause_set:
            violations.append("IDLE_READY expects pause_event.is_set()")
        if session.cancel_requested:
            violations.append("IDLE_READY expects cancel_requested=False")
    elif to is S.PROCESSING:
        if not pause_set:
            violations.append("PROCESSING expects pause_event.is_set()")
        if task_done:
            violations.append("PROCESSING expects a running task")
    elif to is S.PAUSE_REQUESTED:
        # Historically worker-only; the user-facing pause endpoint now uses
        # the same transition for main sessions — the agent loop's pause
        # checkpoint is type-agnostic.
        if pause_set:
            violations.append("PAUSE_REQUESTED expects pause_event.is_set()==False")
    elif to is S.PAUSED:
        if pause_set:
            violations.append("PAUSED expects pause_event.is_set()==False")
    elif to is S.CANCELLING:
        if not pause_set:
            # A paused loop can only observe cancel if pause_event is re-set
            violations.append("CANCELLING expects pause_event.is_set() so cooperative cancel can wake")
    elif to is S.FINALIZING:
        # Plan invariant: post-hooks coroutine running ⇒ _background_refs ≥ 1.
        # _run_post_hooks() adds a ref before its body; this check should pass
        # once post-hooks begin. Entering FINALIZING *before* post-hooks adds
        # the ref (e.g. from the normal loop-complete path) is still legal —
        # we only warn if FINALIZING lingers with refs=0 for suspect
        # diagnostics.
        if session.termination_reason is None:
            violations.append("FINALIZING expects termination_reason to be set")
    elif to is S.AWAITING_USER:
        # Check that at least one open question row exists for this session.
        # Cheap (indexed query) and catches the case where ask_user posted
        # the event but the DB write failed silently.
        try:
            from db import models as _db  # local to avoid cycle

            open_q = _db.get_questions(session.session_id)
            if not open_q:
                violations.append("AWAITING_USER expects at least one open question row")
        except Exception:
            # DB unavailable — skip rather than fail the transition
            pass
    elif to is S.AWAITING_WORKERS:
        if not getattr(session, "_watched_worker_ids", None):
            violations.append("AWAITING_WORKERS expects _watched_worker_ids to be non-empty")

    return violations


# ---------------------------------------------------------------------------
# Mutator
# ---------------------------------------------------------------------------


def transition(
    session: "AgentSession",
    to: S,
    reason: str,
    *,
    termination_reason: TerminationReason | None = None,
) -> bool:
    """Transition `session` to state `to` with `reason`.

    Synchronous — no awaits inside. This is a load-bearing choice: asyncio
    delivers CancelledError only at `await` points, so a sync function
    running on the event loop runs to completion atomically with respect
    to cancel. That gives us the effect of `asyncio.shield` for free —
    the state_log row and the in-memory mutation can't be half-written
    when a second cancel lands mid-transition. Adding an `await` here
    would reintroduce the torn-write hazard.

    Callers from async contexts invoke it directly (no `await`). Mutations
    happen only from the session's own asyncio task (the event loop's
    single-thread guarantee provides serialization); the reaper, which
    runs on a maintenance task, calls this same function to force-unstick
    sessions — safe because the stuck session's task is not running.

    Side effects, in order:
      1. Validate the edge (log and reject if unknown).
      2. Compute elapsed_ms since previous state entry.
      3. Persist session_state_log and sessions.state_v2 in one transaction.
      4. Update in-memory state and counters.
      5. Emit `session.state_changed` SSE with available controls.
    """
    # 1. Edge lookup
    current = _current_state(session)
    key = (current, reason)
    expected_to = TRANSITIONS.get(key)
    edge_ok = expected_to is to
    if not edge_ok:
        logger.warning(
            "Invariant violation: transition %s --(%s)--> %s is not in graph" " (expected target: %s) — rejected",
            current.value,
            reason,
            to.value,
            expected_to.value if expected_to else "none",
        )

    if not edge_ok:
        try:
            db.add_state_log(
                session.session_id,
                turn_id=session._turn_id,
                from_state=current.value,
                to_state=current.value,
                reason=f"invariant-violation:{reason}",
                timestamp_ms=int(time.time() * 1000),
            )
        except Exception:
            logger.exception("Could not record rejected transition")
        return False

    previous_pause = session.pause_event.is_set()
    previous_termination = session.termination_reason
    if to not in {S.PAUSE_REQUESTED, S.PAUSED, S.COMPACTING} and current in {S.PAUSE_REQUESTED, S.PAUSED}:
        session.pause_event.set()
    if termination_reason is not None:
        session.termination_reason = termination_reason.value

    # 1a. State-specific invariants
    for msg in _check_invariants(session, to):
        logger.warning("State invariant [%s]: %s", to.value, msg)

    # 2. Timing
    now_ms = time.monotonic_ns() // 1_000_000
    prev_ms = getattr(session, "_state_entered_ms", None)
    elapsed = (now_ms - prev_ms) if prev_ms else None

    # 3. Turn/retry/compaction accounting
    turn_id = getattr(session, "_turn_id", 0)
    parent_turn_id: int | None = None
    retry_index = getattr(session, "_retry_index", 0)
    compaction_count = getattr(session, "_compaction_count", 0)

    if reason in _NEW_TURN_REASONS:
        if reason == "answer-received":
            parent_turn_id = turn_id  # chain to the pre-answer turn
        turn_id = turn_id + 1
        retry_index = 0
        compaction_count = 0
    elif reason in _RETRY_REASONS:
        retry_index += 1
        compaction_count = 0
    elif reason in _COMPACT_ENTER_REASONS:
        compaction_count += 1

    # 4. Log row (durable; synchronous INSERT)
    from sessions.state import turn_state

    _turn = turn_state(session)
    try:
        db.add_state_log(
            session.session_id,
            turn_id=turn_id,
            parent_turn_id=parent_turn_id,
            retry_index=retry_index,
            compaction_count=compaction_count,
            from_state=current.value,
            to_state=to.value,
            reason=reason if edge_ok else f"invariant-violation:{reason}",
            termination_reason=termination_reason.value if termination_reason else None,
            reflect_count=int(_turn.reflect_count or 0),
            eval_count=int(_turn.eval_count or 0),
            timestamp_ms=int(time.time() * 1000),
            elapsed_ms=elapsed,
            persist_state=True,
        )
    except Exception:
        session.termination_reason = previous_termination
        if previous_pause:
            session.pause_event.set()
        else:
            session.pause_event.clear()
        # No published or in-memory state may get ahead of its durable record.
        raise

    # 5. In-memory mutation (only after durable persistence succeeds)
    session._state_entered_ms = now_ms
    session._turn_id = turn_id
    # Trial arms (W6) key off the turn, so the key is stamped exactly where
    # the turn id is — one string per turn, computed once, read by the two
    # prompt builders and by the grade that records what they rendered.
    session.turn_key = f"{session.session_id}:{turn_id}"
    session._retry_index = retry_index
    session._compaction_count = compaction_count
    session._state_v2 = to
    if termination_reason is not None:
        session.termination_reason = termination_reason.value
    session.touch()

    # 6. SSE event — payload mirrors the log row so reconnecting subscribers
    # can reconcile against /state-log deterministically.
    try:
        session.emit_event(
            {
                "type": "session.state_changed",
                "from": current.value,
                "to": to.value,
                "reason": reason if edge_ok else f"invariant-violation:{reason}",
                "turn_id": turn_id,
                "retry_index": retry_index,
                "compaction_count": compaction_count,
                "termination_reason": termination_reason.value if termination_reason else None,
                "parent_turn_id": parent_turn_id,
                "capabilities": capabilities(session),
            }
        )
    except Exception as e:
        logger.error("state_changed emit failed for %s: %s", session.session_id, e)

    return True


# ---------------------------------------------------------------------------
# State accessors
# ---------------------------------------------------------------------------


def _current_state(session: "AgentSession") -> S:
    """Read the session's state.

    IDLE_READY is the default for a session object that has never
    transitioned (a freshly constructed AgentSession, or one rehydrated
    from a DB row predating migration v16's state_v2 column)."""
    v2 = getattr(session, "_state_v2", None)
    return v2 if isinstance(v2, S) else S.IDLE_READY


def _set_state(session: "AgentSession", to: S) -> None:
    """Write the session's state, in memory and in the DB.

    Persists `state_v2` to the sessions table so a restart can restore the
    true state. Failure to persist is non-fatal — the in-memory mutation
    has already succeeded and the state_log row is the forensic record.
    """
    session._state_v2 = to
    try:
        db.set_session_state_v2(session.session_id, to.value)
    except Exception as e:
        logger.error("state_v2 persist failed for %s (%s): %s", session.session_id, to.value, e)


def compat_status(to: S | str) -> str:
    """Map a state to the coarse 3-value status set for API compat."""
    if isinstance(to, str):
        try:
            to = S(to)
        except ValueError:
            return "unknown"
    return COMPAT_STATUS.get(to, "unknown")


def capabilities(session: "AgentSession") -> dict[str, bool]:
    """Control availability shared by API, tools and clients."""
    current = _current_state(session)
    live = session.task is not None and not session.task.done()
    return {
        "pause": live and current is S.PROCESSING and not session.cancel_requested,
        "resume": live and current in {S.PAUSE_REQUESTED, S.PAUSED} and not session.cancel_requested,
        "steer": not session.cancel_requested
        and (
            current is S.AWAITING_WORKERS
            or (live and current in {S.SCOUTING, S.PROCESSING, S.COMPACTING, S.PAUSE_REQUESTED, S.PAUSED})
        ),
        "cancel": live or current in {S.AWAITING_USER, S.AWAITING_WORKERS},
    }
