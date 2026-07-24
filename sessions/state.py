"""Pernix — AgentSession dataclass and session state machine."""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, NamedTuple


class PendingMessage(NamedTuple):
    """One entry on AgentSession.pending_messages.

    Producers used to append two different tuple shapes — a 5-tuple from the
    normal queue and orphan-sweep paths, and a bare 3-tuple from the worker
    resume and worker-timeout paths. _process_pending coped via
    `entry[4] if len(entry) >= 5`, but the rapid-fire combiner and the orphan
    sweep guarded with `len(e) >= 5 and e[4] == ...` and therefore skipped
    short entries silently. That was only harmless because synthetic resume
    messages have no DB row to match against — a shape waiting to be tripped
    over by the next producer.

    Fields:
        message:       the user-visible text handed to the agent
        system_prompt: per-turn system prompt override (None for synthetic)
        pre_saved:     True when the DB row already exists, so run_agent
                       must not insert a second one
        queued_at:     time.monotonic() at enqueue; feeds the rapid-fire window
        msg_id:        DB row id, or None for synthetic messages that were
                       never persisted (worker resume, worker timeout)
    """

    message: str
    system_prompt: str | None = ""
    pre_saved: bool = False
    queued_at: float = 0.0
    msg_id: int | None = None

    @classmethod
    def coerce(cls, entry) -> "PendingMessage":
        """Accept a legacy plain tuple and normalize it.

        Defensive: keeps older callers and test fixtures that append raw
        tuples working, so consumers can use attribute access unconditionally.
        """
        if isinstance(entry, cls):
            return entry
        return cls(*entry)


class SessionState(str, Enum):
    """Legacy session state enum (pre-v2).

    The authoritative state machine is now `sessions.state_v2.SessionStateV2`
    (9 states). This enum is retained only as a compatibility mirror so that:
      * existing in-memory consumers reading `session.state` keep working
      * DB rows with legacy values ("error", "deleted") still load
      * the reaper's signature-based checks still compile

    `ERROR` and `DELETED` are **deprecated**: production code no longer writes
    them (Stage 1 of the state-machine migration). The v2 bridge maps legacy
    "error" → FINALIZING, "deleted" → IDLE_READY so any stale DB values
    continue to hydrate sensibly.

    Use sessions.state_v2.transition() for all state mutations. Tests may
    still assign `session.state` directly or use `_force_state_for_tests`
    for scenario setup.
    """

    IDLE = "idle"
    SCOUTING = "scouting"
    PROCESSING = "processing"
    ERROR = "error"  # deprecated — no production writes; DB-load only
    DELETED = "deleted"  # deprecated — was never set in v1 either


