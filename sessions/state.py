"""Pernix — AgentSession dataclass. The state machine itself lives in
sessions/state_v2.py; this module only carries the per-session data it
mutates."""

from __future__ import annotations

import asyncio
import itertools
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, NamedTuple

# Monotonic source of unique subprocess handles. count() is atomic under the
# GIL, so concurrent tool threads registering at once still get distinct keys.
_process_handle_counter = itertools.count()


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
    # True only for goal auto-continuation prompts: the dispatch path uses
    # this to set/clear session.goal_continuation_active per turn, so a real
    # user message queued behind a continuation never runs snooze-transparent.
    is_goal_continuation: bool = False

    @classmethod
    def coerce(cls, entry) -> "PendingMessage":
        """Accept a legacy plain tuple and normalize it.

        Defensive: keeps older callers and test fixtures that append raw
        tuples working, so consumers can use attribute access unconditionally.
        """
        if isinstance(entry, cls):
            return entry
        return cls(*entry)


@dataclass
class TurnState:
    """State whose lifetime is exactly one turn.

    Everything here used to live on AgentSession and be hand-reset in three
    separate places (prompt()'s fresh-turn path, _run_agent_safe's turn-start
    path, _resume_from_workers' direct-dispatch path). The three blocks had
    drifted out of sync — reflect_count was cleared in all three, eval_count in
    only two — which is how a stale reflect_retry_requested once leaked across
    a turn boundary and forced a spurious retry despite reflect=pass. Now a
    turn boundary is one assignment: `session.turn = TurnState()`.

    Four of these fields were not fields at all: core/gates.py, sessions/hooks.py
    and core/telos/anomaly.py monkey-patched them onto the AgentSession dataclass
    from outside its class body, so the object's real shape was invisible from
    its definition. tests/test_state_machine_invariants.py pins that shut.

    A retry (reflect-retry / eval-retry) is NOT a new turn: it re-enters the
    agent via _run_agent_retry, which deliberately does not touch this object.
    reflect_count, eval_count and tool_summary accumulate across a turn's
    attempts on purpose — that accumulation is what bounds the retry ladder and
    what reflect grades.

    Deliberately NOT here: error, termination_reason and cancel_requested.
    They stay session-scoped because the previous turn's _finalize_turn may
    still be reading them while the next turn's TurnState has already been
    installed — see the comment in SessionManager._run_agent_safe.
    """

    # --- Reflect (post-execution verification) ---
    reflect_count: int = 0  # retries used this turn
    reflect_lessons: str = ""  # lessons carried into the next attempt
    reflect_retry_requested: bool = False
    # Tools mechanically disabled for the current retry attempt (reflect's
    # retry_without_tools effector). Enforced twice: removed from the schema in
    # core/agent.py, and refused in core/tools/executor.py.
    retry_excluded_tools: set = field(default_factory=set)

    # --- Evaluation (feature QA) ---
    eval_count: int = 0  # eval retries used this turn
    eval_retry_requested: bool = False
    # The judge's per-feature feedback. Rides the same retry channel as
    # reflect_lessons into the agent's scout report (_build_retry_directive).
    eval_feedback: str = ""

    # --- Tool bookkeeping ---
    # Cumulative per-tool execution summary for reflect diagnostic recovery
    # (LogAct-inspired). Accumulated by the agent loop across every attempt of
    # the turn; read by reflect, Candor and TELOS at turn end.
    tool_summary: dict = field(default_factory=dict)

    # --- Owned by other subsystems, declared here so the shape is visible ---
    # core.gates.GateHistory — deterministic-gate fingerprints for this turn, so
    # a retry can reuse a prior failure when watch_paths are unchanged. Typed
    # Any to keep sessions/ from importing core.gates.
    gate_history: Any = None
    # sessions.hooks._maybe_candor delta-tracking: {"turn": id, "tools": {...}}
    # so a reflect-retry re-entry never double-observes the earlier attempt.
    candor_emitted: dict | None = None
    # (turn_id, verdict, failure_cause, experience) stashed by _maybe_reflect
    # for _maybe_candor, which runs after it.
    candor_reflect: tuple | None = None
    # Skill-proposal ids injected as trial hints this turn; the post-verdict
    # success bump reads them back.
    injected_trial_proposals: list = field(default_factory=list)
    # core.telos.anomaly.on_post_task per-turn dedup marker.
    telos_turn_traced: Any = None


def turn_state(session_obj) -> TurnState:
    """Read a session-like object's TurnState, tolerating objects that have none.

    The peripheral hooks (TELOS, Candor, the executor's retry-exclusion guard,
    the canary runner) accept duck-typed or partially-built session objects and
    used `getattr(session, "<field>", <default>)` for exactly that reason.
    Returning a throwaway TurnState preserves that forgiveness now that the
    fields live one level down. Read-only: a write to the throwaway is
    discarded, so callers that must persist go through `session.turn`.
    """
    ts = getattr(session_obj, "turn", None)
    return ts if isinstance(ts, TurnState) else TurnState()


