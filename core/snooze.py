"""Pernix — Snooze: the idle-time self-maintenance ladder.

One cycle walks a fixed priority ladder of ~20 activities (see _do_cycle for
the authoritative order). They fall into a few clusters:

- Memory hygiene — catch-up distillation of un-reviewed sessions, user-insight
  extraction, dedup, consolidation, rerouting entries to better files, tag
  enrichment, FTS5 index reconciliation, splitting bloated files, pruning
  stale entries, and the distillation coverage audit (the lens's own
  feedback loop).
- Skill learning — extracting skill requirements, mining skill co-occurrence.
- Signal synthesis — folding operational signals (and, when Candor is on, the
  candor gate) into durable memory.
- Retention sweeps — expiring post-mortems, workflow runs, RLM runs, canary
  runs, and old cron runs/sessions.
- Self-modification — refine (authoring improvements) and applying approved
  adaptive-policy edits.
- Introspection add-ons — dream and the telos slow loop.

This module owns lifecycle, the idle gate, and the ladder. The work itself
lives next to the store it touches: memory-store surgery in
``core/memory/sweeps.py``, retention pruners in ``core/retention.py``. The
activity methods below are thin delegates that supply budgets, poll for
cancellation, and fold returned counts into the cycle stats.

Add-on activities are gated by their own settings flags, so a default install
runs only the memory/skill/retention core. The ladder runs to completion
unless preempted; every activity polls for cancellation at its own boundaries.

Interruptible via cooperative cancellation. Uses background_model only.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from config import settings

logger = logging.getLogger("pernix.snooze")


# Session types snooze looks straight through (plan §5, pass-3 F3): they
# neither cancel a running cycle, nor block the idle gate, nor refresh the
# activity cooldown. Without this, a canary sweep and snooze deadlock — the
# sweep waits for idle while its own sessions keep snooze from being idle.
SNOOZE_TRANSPARENT_TYPES = frozenset({"canary"})


def snooze_transparent(session) -> bool:
    """True when a session should be invisible to the idle gate.

    Canary sessions by type, plus any session currently driven by goal
    auto-continuations (audit P5): a multi-hour autonomous goal used to
    starve the entire self-improvement ladder — adaptive apply, dream,
    telos — for its whole duration. The LLM semaphore's priority tiers
    keep snooze's background calls from contending with the goal's own.
    """
    return getattr(session, "session_type", "") in SNOOZE_TRANSPARENT_TYPES or bool(
        getattr(session, "goal_continuation_active", False)
    )


def _mutation_blocked() -> bool:
    """True while any non-canary session (including a goal-continuation
    turn) is mid-flight. Read-only/LLM review activities run fine alongside
    an autonomous goal — the semaphore's background priority handles
    contention — but activities that MUTATE shared state the live turn
    depends on (global adaptive applies, memory-store surgery) must wait
    for genuine idle: a mid-turn prompt or memory mutation changes the very
    turn the tripwire would then attribute the batch to."""
    try:
        from sessions.manager import get_manager

        return get_manager().has_active_work(strict=True)
    except Exception:
        return False


def _announce(bus, activity: str, detail: str) -> None:
    """Emit the ladder's per-activity progress event."""
    bus.emit({"type": "snooze.activity", "activity": activity, "detail": detail})


