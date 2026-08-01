"""Pernix — Session-aware LLM scheduler with priority queuing."""

from __future__ import annotations

import asyncio
import heapq
import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger("pernix.llm.semaphore")

# Scheduling priority levels (lower = served first within a capacity slot)
PRIORITY_ORCHESTRATOR = 0  # session_type "normal" or "cron" — the coordinating agent
PRIORITY_WORKER = 1  # session_type "worker" — sub-agents spawned by orchestrator
PRIORITY_BACKGROUND = 2  # snooze, distill, reflect, scout, compaction — best-effort


class LLMConcurrencyError(Exception):
    """Raised when a slot cannot be acquired within the timeout."""


class LLMSessionTimeoutError(LLMConcurrencyError):
    """Raised when a session exceeds its maximum allowed LLM runtime."""


@dataclass(order=True)
class _WaitItem:
    session_created_at: float  # primary: FIFO across sessions (oldest first)
    priority: int  # secondary: 0 < 1 < 2
    seq: int  # tiebreak: insertion order
    session_id: str = field(compare=False)
    future: "asyncio.Future[None]" = field(compare=False)


class SessionAwareLLMScheduler:
    """Per-provider concurrency control with session-level FIFO and priority.

    Scheduling rules:
    - Across sessions: FIFO by session creation time — the oldest session's
      requests are served before newer sessions' when slots are contested.
    - Within a session: orchestrator (priority 0) beats workers (priority 1)
      which beat background tasks (priority 2).
    - Session timeout: once a session has held its first slot, new acquire()
      calls from that session are rejected after llm_session_timeout seconds,
      preventing any single session from monopolising resources indefinitely.
    """

    def __init__(self, max_concurrent: int, session_timeout: float = 1800.0):
        self._capacity = max_concurrent
        self._available = max_concurrent
        self._session_timeout = session_timeout
        self._heap: list[_WaitItem] = []
        self._seq = 0
        self._waiting = 0
        # session_id → monotonic timestamp when session first acquired a slot
        self._session_first_active: dict[str, float] = {}
        # session_id → effective timeout (set by extend_session_budget when the
        # session takes on a long-running orchestrator role; the wall-clock
        # cap conflates "active LLM work" with "blocked waiting on workers",
        # which is wrong for run_workflow). Absent → use the base timeout.
        self._session_timeout_override: dict[str, float] = {}

    def _effective_timeout(self, session_id: str) -> float:
        return self._session_timeout_override.get(session_id, self._session_timeout)

    async def acquire(
        self,
        session_id: str = "",
        session_created_at: float = float("inf"),
        priority: int = PRIORITY_BACKGROUND,
        timeout: float | None = None,
    ) -> None:
        """Acquire one slot.

        Blocks until a slot is available, the per-acquire timeout fires, or
        the session's total LLM time limit is exceeded.

        Args:
            session_id: Identifies the requesting session for timeout tracking
                and FIFO ordering. Empty string = background caller (no timeout).
            session_created_at: Unix timestamp of session creation, used for
                cross-session FIFO ordering. Defaults to inf so background
                callers are served last when there is contention.
            priority: PRIORITY_ORCHESTRATOR (0), PRIORITY_WORKER (1), or
                PRIORITY_BACKGROUND (2).
            timeout: Per-acquire wait cap. Defaults to session_timeout.
        """
        effective_timeout = timeout if timeout is not None else self._session_timeout
        now = time.monotonic()

        # Session-level timeout: reject new requests once runtime limit is hit.
        if session_id:
            first = self._session_first_active.get(session_id)
            cap = self._effective_timeout(session_id)
            if first is not None and (now - first) > cap:
                raise LLMSessionTimeoutError(
                    f"Session {session_id[:12]} has exceeded the " f"{cap:.0f}s LLM time limit"
                )

        # Fast path: slot is free right now.
        if self._available > 0:
            self._available -= 1
            if session_id and session_id not in self._session_first_active:
                self._session_first_active[session_id] = now
            return

        # No slot available — queue a Future; release() will resolve it.
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[None] = loop.create_future()
        item = _WaitItem(
            session_created_at=session_created_at,
            priority=priority,
            seq=self._seq,
            session_id=session_id,
            future=fut,
        )
        self._seq += 1
        heapq.heappush(self._heap, item)
        self._waiting += 1

        try:
            await asyncio.wait_for(fut, timeout=effective_timeout)
        except asyncio.TimeoutError:
            self._waiting -= 1
            # If release() resolved our future between the timeout and now,
            # the slot was granted to us — pass it straight to the next waiter.
            if not fut.cancelled() and fut.done():
                self._wake_next_or_free()
            # Our own item is still in the heap, dead. Reap here too: without
            # a release() to drive _wake_next_or_free, a run of timeouts
            # against a saturated provider would never prune.
            self._drop_dead_waiters()
            raise LLMConcurrencyError(
                f"Timed out after {effective_timeout:.0f}s waiting for LLM slot "
                f"({self._waiting} still waiting, "
                f"{self.available}/{self._capacity} free)"
            )
        except asyncio.CancelledError:
            self._waiting -= 1
            if not fut.done():
                fut.cancel()
            elif not fut.cancelled():
                # Slot was granted just before cancellation — pass it on.
                self._wake_next_or_free()
            self._drop_dead_waiters()
            raise
        else:
            self._waiting -= 1
            if session_id and session_id not in self._session_first_active:
                self._session_first_active[session_id] = time.monotonic()

    def release(self, session_id: str = "") -> None:
        """Release a held slot; grant it to the highest-priority queued waiter."""
        self._wake_next_or_free()

    def _wake_next_or_free(self) -> None:
        """Pop the best waiter from the heap and resolve its future, or free a slot."""
        while self._heap:
            item = heapq.heappop(self._heap)
            if not item.future.done():
                item.future.set_result(None)
                return
            # Future already done (cancelled by timeout or CancelledError) — skip.
        self._available += 1
        self._drop_dead_waiters()

    def _drop_dead_waiters(self) -> None:
        """Clear the heap when nothing is actually waiting on it.

        acquire() decrements _waiting on every exit path, so _waiting == 0
        means every remaining heap entry belongs to a caller that timed out
        or was cancelled. _wake_next_or_free already skips those, so this is
        not a correctness fix — but without it, repeated cancels against a
        saturated provider grow the heap without bound and each wake has to
        pop through the accumulated corpses first.
        """
        if self._waiting == 0 and self._heap:
            dropped = len(self._heap)
            self._heap.clear()
            logger.debug("Dropped %d dead waiter(s) from the scheduler heap", dropped)

    def purge_session(self, session_id: str) -> None:
        """Remove a session from timeout tracking once it is fully reaped."""
        self._session_first_active.pop(session_id, None)
        self._session_timeout_override.pop(session_id, None)

    def reset_session_budget(self, session_id: str) -> None:
        """Reset wall-clock budget tracking for a fresh user turn.

        The session-time budget is a wall-clock cap (default 1800s) measured
        from the session's FIRST LLM acquire and never reset after that.
        For interactive chat that's wrong: after 30 minutes of conversation
        the session locks itself out forever ("LLM time budget exhausted —
        turn aborted before scout") even if the user was idle most of that
        time and the model only ran for 2 minutes total. The cap should be
        a per-turn safety net, not a per-session lifetime cap.

        Called by SessionManager.prompt() at the start of a new user turn.
        Clears both _session_first_active (so the next acquire restarts
        the clock) and any extend_session_budget override (so the next turn
        starts at the base llm_session_timeout — workflow runs that need
        more headroom will re-extend on entry to run_workflow).

        Idempotent and cheap. session_id == "" is a no-op.
        """
        if not session_id:
            return
        self._session_first_active.pop(session_id, None)
        self._session_timeout_override.pop(session_id, None)

    def session_seconds_remaining(self, session_id: str) -> float:
        """Return seconds remaining in the session's LLM time budget.

        Returns float('inf') if the session has not yet acquired its first
        slot (so no budget has been started). Returns 0.0 if the budget is
        already exhausted. Honours any extension installed via
        extend_session_budget — workflow orchestrators get more rope so the
        reflect-retry guard doesn't block legitimate long-runs. Used by hooks
        to refuse a retry that has no chance of completing — without this,
        reflect-retry can fire scout (which burns its own 180s timeout) only
        to discover the budget was gone all along, wasting wall-clock time
        and producing a confusing 15ms agent-error after a 220s scout pause.
        """
        first = self._session_first_active.get(session_id)
        if first is None:
            return float("inf")
        elapsed = time.monotonic() - first
        remaining = self._effective_timeout(session_id) - elapsed
        return max(0.0, remaining)

    def extend_session_budget(self, session_id: str, additional_seconds: float) -> float:
        """Grow a session's LLM budget by `additional_seconds` on top of base.

        Used when a session takes on an orchestrator role whose wall-clock is
        dominated by waiting on subordinate sessions, not by its own LLM work
        (e.g. run_workflow blocking on workers). The base session_timeout is
        a wall-clock guard against any single session monopolising resources;
        for an orchestrator, that mental model is wrong because the workers
        are already independently capped. We grow the cap proportionally to
        the workflow's natural shape so reconciliation rounds and post-flow
        reflect have budget left.

        Semantics:
        - Idempotent: calling with a smaller extension is a no-op (we never
          shrink — once granted, the budget stays granted for the session).
        - Persists for the session's lifetime; cleared by purge_session.
        - additional_seconds < 0 is clamped to 0 (no-op).
        - session_id == "" is a no-op (background callers have no budget).

        Returns the new effective timeout (for diagnostics / logging).
        """
        if not session_id:
            return self._session_timeout
        proposed = self._session_timeout + max(0.0, additional_seconds)
        existing = self._session_timeout_override.get(session_id, self._session_timeout)
        if proposed > existing:
            self._session_timeout_override[session_id] = proposed
        return self._effective_timeout(session_id)

    def ensure_session_budget(self, session_id: str, min_remaining_seconds: float) -> float:
        """Grow the session's cap so at least `min_remaining_seconds` remain.

        extend_session_budget grants headroom relative to the BASE timeout
        (effective = base + additional), so back-to-back long-running tool
        calls in one turn compose wrongly: the second call's identical
        extension is a no-op even though the first consumed most of the
        window, and the run dies on LLMSessionTimeoutError mid-flight
        (RLM stress-test session a45fa830cef9 lost three runs this way).
        This variant is relative to the clock: it measures elapsed time and
        raises the cap just enough that `session_seconds_remaining` comes
        back >= min_remaining_seconds.

        Same guarantees as extend: never shrinks a granted cap, cleared by
        purge/reset, session_id == "" is a no-op. A session that has not
        acquired yet (clock not started) counts elapsed as 0 — the first
        acquire then starts a window of at least min_remaining_seconds.
        Returns the new effective timeout.
        """
        if not session_id:
            return self._session_timeout
        first = self._session_first_active.get(session_id)
        elapsed = 0.0 if first is None else time.monotonic() - first
        needed = elapsed + max(0.0, min_remaining_seconds)
        existing = self._session_timeout_override.get(session_id, self._session_timeout)
        if needed > existing:
            self._session_timeout_override[session_id] = needed
        return self._effective_timeout(session_id)

    @property
    def available(self) -> int:
        return self._available

    @property
    def waiting(self) -> int:
        return self._waiting

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def stats(self) -> dict:
        return {
            "available": self.available,
            "waiting": self._waiting,
            "capacity": self._capacity,
        }


# Keep the old name as an alias so any stale imports don't break at runtime.
FairLLMSemaphore = SessionAwareLLMScheduler