@dataclass
class AgentSession:
    """In-memory state for an active session.

    Everything here outlives a single turn. Anything that does not belongs on
    `turn` (a TurnState, replaced wholesale at each turn boundary) — putting a
    turn-scoped field here means adding it to a reset block somewhere, and the
    reset blocks are exactly what drifted before TurnState existed.

    The session's state lives in `_state_v2` and is mutated exclusively by
    `sessions.state_v2.transition()`. The pre-v2 5-value `SessionState` enum
    and its `state` mirror field were deleted once the v2 machine became
    authoritative — read state via `state_v2._current_state(session)`.

    Nothing outside this module may attach a field to an instance;
    tests/test_state_machine_invariants.py enforces it.
    """

    session_id: str
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
    session_type: str = "normal"  # normal | worker | cron | snooze (dream journal) | rlm (run view)
    parent_session_id: str | None = None
    worker_ids: list = field(default_factory=list)

    # Per-session workspace root override (absolute path). None = the shared
    # global workspace (settings.workspace_dir) — the default and the normal
    # case. Set at spawn time for sessions that need filesystem isolation
    # (canary runs, isolated tasks). Runtime-only: not persisted; sessions
    # that need it are re-created with it. Plumbed to file tools per-call via
    # executor context -> paths.WORKSPACE_OVERRIDE.
    workspace_override: str | None = None

    # Live goal id for token_usage stamping (plan 3b). Resolved at turn
    # start from session_goals when goals_enabled; workers inherit the
    # parent's at spawn so a goal's budget sees fan-out spend.
    active_goal_id: int | None = None

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

    # Per-dispatch tool allow-list for scheduled (cron/heartbeat) runs. Set by
    # the scheduling extension from the job's `allowed_tools` field before the
    # prompt is dispatched, cleared in the same finally as model_override.
    # When set it is EXCLUSIVE: the schema builder intersects the active tool
    # set with it (overriding the builtin force-add and the monotonic
    # allowlist), and the executor refuses anything outside it — the same two
    # enforcement points as reflect's retry_excluded_tools. None (the normal
    # case) means no constraint. Prose bans in job prompts demonstrably do not
    # bind (field case 0ba19fdbc823: bash forbidden in the charter, called 8
    # times on the retry — because the schema still offered it).
    tool_allowlist: frozenset | None = None

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

    # Everything whose lifetime is one turn. Replaced wholesale — never
    # field-by-field — at each turn boundary. See TurnState.
    turn: TurnState = field(default_factory=TurnState)

    # True while the session's turns are driven by goal auto-continuations.
    # Makes the session snooze-transparent (audit P5); cleared on a real
    # user prompt.
    goal_continuation_active: bool = False

    # Worker watch-set: worker IDs this session is waiting on (Gap 1+2+5)
    _watched_worker_ids: set = field(default_factory=set)

    # Subprocesses tools are currently blocked on (bash, RLM children), keyed
    # by an opaque handle. A single slot used to live here, but two concurrent
    # bash calls in one session overwrote each other: the second registration
    # clobbered the first, so a dispatch timeout could no longer kill the first
    # process (its thread stayed blocked for the child's full runtime) and the
    # first `finally` to run cleared the slot out from under the second.
    # Registered and released in the tool's own finally — not at a turn
    # boundary — so entries stay session-scoped. Values are (owner, proc):
    # `owner` is the dispatch call id, which lets a dispatch timeout kill
    # exactly the call that timed out while session cancel kills all of them.
    # Typed loosely to avoid importing subprocess for a field nothing in
    # sessions/ dereferences.
    _active_processes: dict = field(default_factory=dict)

    # Monotonic-ish timestamp of the last "workers appear stalled" warning
    # emitted by orchestration.await_workers, which logs at most once a minute.
    _await_stalled_logged_at: float = 0.0

    # Deferred-reflect ticket counter (sessions/hooks.py). Bumped every time a
    # deferred grade is scheduled; the sleeping task compares the ticket it was
    # issued against this value and skips grading if it no longer matches.
    # Session-scoped on purpose — TurnState is replaced at every turn boundary,
    # and this counter's whole job is to outlive the turn it was issued for.
    _deferred_reflect_seq: int = 0

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

    def register_process(self, proc, owner: str = "") -> str:
        """Track a subprocess so cancel and dispatch-timeout can kill it.

        Returns an opaque handle to pass back to release_process(). Keyed per
        process rather than per session so concurrent tool calls — and a single
        RLM run spawning several children — never overwrite one another.
        """
        handle = f"{owner}#{next(_process_handle_counter)}"
        self._active_processes[handle] = (owner, proc)
        return handle

    def release_process(self, handle: str) -> None:
        """Stop tracking one subprocess. Idempotent."""
        self._active_processes.pop(handle, None)

    def processes_for(self, owner: str) -> list:
        """Live subprocesses registered by one dispatch call."""
        return [proc for _h, (o, proc) in list(self._active_processes.items()) if o == owner]

    def all_processes(self) -> list:
        """Every live subprocess tracked for this session."""
        return [proc for _h, (_o, proc) in list(self._active_processes.items())]

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
        payloads, frontend scripts, orchestration checks) keep working.
        Writers must go through state_v2.transition() — bare assignments
        are gone."""
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