class SnoozeRunner:
    """Background idle-time self-optimization."""

    def __init__(self):
        # Generation counter replaces asyncio.Event for cancel signalling.
        # Event.clear() at cycle start could swallow a cancel that fired during
        # the idle/activity pre-checks. Counters have no such window: if cancel
        # is requested at any point before _cycle_generation is captured,
        # _is_cancelled() returns True immediately.
        self._cancel_generation: int = 0  # bumped by request_cancel()
        self._cycle_generation: int = -1  # set to _cancel_generation at cycle start
        self._running = False
        self._stats = {
            "cycles": 0,
            "cycles_skipped": 0,
            "sessions_reviewed": 0,
            "entries_saved": 0,
            "entries_deduped": 0,
            "entries_enriched": 0,
            "last_cycle": None,
        }
        self._activity_since_last_cycle: bool = True  # first cycle always runs
        self._last_cycle_time: float = 0.0
        # Per-cycle abort signal: fresh Event each cycle (no clear() races),
        # set by request_cancel so the supervisor can abort in-flight awaits.
        self._cancel_event: asyncio.Event | None = None

    def request_cancel(self) -> None:
        """Signal Snooze to stop ASAP. Called when work arrives.

        Two signals: the generation counter (polled by activities at their
        loop boundaries — see __init__ for why a counter, not an Event) and
        the per-cycle cancel event, which lets run_cycle's supervisor abort
        an IN-FLIGHT await (e.g. a minutes-long background LLM call) instead
        of waiting for the next poll point. User work preempts immediately.
        """
        if self._running:
            self._cancel_generation += 1
            evt = self._cancel_event
            if evt is not None:
                evt.set()
            logger.debug("Snooze cancel requested (gen=%d)", self._cancel_generation)

    def notify_activity(self) -> None:
        """Signal that user/cron activity occurred, so next Snooze cycle should run."""
        self._activity_since_last_cycle = True

    def get_stats(self) -> dict:
        return {**self._stats, "running": self._running}

    def _bump(self, key: str, amount: int) -> None:
        """Add to a cycle stat, creating it on first use."""
        if amount:
            self._stats[key] = self._stats.get(key, 0) + amount

    # ------------------------------------------------------------------
    # Idle detection
    # ------------------------------------------------------------------

    def _is_idle(self, ignore_cooldown: bool = False) -> bool:
        """Check if the system is truly idle (relaxed gate).

        ignore_cooldown: skip check #4 (recent-activity heuristic). Used by
        forced triggers — the cooldown guesses "user may still be around",
        but a forced cycle yields instantly if the user shows up, so the
        guess adds nothing there. Checks #1-#3 (real in-progress work)
        always apply.
        """
        from sessions.manager import get_manager

        manager = get_manager()

        # 1. No active processing (v2 state: AWAITING_USER/AWAITING_WORKERS are
        # suspended-not-running, so they count as idle for snooze purposes)
        from sessions import state_v2 as _sv2

        _idle_v2 = (
            _sv2.SessionStateV2.IDLE_READY,
            _sv2.SessionStateV2.AWAITING_USER,
            _sv2.SessionStateV2.AWAITING_WORKERS,
        )
        # Snapshot once and reuse: a tool thread can insert into _sessions
        # (spawn_worker -> create_session) while this runs on the event loop,
        # and iterating the live dict raises "dictionary changed size during
        # iteration" — which would kill the snooze cycle outright.
        # Transparent types (canary) are invisible to every check below.
        sessions = [s for s in list(manager._sessions.values()) if not snooze_transparent(s)]

        for session in sessions:
            if _sv2._current_state(session) not in _idle_v2:
                return False

        # 2. No background tasks
        for session in sessions:
            if session.has_background_tasks:
                return False

        # 3. No cron jobs executing
        try:
            from db import models as db

            running_crons = db.list_cron_runs(limit=5)
            if any(r.get("status") == "running" for r in running_crons):
                return False
        except Exception:
            pass

        # 4. Cooldown elapsed (5 min since last user activity)
        if not ignore_cooldown:
            cooldown = settings.snooze_cooldown_minutes * 60
            now = time.time()
            for session in sessions:
                if (now - session.last_activity_time) < cooldown:
                    return False

        return True

    def idle_blockers(self) -> list[str]:
        """Read-only diagnostic: why _is_idle() would refuse right now.

        Mirrors _is_idle's checks without short-circuiting so the admin
        trigger can report every blocker. Never raises.
        """
        blockers: list[str] = []
        try:
            from sessions import state_v2 as _sv2
            from sessions.manager import get_manager

            _idle_v2 = (
                _sv2.SessionStateV2.IDLE_READY,
                _sv2.SessionStateV2.AWAITING_USER,
                _sv2.SessionStateV2.AWAITING_WORKERS,
            )
            sessions = [s for s in list(get_manager()._sessions.values()) if not snooze_transparent(s)]
            cooldown = settings.snooze_cooldown_minutes * 60
            now = time.time()
            for session in sessions:
                sid = getattr(session, "session_id", "?")[:8]
                state = _sv2._current_state(session)
                if state not in _idle_v2:
                    blockers.append(f"session {sid}: state={getattr(state, 'value', state)}")
                if session.has_background_tasks:
                    blockers.append(f"session {sid}: background tasks")
                idle_for = int(now - session.last_activity_time)
                if idle_for < cooldown:
                    blockers.append(f"session {sid}: active {idle_for}s ago (cooldown {int(cooldown)}s)")
        except Exception as e:
            blockers.append(f"manager unavailable: {type(e).__name__}")
        try:
            from db import models as db

            for r in db.list_cron_runs(limit=5):
                if r.get("status") == "running":
                    blockers.append(f"cron run {r.get('job_name') or r.get('job_id')}: running")
        except Exception:
            pass
        return blockers

    def cycle_backstop_seconds(self) -> int:
        """Effective hang backstop for one cycle.

        Local (Ollama) background models get 4x headroom: slow local
        inference is free compute and user activity preempts instantly via
        the yield path, so a tight cap only starves work. Remote (paid API)
        models keep the configured cap to bound spend. Shared with
        maintenance's outer budget so the inner backstop always fires first.
        """
        base = max(settings.snooze_max_cycle_seconds, 1)
        try:
            from core.llm.client import get_llm_client

            model = settings.background_model or settings.llm_model
            if model and get_llm_client().resolve_provider(model) == "ollama":
                return base * 4
        except Exception:
            pass
        return base

    def _is_cancelled(self) -> bool:
        return self._cancel_generation != self._cycle_generation

    def _llm_available(self) -> bool:
        """Check if LLM semaphore is fully available (no contention)."""
        try:
            from core.llm.client import _get_semaphore_stats

            stats = _get_semaphore_stats()
            return stats["available"] == stats["capacity"]
        except Exception:
            return False

    def _llm_ready(self) -> bool:
        """An uncontended semaphore AND a model to spend it on."""
        return self._llm_available() and bool(settings.background_model or settings.llm_model)

    # ------------------------------------------------------------------
    # Main cycle
    # ------------------------------------------------------------------

    # Minimum interval between snooze cycles when nothing has changed.
    # Active-work gate below handles the "don't step on a running session"
    # concern; this cadence just keeps snooze from burning cycles on an
    # idle-but-unchanged system.
    _MIN_CYCLE_INTERVAL_SEC = 900  # 15 min

    async def run_cycle(self, force: bool = False) -> str:
        """Run one Snooze cycle. Called by maintenance heartbeat.

        force=True (the localhost admin trigger) skips the cadence check and
        the recent-activity cooldown — but never the real gates: sessions
        actively processing, background tasks, or a running cron still
        refuse the cycle, and user activity arriving mid-cycle preempts it
        via the yield path. Returns a reason string; existing callers
        ignore it.
        """
        if not settings.snooze_enabled:
            return "disabled"

        # Explicit active-work gate. Do NOT clear _activity_since_last_cycle
        # here — we want snooze to run at the next opportunity once things
        # quiesce, carrying forward the pending-work flag.
        try:
            from sessions.manager import get_manager

            if get_manager().has_active_work():
                self._stats["cycles_skipped"] += 1
                logger.debug("Snooze: skipping (active session or worker)")
                return "skipped_active"
        except Exception:
            # Don't let a manager import failure kill snooze.
            pass

        # Forced triggers relax only the cooldown heuristic; the heartbeat
        # path keeps the bare call (and its stricter gate).
        idle_ok = self._is_idle(ignore_cooldown=True) if force else self._is_idle()
        if not idle_ok:
            return "skipped_idle"

        # Skip if no activity since last cycle and last run was recent.
        if not force and not self._activity_since_last_cycle:
            if self._last_cycle_time and (time.time() - self._last_cycle_time < self._MIN_CYCLE_INTERVAL_SEC):
                self._stats["cycles_skipped"] += 1
                logger.debug("Snooze: no activity since last cycle, skipping")
                return "skipped_cadence"

        # Capture current cancel generation. Any request_cancel() that fired
        # before this point will make _is_cancelled() return True immediately.
        self._cycle_generation = self._cancel_generation
        self._cancel_event = asyncio.Event()
        self._running = True
        logger.info("Snooze cycle starting")

        from core.events import get_event_bus

        bus = get_event_bus()
        _start = time.time()
        bus.emit({"type": "snooze.start", "activity": "cycle"})

        # The cycle runs until the ladder COMPLETES. Two things end it early:
        # user activity (request_cancel sets the cancel event; the in-flight
        # await is aborted so user work preempts immediately, and interrupted
        # activities resume next cycle via their watermarks) and the
        # snooze_max_cycle_seconds hang backstop — runaway protection, not a
        # scheduler.
        outcome = "ran"
        backstop = self.cycle_backstop_seconds()
        cycle_task = asyncio.create_task(self._do_cycle())
        waiter = asyncio.create_task(self._cancel_event.wait())
        try:
            try:
                done, _pending = await asyncio.wait(
                    {cycle_task, waiter},
                    timeout=backstop,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except asyncio.CancelledError:
                # Shutdown (maint.stop() cancels us): abort the cycle and
                # re-raise. Swallowing this meant the maintenance tick
                # carried on into WAL checkpoint/vacuum while shutdown
                # waited. The finally below still runs, bookkeeping intact.
                cycle_task.cancel()
                with contextlib.suppress(BaseException):
                    await cycle_task
                logger.debug("Snooze cycle cancelled")
                raise
            if cycle_task in done:
                exc = cycle_task.exception()
                if exc is not None:
                    outcome = "error"
                    logger.error("Snooze cycle error: %s", exc, exc_info=exc)
            else:
                yielded = self._cancel_event.is_set()
                cycle_task.cancel()
                with contextlib.suppress(BaseException):
                    await cycle_task
                if yielded:
                    outcome = "yielded"
                    logger.info(
                        "Snooze cycle yielded to user activity after %.0fs — will resume next cycle",
                        time.time() - _start,
                    )
                else:
                    outcome = "backstop"
                    logger.warning(
                        "Snooze cycle hit the %ds hang backstop — aborted in-flight activity",
                        backstop,
                    )
        finally:
            waiter.cancel()
            self._cancel_event = None
            self._running = False
            self._stats["cycles"] += 1
            self._stats["last_cycle"] = datetime.now(timezone.utc).isoformat()
            self._activity_since_last_cycle = False
            self._last_cycle_time = time.time()
            duration_ms = int((time.time() - _start) * 1000)
            bus.emit({"type": "snooze.done", "duration_ms": duration_ms, "outcome": outcome, "stats": {**self._stats}})
            logger.info("Snooze cycle complete (outcome=%s, stats: %s)", outcome, self._stats)
        return outcome

    async def _do_cycle(self) -> None:
        """Execute activities in priority order."""
        from core.events import get_event_bus

        bus = get_event_bus()
        did_llm = False
        # Separate budget for file-org activities (split, stale prune) so that
        # content-creation LLM calls (distill, insights) don't starve them.
        did_maintenance_llm = False

        # Activity 1: Catch-up distillation (max 1 LLM call)
        if not self._is_cancelled() and not did_llm and self._llm_ready():
            _announce(bus, "distill", "Catching up on un-reviewed sessions")
            did_llm = await self._catchup_distill()

        # Activity 2: User insight extraction (LLM, if distill didn't use it)
        if not self._is_cancelled() and not did_llm and self._llm_ready():
            _announce(bus, "user_insights", "Extracting user profile insights from conversations")
            did_llm = await self._extract_user_insights()

        # Activity 2c: Skill requirements install (no LLM). Hash-triggered:
        # a skill whose requirements.txt changed since the last successful
        # install gets its packages installed into the workspace venv, then
        # the registry rescans so the 1d health flag clears. Bounded to one
        # skill per cycle to keep cycles short.
        if not self._is_cancelled():
            _announce(bus, "skill_requirements", "Installing changed skill requirements into workspace venv")
            await self._install_skill_requirements()

        # Activity 3: Dedup sweep (no LLM)
        if not self._is_cancelled():
            _announce(bus, "dedup", "Checking for duplicate memory entries")
            await self._dedup_sweep()

        # Activity 3b: Cross-file consolidation (trivial=no LLM, ambiguous=LLM)
        if not self._is_cancelled():
            _announce(bus, "consolidate", "Consolidating overlapping memory files")
            did_llm = await self._consolidate_files(did_llm) or did_llm

        # Activity 3c: Entry re-routing (fix entries in the wrong file)
        if not self._is_cancelled():
            _announce(bus, "reroute", "Re-routing misplaced memory entries to correct files")
            did_llm = await self._reroute_misplaced_entries(did_llm) or did_llm

        # Activity 4: Tag enrichment (no LLM)
        if not self._is_cancelled():
            _announce(bus, "enrich_tags", "Enriching memory entry tags")
            await self._enrich_tags()

        # Activity 5: Index reconciliation (no LLM)
        if not self._is_cancelled():
            _announce(bus, "reconcile", "Reconciling memory index")
            await self._reconcile_index()

        # Activity 6: File splitting (LLM, maintenance budget — independent of did_llm)
        if not self._is_cancelled() and not did_maintenance_llm and self._llm_ready():
            _announce(bus, "split", "Splitting large memory files")
            did_maintenance_llm = await self._split_file()

        # Activity 7: Cron cleanup (no LLM)
        if not self._is_cancelled():
            await self._cleanup_cron(bus)

        # Activity 8: Staleness pruning (LLM, maintenance budget — runs even when distill used LLM)
        if not self._is_cancelled() and not did_maintenance_llm and self._llm_ready():
            _announce(bus, "stale_prune", "Pruning stale low-recall memory entries")
            await self._prune_stale_entries()

        # Activity 9: Skill cooccurrence update (no LLM)
        if not self._is_cancelled():
            _announce(bus, "skill_cooccurrence", "Updating skill co-occurrence map from memory")
            await self._update_skill_cooccurrence()

        # Activity 10: Synthesize post-mortems into tool/skill performance counters (no LLM)
        if not self._is_cancelled():
            _announce(bus, "synthesize_signals", "Synthesizing post-mortems into scout signals")
            await self._synthesize_signals()

        # Activity 11: Post-mortem TTL cleanup (no LLM)
        if not self._is_cancelled():
            _announce(bus, "cleanup_post_mortems", "Pruning old synthesized post-mortems")
            await self._cleanup_post_mortems()

        # Activity 12: Workflow run directory cleanup (no LLM)
        if not self._is_cancelled():
            _announce(bus, "cleanup_workflow_runs", "Pruning old workflow run directories")
            await self._cleanup_workflow_runs()

        # Activity 12a: RLM run directory cleanup (no LLM). Age-based only —
        # runs are one-shot transient work products ("extract and discard"),
        # so unlike workflows there is no keep-N-per-name window.
        if not self._is_cancelled():
            _announce(bus, "cleanup_rlm_runs", "Pruning old RLM run directories")
            await self._cleanup_rlm_runs()

        # Activity 12b: Candor operational-memory maintenance (no LLM).
        # Runs the admission gate (the expensive sweep — this is its only
        # home), drains the pending observation buffer, and checkpoints. All
        # store work happens on the bridge's dedicated executor thread;
        # cancellation is polled between phases and drain chunks.
        if not self._is_cancelled() and settings.candor_enabled:
            _announce(bus, "candor_gate", "Sweeping Candor operational memory (gate + buffer drain)")
            await self._candor_maintenance()

        # Activity 12c: Canary retention cleanup (no LLM). Prunes canary_runs
        # rows and the canary sessions behind them past canary_retention_days.
        # NEVER dispatches sweeps — post-batch sweeps are enqueued through the
        # scheduler for the next idle window (plan §5: inline dispatch from a
        # snooze activity would cancel the cycle that produced the batch).
        if not self._is_cancelled() and settings.canary_enabled:
            _announce(bus, "cleanup_canary_runs", "Pruning old canary runs and sessions")
            await self._cleanup_canary_runs()

        # Activity 12d: Canary suite auto-maintenance (no LLM). Promotes
        # vetted auto-admitted canaries, tags flapping ones flaky, retires
        # long-green ones to quarantine, purges the quarantine. The Goodhart
        # lock lives in core/canary/maintain.py: a failing canary is never
        # auto-mutated.
        if not self._is_cancelled() and settings.canary_enabled and settings.canary_auto_maintain:
            _announce(bus, "canary_maintain", "Maintaining the canary suite (promote/flaky/retire/purge)")
            await self._canary_maintain()

        # Activity 13: Refine pass — the single session-improvement rung.
        # Runs independent of did_llm: refine has its own budget, bounded to
        # one session per cycle, watermarked refined:{sid}.
        if not self._is_cancelled() and self._llm_ready():
            _announce(bus, "refine", "Crystallizing skill/memory updates from an idle session")
            await self._refine_one_session()

        # Activity 14: Dream step — idle-time introspection (core/dream).
        # Runs independent of did_llm like refine: one bounded unit per cycle
        # (a validation OR a hypothesis-generation call), watermarked in
        # snooze_state under dream_* keys. Gated on dream_enabled — fully
        # absent from the cycle when off.
        if not self._is_cancelled() and settings.dream_enabled and self._llm_ready():
            detail = "Dreaming: examining memory and outcome evidence for hypotheses"
            _announce(bus, "dream", detail)
            # Journal the step marker directly instead of via the bus
            # listener: an instant verdict inside the step would win the
            # race against the listener's queue and print above its own
            # "→" header.
            from core.dream.journal import append as journal_append

            await journal_append(f"→ {detail}")
            await self._dream_step()

        # Activity 14b: Distillation coverage audit — the feedback loop on
        # the memory lens itself (core/memory/audit.py). One sampled session
        # per run under a daily budget; misses land in Candor and are written
        # back to memory. Own budget like refine/dream — independent of
        # did_llm.
        if not self._is_cancelled() and settings.distill_audit_enabled and self._llm_ready():
            _announce(bus, "distill_audit", "Auditing distillation coverage against a raw transcript")
            await self._distill_audit()

        # Activity 16 (runs before 15's store work so its LLM call sits in
        # the same cancellation window as dream's): TELOS fast loop — one
        # bounded unit per cycle: evaluate a gated hypothesis OR generate
        # SOUP hypotheses for the next scheduled question (85% goal-linked /
        # 15% serendipity). Gated on telos_enabled — fully absent when off.
        if not self._is_cancelled() and settings.telos_enabled and self._llm_ready():
            _announce(bus, "telos", "TELOS: generating or evaluating hypotheses for open questions")
            await self._telos_step()

        # Activity 15: Adaptive layer — drain pending auto-applies, enqueue
        # post-batch canary sweeps, evaluate the tripwire (plan §6c). Runs
        # inside the idle window by construction, which is what makes
        # global-scope applies safe (no session's cached prefix is mid-turn).
        # No LLM — pure store work.
        if not self._is_cancelled() and settings.adaptive_enabled:
            _announce(bus, "adaptive_apply", "Applying pending adaptive edits and evaluating the tripwire")
            await self._adaptive_step()

    # ------------------------------------------------------------------
    # Activity 1: Catch-up distillation
    # ------------------------------------------------------------------

    async def _catchup_distill(self) -> bool:
        """Review one un-distilled session. Returns True if work was done."""
        from db import models as db

        sessions = db.get_unreviewed_sessions(
            min_age_minutes=settings.snooze_cooldown_minutes * 2,
            limit=1,
        )
        if not sessions:
            return False

        session = sessions[0]
        sid = session["id"]

        # Skip distillation if user already saved entries manually in this session.
        # Manual saves are tracked by the remember() tool via snooze_state.
        manual_save = db.get_snooze_state(f"manual_save:{sid}")
        if manual_save:
            logger.info("Snooze: session %s has manual saves, skipping distillation", sid)
            db.mark_session_reviewed(sid)
            return False

        logger.info("Snooze: distilling session %s (%s)", sid, session.get("title", "?"))

        try:
            messages = db.get_messages(sid)
            # Filter to substantive messages
            substantive = [m for m in messages if m["role"] in ("user", "assistant") and m.get("content")]
            if len(substantive) < 4:
                db.mark_session_reviewed(sid)
                return False

            from core.memory.distill import distill_session

            await distill_session(
                session_id=sid,
                title=session.get("title", "Untitled"),
                messages=messages,
                session_type=session.get("session_type", "normal"),
            )
            db.mark_session_reviewed(sid)
            self._stats["sessions_reviewed"] += 1
            logger.info("Snooze: distilled session %s", sid)
            return True

        except Exception as e:
            logger.warning("Snooze: distillation failed for %s: %s", sid, e)
            # Still mark as reviewed to avoid retrying a broken session forever
            db.mark_session_reviewed(sid)
            return False

    # ------------------------------------------------------------------
    # Activity 2: User insight extraction
    # ------------------------------------------------------------------

    # Transcript budget for the profiling call. Sized for the modern context
    # window, not the weak-model era this was first tuned for — a 15k cap
    # starved the profiler of exactly the late-session turns where stated
    # preferences show up (audit P2).
    _TRANSCRIPT_VIEW_CHARS = 40000

    _USER_INSIGHTS_PROMPT = """You are a user profiling agent. Analyze this conversation and extract facts about the USER — not about the technical work itself (that's handled separately).

Extract ONLY information that would help a future AI assistant understand and serve this user better. Output a JSON array of entries, each:
{
  "tags": "comma,separated,keywords",
  "weight": "high|normal",
  "content": "Self-contained fact about the user"
}

Categories to look for (extract only what's actually present — do NOT infer or guess):

IDENTITY & DEMOGRAPHICS: name, age, birthday, gender, location, timezone, language, occupation, role, employer, team
EXPERTISE & KNOWLEDGE: technical skills, domains of experience, experience level, certifications, education background
PREFERENCES & STYLE: preferred tools, languages, frameworks, communication style, verbosity preference, work habits
LIKES & DISLIKES: stated preferences, things they explicitly enjoy or avoid, aesthetic tastes, workflow preferences
GOALS & CONTEXT: what they're working toward, project context, deadlines, constraints, team dynamics
BEHAVIORAL PATTERNS: what approaches worked well for them, what frustrated them, how they like to collaborate with AI, feedback they gave about responses

RULES:
- Only extract what the user explicitly stated or clearly demonstrated — never guess demographics
- Each fact must be self-contained (understandable without the conversation)
- Use "weight": "high" for core identity facts (name, role, location) and strong stated preferences
- Use "weight": "normal" for contextual observations and softer patterns
- If the conversation reveals nothing about the user personally, respond with just: SKIP
- Do NOT extract technical findings, code decisions, or project architecture — those are handled by distillation
- Be concise: each content field should be 1-2 sentences max

Output valid JSON only. No markdown fences. /no_think"""

    async def _extract_user_insights(self) -> bool:
        """Extract user profile facts from one already-distilled session.

        Returns True if LLM was used.
        """
        from db import models as db

        # get_unreviewed_sessions returns sessions where snooze_reviewed_at IS
        # NULL. We need sessions that HAVE been distilled — query directly.
        with db.connect_sessions() as conn:
            cutoff = (datetime.now(timezone.utc) - timedelta(minutes=settings.snooze_cooldown_minutes * 2)).isoformat()
            rows = conn.execute(
                f"""SELECT s.* FROM sessions s
                   WHERE s.snooze_reviewed_at IS NOT NULL
                     AND {db.SQL_SESSION_IS_IDLE}
                     AND s.updated_at < ?
                     AND s.session_type != 'worker'
                     AND (
                         SELECT COUNT(*) FROM messages m
                         WHERE m.session_id = s.id
                           AND m.role = 'user'
                           AND m.content != ''
                     ) >= 2
                   ORDER BY s.updated_at ASC
                   LIMIT 5""",
                (cutoff,),
            ).fetchall()

        # Filter out sessions already profiled (tracked via snooze_state)
        candidates = [dict(row) for row in rows if not db.get_snooze_state(f"profiled:{row['id']}")]
        if not candidates:
            return False

        session = candidates[0]
        sid = session["id"]
        logger.info("Snooze: extracting user insights from session %s (%s)", sid, session.get("title", "?"))

        try:
            messages = db.get_messages(sid)
            # Build transcript focused on user messages (with assistant context)
            transcript_lines = [f"Session: {session.get('title', 'Untitled')}"]
            for msg in messages:
                role = msg.get("role", "")
                content = msg.get("content", "")
                if role == "user" and content:
                    transcript_lines.append(f"[USER] {content[:1000]}")
                elif role == "assistant" and content:
                    transcript_lines.append(f"[ASSISTANT] {content[:400]}")
            transcript = "\n".join(transcript_lines)

            if len(transcript) < 200:
                db.set_snooze_state(f"profiled:{sid}", str(time.time()))
                return False

            from core.llm.client import get_llm_client

            client = get_llm_client()
            model = settings.background_model or settings.llm_model

            try:
                response = await client.chat(
                    messages=[
                        {"role": "system", "content": self._USER_INSIGHTS_PROMPT},
                        {"role": "user", "content": transcript[: self._TRANSCRIPT_VIEW_CHARS]},
                    ],
                    model=model,
                    max_tokens=1500,
                )
            except Exception as e:
                logger.warning("Snooze: user insight LLM call failed for %s: %s", sid, e)
                db.set_snooze_state(f"profiled:{sid}", str(time.time()))
                return True
            text = response.content.strip()

            db.set_snooze_state(f"profiled:{sid}", str(time.time()))

            if text.upper() == "SKIP":
                logger.debug("Snooze: no user insights for session %s", sid)
                return True

            # Parse entries
            entries = self._parse_insight_entries(text)
            if not entries:
                return True

            # Save with dedup
            from core.memory.store import get_memory_store

            store = get_memory_store()
            if not store:
                return True

            saved = 0
            for entry in entries:
                content = entry.get("content", "")
                if not content:
                    continue

                # Multi-signal dedup against existing memories
                if store.is_duplicate(content):
                    continue

                tags = entry.get("tags", "")
                tags = f"user,profile,{tags}" if tags else "user,profile"
                tags += f",{time.strftime('%Y-%m-%d')}"

                await asyncio.to_thread(
                    store.add_entry,
                    content=content,
                    file_name="user.profile",
                    entry_type="profile",
                    tags=tags,
                    weight=entry.get("weight", "normal"),
                    source="snooze",
                )
                saved += 1
                await asyncio.sleep(0.1)

            if saved:
                self._stats["entries_saved"] += saved
                logger.info("Snooze: extracted %d user insight(s) from session %s", saved, sid)
            return True

        except Exception as e:
            logger.warning("Snooze: user insight extraction failed for %s: %s", sid, e)
            db.set_snooze_state(f"profiled:{sid}", str(time.time()))
            return False

    @staticmethod
    def _parse_insight_entries(text: str) -> list[dict]:
        """Parse JSON array from LLM response."""
        import json

        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:])
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return [data]
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            logger.debug("Failed to parse user insights JSON: %s", text[:200])
        return []

    # ------------------------------------------------------------------
    # Activity 13: Whole-session refine (tail-end of snooze cycle)
    # ------------------------------------------------------------------

    async def _refine_one_session(self) -> bool:
        """Run a refine pass on one idle session.

        Selects via :func:`db.get_unrefined_sessions` (10-min idle floor,
        watermark ``refined:{sid}``). Stamps the watermark unconditionally
        after the call so a session that produced nothing actionable, or
        one whose LLM call failed, is never retried — matches the
        mark-on-failure pattern used by ``_catchup_distill``.

        Returns True if the LLM was invoked (so stats reflect cycle work),
        False otherwise. Snooze does not gate any subsequent activity on
        this return, but the bool keeps the call site uniform.
        """
        from db import models as db

        sessions = db.get_unrefined_sessions(
            min_idle_minutes=settings.snooze_cooldown_minutes * 2,
            limit=1,
        )
        if not sessions:
            return False

        session = sessions[0]
        sid = session["id"]

        if self._is_cancelled():
            return False

        try:
            from core.refine import run_for_session

            stats = await run_for_session(sid)
            self._bump("refine_proposals_saved", stats.get("proposals_saved", 0))
            self._bump("refine_lessons_saved", stats.get("lessons_saved", 0))
            if stats.get("nothing_actionable"):
                self._bump("refine_nothing_actionable", 1)
            llm_used = stats.get("skipped_reason") not in (
                "session_not_found",
                "worker_session",
                "no_messages",
                "insufficient_exchange",
                "no_model_configured",
            )
            db.set_snooze_state(f"refined:{sid}", datetime.now(timezone.utc).isoformat())
            return bool(llm_used)
        except Exception as e:
            logger.warning("Snooze: refine pass failed for %s: %s", sid, e)
            db.set_snooze_state(f"refined:{sid}", datetime.now(timezone.utc).isoformat())
            return False

    # ------------------------------------------------------------------
    # Activity 2c: Skill requirements install (adaptation plan 1d)
    # ------------------------------------------------------------------

    async def _install_skill_requirements(self) -> None:
        """Install one changed skill's requirements.txt into the workspace venv.

        Hash-watermarked via snooze_state['skill_reqs_hash:{name}'] so each
        requirements.txt is installed once per content change. Network work
        never happens in scan() (it runs in the startup path); it happens
        here, off the user's critical path. One skill per cycle.
        """
        import hashlib
        import subprocess
        from pathlib import Path as _Path

        from core.skills.registry import get_skill_registry
        from db import models as db

        registry = get_skill_registry()
        venv_python = _Path(settings.workspace_venv_python)
        if not venv_python.exists():
            # bash creates the venv lazily on first use; install next cycle.
            return

        for skill in registry.all_skills():
            if self._is_cancelled():
                return
            req = skill.path / "requirements.txt"
            if not req.exists():
                continue
            try:
                content = req.read_bytes()
            except OSError:
                continue
            digest = hashlib.sha256(content).hexdigest()
            if db.get_snooze_state(f"skill_reqs_hash:{skill.name}") == digest:
                continue

            logger.info("Snooze: installing requirements for skill '%s'", skill.name)
            try:
                proc = await asyncio.to_thread(
                    subprocess.run,
                    [str(venv_python), "-m", "pip", "install", "--quiet", "-r", str(req)],
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
            except Exception as e:
                logger.warning("Snooze: pip install failed for skill '%s': %s", skill.name, e)
                return
            if proc.returncode != 0:
                tail = (proc.stderr or proc.stdout or "").strip()[-400:]
                logger.warning("Snooze: pip install failed for skill '%s': %s", skill.name, tail)
                # No watermark on failure — retried next cycle (a transient
                # network error shouldn't permanently skip the install).
                return
            db.set_snooze_state(f"skill_reqs_hash:{skill.name}", digest)
            # Rescan so the health flag from 1d's requirements check clears.
            try:
                registry.rescan(_Path(settings.skills_dir))
            except Exception as e:
                logger.warning("Snooze: post-install rescan failed: %s", e)
            return  # one install per cycle

    # ------------------------------------------------------------------
    # Activities 3-8: memory-store sweeps (core/memory/sweeps.py)
    # ------------------------------------------------------------------

    @staticmethod
    def _store():
        from core.memory.store import get_memory_store

        return get_memory_store()

    async def _dedup_sweep(self) -> None:
        """Activity 3 — near-duplicate archival on one due memory file."""
        if _mutation_blocked():
            return
        from core.memory import sweeps
        from db import models as db

        deduped = await sweeps.dedup_sweep(
            self._store(), db, self._is_cancelled, interval_days=settings.snooze_dedup_interval_days
        )
        self._stats["entries_deduped"] += deduped

    async def _consolidate_files(self, did_llm_already: bool) -> bool:
        """Activity 3b — merge one overlapping cluster. True if LLM was used."""
        if _mutation_blocked():
            return did_llm_already
        from core.memory import sweeps
        from db import models as db

        used_llm, merged = await sweeps.consolidate_files(
            self._store(),
            db,
            self._is_cancelled,
            did_llm_already=did_llm_already,
            llm_ready=self._llm_ready,
            interval_hours=settings.snooze_consolidation_interval_hours,
        )
        self._bump("files_consolidated", merged)
        return used_llm

    async def _reroute_misplaced_entries(self, did_llm_already: bool) -> bool:
        """Activity 3c — move misfiled entries. True if LLM was used."""
        if _mutation_blocked():
            return did_llm_already
        from core.memory import sweeps
        from db import models as db

        used_llm, rerouted = await sweeps.reroute_misplaced_entries(
            self._store(),
            db,
            self._is_cancelled,
            did_llm_already=did_llm_already,
            llm_ready=self._llm_ready,
            interval_hours=settings.snooze_consolidation_interval_hours,
        )
        self._bump("entries_rerouted", rerouted)
        return used_llm

    async def _enrich_tags(self) -> None:
        """Activity 4 — heuristic tags for sparsely-tagged entries."""
        from core.memory import sweeps

        self._stats["entries_enriched"] += await sweeps.enrich_tags(self._store(), self._is_cancelled)

    async def _reconcile_index(self) -> None:
        """Activity 5 — FTS5 drift check plus the every-cycle embedding sweep."""
        from core.memory import sweeps
        from db import models as db

        await sweeps.reconcile_index(self._store(), db, self._is_cancelled)

    async def _split_file(self) -> bool:
        """Activity 6 — split a bloated file. True if the LLM was called."""
        if _mutation_blocked():
            return False
        from core.memory import sweeps

        used_llm, moved = await sweeps.split_file(self._store(), self._is_cancelled)
        self._bump("entries_split", moved)
        return used_llm

    async def _prune_stale_entries(self) -> None:
        """Activity 8 — archive low-recall entries past the LLM gatekeeper."""
        if _mutation_blocked():
            return
        from core.memory import sweeps
        from db import models as db

        pruned = await sweeps.prune_stale_entries(
            self._store(), db, self._is_cancelled, interval_days=settings.snooze_dedup_interval_days
        )
        self._bump("entries_pruned", pruned)

    # ------------------------------------------------------------------
    # Activities 7, 11, 12, 12a, 12c: retention sweeps (core/retention.py)
    # ------------------------------------------------------------------

    async def _cleanup_cron(self, bus=None) -> None:
        """Activity 7 — prune cron runs, cron sessions, state_log (6h interval)."""
        from core import retention
        from db import models as _db

        try:
            last = _db.get_snooze_state("last_cron_cleanup")
            if last and (time.time() - float(last)) < 6 * 3600:
                return
        except Exception:
            pass

        if bus:
            _announce(bus, "cron_cleanup", "Pruning old cron runs and sessions")

        counts = retention.prune_cron()
        if any(counts.values()):
            logger.info(
                "Snooze cron cleanup: %d runs pruned, %d sessions pruned, %d state_log rows pruned",
                counts["runs"],
                counts["sessions"],
                counts["state_log"],
            )
            self._bump("cron_runs_pruned", counts["runs"])
            self._bump("cron_sessions_pruned", counts["sessions"])
            self._bump("state_log_rows_pruned", counts["state_log"])

        _db.set_snooze_state("last_cron_cleanup", str(time.time()))

    async def _cleanup_post_mortems(self) -> None:
        """Activity 11 — drop synthesized post-mortems past their TTL."""
        from core import retention

        self._bump("post_mortems_pruned", retention.prune_post_mortems())

    async def _cleanup_canary_runs(self) -> None:
        """Activity 12c — canary run/session retention plus the staleness nudge."""
        from core import retention

        await retention.prune_canary_runs()
        await retention.nudge_stale_canaries()

    async def _canary_maintain(self) -> None:
        """Activity 12d — one canary auto-maintenance sweep. Never raises."""
        try:
            from core.canary.maintain import run_maintenance

            stats = await asyncio.to_thread(run_maintenance, self._is_cancelled)
            for key in ("promoted", "settled_flaky", "flaky_tagged", "demoted", "purged"):
                self._bump(f"canaries_{key}", len(stats.get(key) or []))
        except Exception as e:
            logger.warning("Snooze canary maintenance failed: %s", e)

    async def _cleanup_workflow_runs(self) -> None:
        """Activity 12 — workflow run dirs beyond keep-10-per-name or 30 days."""
        from core import retention

        await retention.prune_workflow_runs()

    async def _cleanup_rlm_runs(self) -> None:
        """Activity 12a — RLM run dirs past rlm_run_retention_days."""
        from core import retention

        self._bump("rlm_runs_pruned", await retention.prune_rlm_runs())

    # ------------------------------------------------------------------
    # Activity 9: Skill cooccurrence update (no LLM)
    # ------------------------------------------------------------------

    async def _update_skill_cooccurrence(self) -> None:
        """Populate SKILL_COOCCURRENCE from skill-type memory entries and surface
        un-promoted skill clusters.

        Groups skill-type entries by shared tags, cross-references against the
        skill registry, and builds cooccurrence links.
        """
        from core.skills.registry import get_skill_registry
        from db import models as db

        store = self._store()
        if not store:
            return

        # Check if we have new skill entries since last run
        last = db.get_snooze_state("last_skill_cooccurrence")
        if last:
            try:
                last_dt = datetime.fromisoformat(last)
                if (datetime.now(timezone.utc) - last_dt).total_seconds() < 3600:  # 1 hour
                    return
            except ValueError:
                pass

        if self._is_cancelled():
            return

        # Query all skill-type entries from FTS5
        conn = store._connect()
        try:
            rows = conn.execute(
                "SELECT file_name, content, tags, epoch FROM memory_fts WHERE entry_type = 'skill'"
            ).fetchall()
        finally:
            conn.close()

        if not rows or len(rows) < 2:
            db.set_snooze_state("last_skill_cooccurrence", datetime.now(timezone.utc).isoformat())
            return

        # Parse tags and build tag-to-entries index
        tag_to_entries: dict[str, list[str]] = {}  # tag -> list of file_names

        for row in rows:
            tags_str = row["tags"] or ""
            tags = {t.strip().lower() for t in tags_str.split(",") if t.strip() and len(t.strip()) > 2}
            # Exclude date-like tags
            tags = {t for t in tags if not re.match(r"^\d{4}-\d{2}-\d{2}$", t)}
            for tag in tags:
                tag_to_entries.setdefault(tag, []).append(row["file_name"])

        if self._is_cancelled():
            return

        # Cross-reference with registered, enabled skills only.
        # Disabled skills shouldn't accumulate cooccurrence training data — when
        # re-enabled they'd come back with stale boost links from a period the
        # user had explicitly turned them off.
        registry = get_skill_registry()
        skill_tag_map: dict[str, set[str]] = {
            skill.name: {t.lower() for t in skill.tags} for skill in registry.enabled_skills()
        }

        # Build cooccurrence: if a registered skill shares 2+ tags with another
        # registered skill via the memory skill entries, they are related
        cooccurrence: dict[str, list[str]] = {}
        skill_names = list(skill_tag_map.keys())

        for i, s1 in enumerate(skill_names):
            for s2 in skill_names[i + 1 :]:
                # Find tags shared through skill-type memory entries
                if len(skill_tag_map[s1] & skill_tag_map[s2]) >= 2:
                    cooccurrence.setdefault(s1, [])
                    cooccurrence.setdefault(s2, [])
                    if s2 not in cooccurrence[s1]:
                        cooccurrence[s1].append(s2)
                    if s1 not in cooccurrence[s2]:
                        cooccurrence[s2].append(s1)

        # Also: find memory skill entries whose tags overlap with registered skills
        for skill_name, skill_tags in skill_tag_map.items():
            for tag in skill_tags:
                if tag not in tag_to_entries:
                    continue
                for other_skill, other_tags in skill_tag_map.items():
                    if other_skill != skill_name and tag in other_tags:
                        cooccurrence.setdefault(skill_name, [])
                        if other_skill not in cooccurrence[skill_name]:
                            cooccurrence[skill_name].append(other_skill)

        if cooccurrence:
            registry.update_cooccurrence(cooccurrence)
            logger.info("Snooze: updated skill cooccurrence with %d links", sum(len(v) for v in cooccurrence.values()))

        # Surface un-promoted skill clusters: tag groups with 3+ skill entries
        # but no registered skill.
        all_skill_tags = set()
        for tags in skill_tag_map.values():
            all_skill_tags.update(tags)

        tag_counts = {tag: len(set(entries)) for tag, entries in tag_to_entries.items()}
        orphan_clusters = [
            (tag, count)
            for tag, count in sorted(tag_counts.items(), key=lambda x: -x[1])
            if count >= 3 and tag not in all_skill_tags
        ]

        if orphan_clusters:
            cluster_summary = ", ".join(f"{tag}({count})" for tag, count in orphan_clusters[:5])
            logger.info("Snooze: un-promoted skill clusters detected: %s", cluster_summary)

        db.set_snooze_state("last_skill_cooccurrence", datetime.now(timezone.utc).isoformat())

    # ------------------------------------------------------------------
    # Activity 10: Post-mortem → tool/skill performance synthesis (no LLM)
    # ------------------------------------------------------------------

    async def _synthesize_signals(self) -> None:
        """Run one batch of post-mortem → tool/skill performance synthesis.

        Pure SQL + attribution rules; no LLM calls. Runs every cycle; the
        `synthesized_at` watermark makes repeat cycles cheap no-ops once
        caught up. Bounded batch size keeps a single cycle's work finite
        even if a backlog accumulates.
        """
        from core import synthesis

        try:
            stats = await asyncio.to_thread(synthesis.run, 500)
            if stats.processed:
                logger.info(
                    "Snooze synthesis: %d post-mortems → %d signal updates",
                    stats.processed,
                    stats.attributions,
                )
        except Exception as e:
            logger.warning("Snooze synthesis failed: %s", e)

    # ------------------------------------------------------------------
    # Activity 12b: Candor operational-memory maintenance
    # ------------------------------------------------------------------

    async def _candor_maintenance(self) -> None:
        """Gate sweep + pending-buffer drain for the Candor add-on.

        No watermark needed: the bridge's drain cursor is the durable state,
        and run_gate() is O(1) when no new observations exist.
        """
        try:
            from core.extensions.candor.bridge import get_candor_bridge

            stats = await get_candor_bridge().run_maintenance(self._is_cancelled)
            if stats and (stats.get("seeded") or stats.get("drained") or stats.get("checkpointed")):
                logger.info("Snooze candor maintenance: %s", stats)
        except Exception as e:
            logger.warning("Snooze candor maintenance failed: %s", e)
            return

        # Candor producer (plan 4d): calibrated reliability regressions →
        # routing_hint edits. Deduped by slug — a live hint for the same tool
        # is left alone (updates would churn the cooldown; the ledger ref lets
        # a reviewer pull the full audit chain on demand). The pass is
        # symmetric: it retires hints as well as minting them, so a recovered
        # tool releases its slot instead of wedging the per-kind cap.
        if settings.adaptive_enabled and not self._is_cancelled():
            try:
                from core.adaptive.contract import queue_producer_edits
                from core.extensions.candor.bridge import get_candor_bridge
                from db import models as db

                degraded = await get_candor_bridge().degraded_tools()
                degraded_ids = {f"tool-{d['tool']}-degraded" for d in degraded}
                mints = []
                for d in degraded:
                    entry_id = f"tool-{d['tool']}-degraded"
                    existing = db.adaptive_get_entry(entry_id)
                    # Only a LIVE hint dedupes: a retired one must be able to
                    # come back if the tool degrades again.
                    if existing and existing.get("status") == "active":
                        continue
                    mints.append(
                        {
                            "action": "create",
                            "kind": "routing_hint",
                            "scope": "global",
                            "entry_id": entry_id,
                            "title": f"tool {d['tool']} degraded",
                            "content": (
                                f"Calibrated reliability for {d['tool']} is {d['p']:.0%} over "
                                f"{d['n']} observations — prefer an alternative or verify its "
                                f"output; see why_reliability('tool_ok', '{d['tool']}')."
                            ),
                            "evidence": [f"candor:tool_ok({d['tool']})"],
                        }
                    )
                # Retirement: a hint whose tool has RECOVERED (calibrated p
                # back above threshold, so it fell out of the degraded set) is
                # stale advice AND holds a slot against the per-kind cap
                # forever. Candor deleting its own entry is same-producer, so
                # it stays low-risk (the 4b escalation is cross-producer).
                retires = []
                for row in db.adaptive_list_entries(kind="routing_hint"):
                    eid = row["id"]
                    if row.get("source") != "candor" or eid in degraded_ids:
                        continue
                    if not (eid.startswith("tool-") and eid.endswith("-degraded")):
                        continue
                    retires.append(
                        {
                            "action": "delete",
                            "kind": "routing_hint",
                            "scope": "global",
                            "entry_id": eid,
                            "baseline_version": row["version"],
                            "evidence": [f"candor:tool_ok recovered ({eid})"],
                        }
                    )
                edits = mints[:2] + retires[:2]
                if edits:
                    q = queue_producer_edits(edits, "candor", rationale="candor reliability regression")
                    if q["queued"] or q["gated"]:
                        logger.info(
                            "Candor adaptive hints queued: %d (%d retirement), gated: %d",
                            q["queued"],
                            len(retires[:2]),
                            q["gated"],
                        )
            except Exception as e:
                logger.warning("Candor adaptive producer failed: %s", e)

    # ------------------------------------------------------------------
    # Activity 15: Adaptive layer drain + tripwire (plan §6c)
    # ------------------------------------------------------------------

    async def _adaptive_step(self) -> None:
        """Drain pending auto-applies → enqueue post-batch sweeps → evaluate
        the tripwire. Each stage guarded; a failure never kills the cycle."""
        if _mutation_blocked():
            return  # global prompt/policy mutations wait for genuine idle
        try:
            from core.adaptive import drain_pending

            out = await asyncio.to_thread(drain_pending)
            # A batch whose edits were ALL refused lands terminal-'rejected'
            # and changed nothing: nothing to announce, and no state change
            # for a post-batch sweep to measure against.
            landed = [r for r in (out.get("results") or []) if r.get("applied")]
            if landed:
                edits_n = sum(len(r["applied"]) for r in landed)
                self._bump("adaptive_batches_applied", len(landed))
                from db import models as db

                db.add_notification(
                    title="Adaptive layer: edits auto-applied",
                    body=f"{edits_n} edit(s) across {len(landed)} batch(es) applied at idle — review in the Adaptive panel.",
                    urgency="normal",
                )
                # Post-batch sweeps: batch-tagged canary data for the
                # tripwire join. Enqueued through the scheduler for its own
                # idle window — NEVER dispatched inline from this activity.
                if settings.canary_enabled:
                    from core.extensions.scheduling import enqueue_post_batch_sweep

                    for r in landed:
                        enqueue_post_batch_sweep(r["batch_id"])
        except Exception as e:
            logger.warning("Adaptive drain failed: %s", e)

        if self._is_cancelled():
            return
        try:
            from core.adaptive.tripwire import evaluate_tripwire

            actions = await asyncio.to_thread(evaluate_tripwire)
            for a in actions:
                logger.info("Adaptive tripwire: %s %s (%s)", a["action"], a["batch_id"], a.get("detail", ""))
        except Exception as e:
            logger.warning("Adaptive tripwire failed: %s", e)

    # ------------------------------------------------------------------
    # Activities 14/16: Dream + TELOS steps
    # ------------------------------------------------------------------

    async def _telos_step(self) -> None:
        """One bounded TELOS fast-loop unit (core/telos). Never raises."""
        try:
            from core.telos import run_step

            result = await run_step(self._is_cancelled)
            for key in (
                "telos_hypotheses",
                "telos_gated",
                "telos_souped",
                "telos_evaluated",
                "telos_claims",
            ):
                self._bump(key, result.get(key, 0))
        except Exception as e:
            logger.warning("Snooze TELOS step failed: %s", e)

    async def _distill_audit(self) -> None:
        """Activity 14b — one distillation-coverage audit unit. Never raises."""
        try:
            from core.memory.audit import run_audit

            result = await run_audit(self._store(), self._is_cancelled)
            self._bump("distill_audit_sessions", result.get("audited", 0))
            self._bump("distill_audit_missed", result.get("missed", 0))
            self._bump("distill_audit_recovered", result.get("recovered", 0))
        except Exception as e:
            logger.warning("Snooze distill audit failed: %s", e)

    async def _dream_step(self) -> None:
        """One bounded dream unit: validate a pending hypothesis or generate
        new ones, then write the periodic report when due. Never raises."""
        try:
            from core.dream import run_step

            result = await run_step(self._is_cancelled)
            for key in ("dream_hypotheses", "dream_validated", "dream_refuted", "dream_expired", "dream_reports"):
                self._bump(key, result.get(key, 0))
        except Exception as e:
            logger.warning("Snooze dream step failed: %s", e)


# ---------------------------------------------------------------------------
# Module singleton
# ---------------------------------------------------------------------------

_runner: SnoozeRunner | None = None


def get_snooze() -> SnoozeRunner:
    global _runner
    if _runner is None:
        _runner = SnoozeRunner()
    return _runner