@dataclass
class AgentSession:
    """In-memory state for an active session."""

    session_id: str
    state: SessionState = SessionState.IDLE
    task: asyncio.Task | None = None

    # Event system
    events: deque = field(default_factory=lambda: deque(maxlen=2000))
    event_seq: int = 0
    subscribers: list = field(default_factory=list)  # list of asyncio.Queue

    # Error tracking
    error: str | None = None

    # Terminal exit reason for the most recent agent loop run. Survives the
    # post-turn force-reset to IDLE so downstream hooks (_finalize_worker,
    # get_worker_result) can classify completion honestly instead of
    # inferring from state.
    # Legal values: "complete", "round_ceiling", "compaction_failed",
    # "cancelled", "error". None = never ran or not yet classified.
    termination_reason: str | None = None

    # Session type
    session_type: str = "normal"  # normal | worker | cron
    parent_session_id: str | None = None
    worker_ids: list = field(default_factory=list)

    # Cooperative cancellation flag — checked by agent loop, tools, post-hooks
    cancel_requested: bool = False

    # Message queue (when agent is busy) — deque for O(1) popleft
    pending_messages: deque = field(default_factory=deque)

    # Last user message id+timestamp — used by the rapid-fire combiner in
    # sessions/manager.py to fold messages that arrive within a short window
    # into the same DB row. None when no user turn has run yet on this
    # in-memory session object (after a server restart it stays None until
    # the next prompt — that's fine: rapid-fire only matters within seconds,
    # which doesn't survive a restart anyway).
    # NOTE: this points at the LATEST user message including queued ones, so
    # it's not the right choice for "this turn's user msg" — see below.
    last_user_msg_id: int | None = None
    last_user_msg_at: float = 0.0  # time.monotonic() value

    # Tracks message IDs already re-queued by the orphan sweep or Window B
    # recovery. Prevents perpetual re-queuing of consecutive ask_user answer
    # chains (user→user→user rows with no assistant between them in the DB
    # sequence) — once an orphan ID is swept once, it is never swept again in
    # this server run. Clears on restart (which is fine: genuine orphans from
    # before a restart get one re-queue, then are tracked going forward).
    swept_orphan_ids: set = field(default_factory=set)

    # The user message id that the CURRENTLY RUNNING agent turn is processing.
    # Stable across the whole turn (scout + agent rounds + reflect/retries) so
    # compile_context and reflect can scope to this turn's request and ignore
    # later user messages queued for future turns. Set by run_agent; cleared
    # when the turn finalizes.
    current_turn_user_msg_id: int | None = None

    # Concurrency
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _event_lock: threading.Lock = field(default_factory=threading.Lock)

    # Worker pause/resume
    pause_event: asyncio.Event = field(default_factory=lambda: _make_set_event())

    # Per-session model override (for workers with specific model needs)
    model_override: str | None = None

    # Per-session context budget override, paired with model_override. When a
    # switch_model call scales the budget to the new model's capacity, the
    # value lives here instead of on global settings so concurrent sessions
    # on different-sized models don't clobber each other's budgets.
    context_budget_override: int | None = None

    # Tracks the model before an agent-initiated switch_model call.
    # Set on the first switch_model in a turn; restored after the turn completes.
    _model_before_agent_switch: str | None = None
    _budget_before_agent_switch: int | None = None

    # Scout report cache
    last_scout_report: Any = None  # ScoutReport or None

    # Reflect (post-execution verification)
    reflect_count: int = 0  # Retries used this turn
    reflect_lessons: str = ""  # Lessons from prior attempts
    reflect_retry_requested: bool = False

    # Worker watch-set: worker IDs this session is waiting on (Gap 1+2+5)
    _watched_worker_ids: set = field(default_factory=set)

    # Tool execution summary for reflect diagnostic recovery (LogAct-inspired)
    last_tool_summary: dict = field(default_factory=dict)

    # Evaluation (feature QA)
    eval_count: int = 0  # Eval retries used this turn
    eval_retry_requested: bool = False

    # Activity tracking
    last_activity_time: float = field(default_factory=time.time)

    # Background task reference counting (immune to reaping when > 0)
    _background_refs: int = 0

    # Per-session dangerous tool approvals. Populated by approve_dangerous_tool()
    # after the user confirms via ask_user.
    #
    # Structure: {tool_name: {"scope": str, "persistent": bool}}
    #   scope      — human-readable description of the SPECIFIC action approved
    #                (e.g. "list running processes" not just "bash")
    #   persistent — False (default): consumed after one use, next call needs
    #                new approval; True: stays for session lifetime (for genuinely
    #                repetitive low-risk actions like "browse several web pages").
    #
    # In-memory only — cleared on server restart, requiring re-approval.
    _approved_dangerous_tools: dict = field(default_factory=dict)

    # Per-session memory-recall ledger. Tracks "file_name@epoch" keys for memory
    # entries already surfaced to the model in this session so a follow-up recall
    # (or the search_web internal-knowledge preamble) can collapse duplicates to
    # a short reference instead of re-emitting full bodies. In-memory only.
    # Lock guards check-and-record against parallel tool calls in the same round
    # (e.g. recall + search_web fanned out via asyncio.gather).
    _seen_memory_keys: set = field(default_factory=set)
    _seen_memory_lock: threading.Lock = field(default_factory=threading.Lock)

    # v2 state machine fields — written exclusively by sessions.state_v2.transition().
    # Declared here so they appear in __repr__, satisfy type checkers, and avoid
    # getattr() fallbacks at every read site. _state_v2 typed Any to prevent a
    # circular import with state_v2.py (which imports AgentSession under TYPE_CHECKING).
    _state_v2: Any = field(default=None)
    _turn_id: int = field(default=0)
    _retry_index: int = field(default=0)
    _compaction_count: int = field(default=0)
    _state_entered_ms: int | None = field(default=None)

    def _force_state_for_tests(self, new_state: SessionState, reason: str = "") -> None:
        """TEST-ONLY: force a legacy-enum state change without validation.

        Production code MUST go through `sessions.state_v2.transition()` which
        validates against the graph, writes the state_log row, and emits
        session.state_changed. This underscore-prefixed method exists so test
        fixtures can arbitrarily set up a session in any legacy state (e.g.
        assert snooze skips on ERROR) without routing through a full turn.

        tests/test_state_machine_invariants.py enforces that this method is
        only called from tests/ files.
        """
        import logging as _logging

        _logging.getLogger("pernix.session.state").debug(
            "[test] Force state %s → %s%s",
            self.state.value,
            new_state.value,
            f" ({reason})" if reason else "",
        )
        self.state = new_state

    def emit_event(self, event: dict) -> None:
        """Broadcast event to all subscribers and buffer it.

        Safe from any thread: the buffer append is lock-guarded, and the
        subscriber-queue delivery is marshaled onto the main event loop —
        asyncio.Queue.put_nowait from a tool thread wakes getters without
        waking the selector, so events could arrive late or corrupt loop
        internals (the question modal appearing seconds after ask_user was
        exactly this shape).
        """
        with self._event_lock:
            self.event_seq += 1
            event["_seq"] = self.event_seq
            event["session_id"] = self.session_id
            if "timestamp" not in event:
                event["timestamp"] = time.time()
            self.events.append(event)

        from core.events import run_on_loop

        run_on_loop(self._deliver_to_subscribers, event)

    def _deliver_to_subscribers(self, event: dict) -> None:
        """Push an event to subscriber queues. Must run on the event loop."""
        dead = []
        subscriber_snapshot = list(self.subscribers)
        for q in subscriber_snapshot:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            if q in self.subscribers:
                self.subscribers.remove(q)

    def subscribe(self) -> asyncio.Queue:
        """Create a new subscriber queue."""
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self.subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        """Remove a subscriber queue."""
        try:
            self.subscribers.remove(q)
        except ValueError:
            pass

    def touch(self) -> None:
        """Update last activity time."""
        self.last_activity_time = time.time()

    def add_background_ref(self) -> None:
        self._background_refs += 1

    def remove_background_ref(self) -> None:
        self._background_refs = max(0, self._background_refs - 1)

    @property
    def has_background_tasks(self) -> bool:
        return self._background_refs > 0

    @property
    def idle_seconds(self) -> float:
        return time.time() - self.last_activity_time

    @property
    def post_hooks_complete(self) -> bool:
        """True when the turn (including post-hooks) has fully settled.

        Derived from the v2 state machine: True iff state == IDLE_READY.
        Retained as a read-only property so existing consumers (API status
        payloads, frontend scripts, orchestration checks) keep working
        while the migration proceeds. Writers must go through
        state_v2.transition() — bare assignments are gone."""
        from sessions.state_v2 import SessionStateV2, _current_state

        return _current_state(self) == SessionStateV2.IDLE_READY

    @property
    def waiting_for_input(self) -> bool:
        """True iff the turn is suspended on ask_user.

        Derived from the v2 state machine: True iff state == AWAITING_USER."""
        from sessions.state_v2 import SessionStateV2, _current_state

        return _current_state(self) == SessionStateV2.AWAITING_USER

    @property
    def waiting_for_workers(self) -> bool:
        """True iff the session suspended via await_workers(suspend=True).

        Derived from the v2 state machine: True iff state == AWAITING_WORKERS."""
        from sessions.state_v2 import SessionStateV2, _current_state

        return _current_state(self) == SessionStateV2.AWAITING_WORKERS


def _make_set_event() -> asyncio.Event:
    """Create an Event that starts in the set (running) state."""
    e = asyncio.Event()
    e.set()
    return e
