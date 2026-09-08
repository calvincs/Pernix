"""Pernix — Background maintenance heartbeat with stratified duties."""

from __future__ import annotations

import asyncio
import logging
import time

from config import settings
from core.pools import get_background_executor, run_background
from db.exclusive import MaintenanceBusy

logger = logging.getLogger("pernix.maintenance")

TICK_INTERVAL = 60  # seconds

# Bound on the fast duties only (subscriber reaping, session reaping, partial
# cleanup, checkpoint, hygiene). Snooze is deliberately NOT covered by this —
# it has its own, larger budget and runs outside the tick. See _run_snooze.
# Neither is the daily tier: it used to sit inside this bound with only one
# await in it, so wait_for could neither preempt the blocking half nor bound
# it, and firing meant abandoning five duties. See _run_daily_tier.
TICK_TIMEOUT = 30  # seconds

# Headroom over settings.snooze_max_cycle_seconds. run_cycle already bounds
# itself; this outer wait only catches a cycle wedged outside its own wait_for,
# so it must never be the one that fires first.
SNOOZE_TIMEOUT_GRACE = 15  # seconds

# The daily duties, in the order they are attempted. Named individually
# because completion is tracked per duty: a health check that overruns must
# not take the vacuum and the four prunes down with it, and must not mark
# them done either.
DAILY_DUTIES = ("memory_health", "incremental_vacuum", "prune_cron", "prune_hygiene")

# Per-duty step budget. Generous — these are hour-scale-rare jobs on a thread
# of their own — but finite, so one wedged duty cannot hold the tier's
# single-flight slot shut and starve the duties queued behind it.
DAILY_DUTY_TIMEOUT = 300  # seconds

# How soon the tier tries again when it did not get through all of its
# duties. The old code stamped once a day BEFORE doing any work, so a tier
# that was cut short waited a full day — and, because the stamp is durable,
# waited it out across restarts too. An hour is often enough to recover from
# a transient stall and rare enough not to hammer a duty that keeps failing.
DAILY_RETRY_INTERVAL_S = 3600

# How long a daily duty waits on the database gate before deferring. It
# leaves itself unstamped when it does, so deferring costs an hour rather
# than a day.
DB_GATE_WAIT_S = 30.0
DB_DRAIN_TIMEOUT_S = 60.0


class MaintenanceRunner:
    """Background heartbeat that runs periodic maintenance tasks."""

    def __init__(self):
        self._task: asyncio.Task | None = None
        self._tracked_tasks: set[asyncio.Task] = set()
        self._snooze_task: asyncio.Task | None = None
        self._daily_task: asyncio.Task | None = None
        self._backup_task: asyncio.Task | None = None
        # Duty name -> when its thread started. Cleared by the FUTURE's done
        # callback, not by whoever stopped waiting on it: a duty whose thread
        # outlived its budget is still running, and starting a second one
        # would be two memory repairs on one index.
        self._daily_inflight: dict[str, float] = {}
        # Duty name -> last success, mirroring the durable snooze_state rows
        # so /api/health does not read four rows on every poll.
        self._daily_success: dict[str, float] = {}
        self._daily_errors: dict[str, str] = {}
        self._tick_count = 0
        self._last_tick_time = 0.0
        self._stats = {
            "sessions_reaped": 0,
            "subscribers_reaped": 0,
            "partials_cleaned": 0,
            "tasks_completed": 0,
        }

    @staticmethod
    def _on_task_done(task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception():
            logger.error("Maintenance heartbeat died: %s", task.exception())

    def start(self) -> None:
        """Start the maintenance heartbeat."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._heartbeat())
            self._task.add_done_callback(self._on_task_done)
            logger.info("Maintenance heartbeat started (tick=%ds)", TICK_INTERVAL)

    async def stop(self) -> None:
        """Stop the heartbeat and wait for tracked tasks."""
        # Cancel Snooze if running
        from core.snooze import get_snooze

        get_snooze().request_cancel()

        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        # The daily tier is cancelled explicitly rather than waited out: it
        # is budgeted in minutes, and shutdown is budgeted in seconds. What
        # cancellation reaches is the coroutine awaiting the duty, never the
        # thread running it — so say so, and leave the duty unstamped. The
        # next process finds it still eligible and runs it again, which is
        # safe because every duty here is idempotent.
        if self._daily_task and not self._daily_task.done():
            self._daily_task.cancel()
        if self._daily_inflight:
            logger.info(
                "Shutdown with %d daily maintenance duties still on their threads (%s): "
                "left to finish, none stamped, all eligible again on the next start",
                len(self._daily_inflight),
                ", ".join(sorted(self._daily_inflight)),
            )

        # Wait for tracked background tasks (with timeout to prevent shutdown hang)
        if self._tracked_tasks:
            logger.info("Waiting for %d background tasks", len(self._tracked_tasks))
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._tracked_tasks, return_exceptions=True),
                    timeout=3,
                )
            except asyncio.TimeoutError:
                logger.warning("Timed out waiting for background tasks, cancelling")
                for t in self._tracked_tasks:
                    if not t.done():
                        t.cancel()

    def track_task(self, task: asyncio.Task) -> None:
        """Register a background task for monitoring."""
        self._tracked_tasks.add(task)
        task.add_done_callback(self._tracked_tasks.discard)

    def get_stats(self) -> dict:
        from core.snooze import get_snooze

        return {
            **self._stats,
            "tick_count": self._tick_count,
            "last_tick_time": self._last_tick_time,
            "active_background_tasks": len(self._tracked_tasks),
            "daily": self.daily_status(),
            "snooze": get_snooze().get_stats(),
        }

    def daily_status(self) -> dict:
        """What the daily tier has and has not managed to do.

        Reported because a stranded day used to be invisible: the durable
        stamp said the tier had run, the log said only "Maintenance tick
        exceeded 30s timeout, skipping", and nothing anywhere said the
        vacuum and the five prunes had not happened. Attempted, running and
        successfully-completed are three different facts and each is here.
        """
        return {
            "running": self._daily_task is not None and not self._daily_task.done(),
            "in_flight": sorted(self._daily_inflight),
            "due": self._duties_due(),
            "last_success": {name: (self._duty_last_success(name) or None) for name in DAILY_DUTIES},
            "last_error": dict(self._daily_errors),
        }

    async def _heartbeat(self) -> None:
        """Main heartbeat loop."""
        while True:
            try:
                await asyncio.sleep(TICK_INTERVAL)
                self._tick_count += 1
                self._last_tick_time = time.time()

                try:
                    await asyncio.wait_for(self._tick(), timeout=TICK_TIMEOUT)
                except asyncio.TimeoutError:
                    logger.warning("Maintenance tick exceeded %ds timeout, skipping", TICK_TIMEOUT)

                # Snooze runs OUTSIDE the tick bound. It is budgeted by
                # settings.snooze_max_cycle_seconds, which can legitimately
                # exceed TICK_TIMEOUT; running it inside meant the tick's
                # wait_for force-cancelled every cycle partway through.
                # Snooze runs as its own task, not awaited here: a cycle can
                # legitimately last up to its backstop (900s, and ×4 for a
                # local background model). Awaiting it inline stalled the
                # whole heartbeat for that long — no subscriber reaping, no
                # CANCELLING/FINALIZING/PROCESSING unstick, no WAL checkpoint
                # — and froze _tick_count, so the hourly and daily tiers
                # drifted by however long the cycle took.
                interval = max(1, int(settings.snooze_interval_ticks))
                if self._tick_count % interval == 0:
                    if self._snooze_task is None or self._snooze_task.done():
                        self._snooze_task = asyncio.create_task(self._run_snooze())
                        self.track_task(self._snooze_task)
                    else:
                        logger.debug("Snooze cycle still running — skipping this slot")

                # The daily tier runs OUTSIDE the tick bound too, and for a
                # sharper version of the same reason. It used to sit inside
                # _tick with exactly one await in it — the memory health
                # check — so wait_for could only ever fire there, and firing
                # abandoned the incremental vacuum and five prunes that came
                # after it. Worse, the durable "last daily run" stamp had
                # already been written before any of the work started, so the
                # abandoned duties were suppressed for a full day and across
                # restarts, with nothing in the log saying they had not run.
                # As its own single-flight task it is bounded per duty, and a
                # duty that does not finish simply stays due.
                if self._daily_task is None or self._daily_task.done():
                    if self._daily_tier_due():
                        self._daily_task = asyncio.create_task(self._run_daily_tier())
                        self.track_task(self._daily_task)
                else:
                    logger.debug("Daily maintenance still running — skipping this slot")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Maintenance tick error: %s", e, exc_info=True)

    async def _run_snooze(self) -> None:
        """Run one Snooze cycle under its own budget.

        Kept out of _tick() because _tick is bounded by TICK_TIMEOUT while
        Snooze is budgeted by settings.snooze_max_cycle_seconds. When the
        cycle ran inside the tick, the tick's wait_for cancelled it at
        TICK_TIMEOUT regardless of the configured budget — cutting memory
        maintenance mid-write. The bound here is strictly larger than the
        cycle's own so run_cycle's internal wait_for is what actually fires.
        """
        from core.snooze import get_snooze

        snooze = get_snooze()
        # Shared backstop computation (local models get more headroom) so
        # the cycle's own supervisor always fires before this outer bound.
        # getattr: tests substitute minimal snooze stand-ins.
        backstop_fn = getattr(snooze, "cycle_backstop_seconds", None)
        base = backstop_fn() if backstop_fn else max(settings.snooze_max_cycle_seconds, 1)
        budget = base + SNOOZE_TIMEOUT_GRACE
        try:
            await asyncio.wait_for(snooze.run_cycle(), timeout=budget)
        except asyncio.TimeoutError:
            logger.warning("Snooze cycle exceeded its outer %ds bound", budget)
        except asyncio.CancelledError:
            raise  # shutdown — let the heartbeat's handler stop the loop
        except Exception as e:
            logger.error("Snooze cycle error: %s", e, exc_info=True)

    # The ATTEMPT stamp. It used to be the only stamp there was, written
    # before any work ran, which is what made an interrupted tier look
    # identical to a finished one. It now throttles retries and nothing else;
    # whether a duty is DONE is a separate row per duty.
    _DAILY_TIER_KEY = "maintenance_last_daily_at"
    _DAILY_TIER_INTERVAL_S = 24 * 3600

    @staticmethod
    def _duty_key(name: str) -> str:
        return f"maintenance_daily_ok:{name}"

    def _duty_last_success(self, name: str) -> float:
        """When this duty last COMPLETED, from the durable row, cached."""
        cached = self._daily_success.get(name)
        if cached is not None:
            return cached
        from db import models as db

        try:
            value = float(db.get_snooze_state(self._duty_key(name)) or 0)
        except Exception:
            return 0.0
        self._daily_success[name] = value
        return value

    def _stamp_duty(self, name: str) -> None:
        """Record a duty as done — after it is done, and only for itself."""
        from db import models as db

        now = time.time()
        self._daily_success[name] = now
        try:
            db.set_snooze_state(self._duty_key(name), str(now))
        except Exception as e:
            logger.warning("Could not stamp daily maintenance duty %s: %s", name, e)

    def _duties_due(self) -> list[str]:
        """The duties whose last SUCCESS is older than a day (or never)."""
        now = time.time()
        return [name for name in DAILY_DUTIES if now - self._duty_last_success(name) >= self._DAILY_TIER_INTERVAL_S]

    def _daily_tier_due(self) -> bool:
        """True when there is daily work outstanding and it is time to try.

        Two questions, deliberately separate. Is anything outstanding? — per
        duty, from its own completion row, so unfinished work stays eligible
        without anything else being re-run. Is it time to try again? — from
        the attempt stamp, so a duty that keeps failing is retried hourly
        rather than every tick, and a box whose duties all succeeded is left
        alone for a day.

        Both rows are durable, so the schedule survives a restart. That was
        always the point of the stamp; what it must not survive is work that
        never happened.
        """
        from db import models as db

        if not self._duties_due():
            return False

        now = time.time()
        try:
            last_attempt = float(db.get_snooze_state(self._DAILY_TIER_KEY) or 0)
        except (TypeError, ValueError):
            last_attempt = 0.0
        if last_attempt and now - last_attempt < DAILY_RETRY_INTERVAL_S:
            return False
        try:
            db.set_snooze_state(self._DAILY_TIER_KEY, str(now))
        except Exception as e:
            logger.warning("Could not stamp the daily maintenance attempt: %s", e)
        return True

    # ------------------------------------------------------------------
    # The daily tier
    # ------------------------------------------------------------------

    async def _run_daily_tier(self) -> None:
        """Run the outstanding daily duties, one at a time, off the loop.

        Every duty here used to run synchronously on the event loop —
        `incremental_vacuum`, `prune_cron` and the four hygiene prunes were
        plain blocking calls, unlike the WAL checkpoint and the backup, which
        were explicitly moved off it because running them on the loop "froze
        every session's SSE". A daily prune sweep froze it too; it just did
        so once a day, so nobody caught it in the act.
        """
        # Ordered first, and unchanged in intent: the duties below rewrite
        # the memory index and delete rows, and they must never do that
        # without a recent snapshot to undo them from. Sharing one helper
        # with the hourly check keeps that true now that the tier is a task
        # of its own rather than a block inside the same tick.
        await self._ensure_recent_backup()

        for name in self._duties_due():
            if name in self._daily_inflight:
                logger.warning(
                    "Daily maintenance duty %s is still running from an earlier attempt — not started again",
                    name,
                )
                continue
            await self._run_daily_duty(name)

    async def _run_daily_duty(self, name: str) -> None:
        """One duty, on a thread, under its own budget.

        `asyncio.shield` is the load-bearing part. A thread cannot be
        cancelled, so a plain `wait_for` around it would either hang until
        the thread finished anyway or leave the executor future cancelled-in
        -name-only. Shielding lets this coroutine give up on schedule while
        the thread runs on, and the future's done callback — not this
        function's exit — is what clears the in-flight marker. So the duty
        stays unstamped (eligible again in an hour) and is not started a
        second time on top of the thread still doing it.
        """
        body = getattr(self, f"_duty_{name}")
        future = asyncio.get_running_loop().run_in_executor(get_background_executor(), body)
        self._daily_inflight[name] = time.time()
        future.add_done_callback(lambda f, n=name: self._duty_thread_done(n, f))

        try:
            summary = await asyncio.wait_for(asyncio.shield(future), timeout=DAILY_DUTY_TIMEOUT)
        except asyncio.TimeoutError:
            self._daily_errors[name] = f"exceeded its {DAILY_DUTY_TIMEOUT}s budget"
            logger.warning(
                "Daily maintenance duty %s exceeded its %ds budget; its thread continues and the duty stays due",
                name,
                DAILY_DUTY_TIMEOUT,
            )
            return
        except asyncio.CancelledError:
            logger.info("Daily maintenance cancelled during %s; the duty stays due", name)
            raise
        except MaintenanceBusy as e:
            self._daily_errors[name] = str(e)
            logger.info("Daily maintenance duty %s deferred: %s", name, e)
            return
        except Exception as e:
            self._daily_errors[name] = str(e)
            logger.warning("Daily maintenance duty %s failed: %s", name, e, exc_info=True)
            return

        self._daily_errors.pop(name, None)
        self._stamp_duty(name)
        if summary:
            logger.info("Daily maintenance — %s: %s", name, summary)

    def _duty_thread_done(self, name: str, future) -> None:
        """The thread has actually stopped. Only now is the duty not running."""
        self._daily_inflight.pop(name, None)
        try:
            error = future.exception()
        except asyncio.CancelledError:
            return
        if error is not None and self._daily_errors.get(name, "").startswith("exceeded"):
            logger.warning("Daily maintenance duty %s failed after its budget ran out: %s", name, error)

    # -- the duties themselves. Blocking bodies, run on the background pool.

    def _duty_memory_health(self) -> str:
        from core.memory.store import get_memory_store

        store = get_memory_store()
        if store is None:
            return "memory store unavailable"
        return str(store.health_check(fix=True))

    def _duty_incremental_vacuum(self) -> str:
        from db import models as db
        from db.exclusive import exclusive

        # Exclusive, not merely announced: this is a rebuild of the free
        # list, and an operator pressing Optimize at the same moment is the
        # collision the S07 gate exists to make impossible. Whichever gets
        # there first wins; this one defers and comes back in an hour.
        with exclusive("maintenance:incremental-vacuum", drain_timeout=DB_DRAIN_TIMEOUT_S):
            db.incremental_vacuum()
        return "incremental vacuum complete"

    def _duty_prune_cron(self) -> str:
        from core.retention import prune_cron
        from db.exclusive import writing

        # Also done by snooze, but this ensures it happens even if snooze is
        # disabled. Same implementation as snooze's Activity 7 — one set of
        # retention budgets, two callers.
        with writing("maintenance:prune-cron", wait=DB_GATE_WAIT_S):
            counts = prune_cron()
        return f"{counts['runs']} runs, {counts['sessions']} sessions, {counts['state_log']} state_log rows pruned"

    def _duty_prune_hygiene(self) -> str:
        from core import retention
        from db import models as db
        from db.exclusive import writing

        with writing("maintenance:prune-hygiene", wait=DB_GATE_WAIT_S):
            token_usage = db.prune_orphaned_token_usage(max_age_days=30)
            messages = db.prune_old_session_messages(max_age_days=7)
            questions = db.prune_old_questions(max_age_days=7)
            # This tier runs even with snooze disabled — same reason the cron
            # cleanup lives here too.
            retention.prune_notifications()
        return f"{token_usage} token-usage, {messages} session-message, {questions} question rows pruned"

    # ------------------------------------------------------------------
    # Backups
    # ------------------------------------------------------------------

    async def _ensure_recent_backup(self) -> None:
        """Take a backup if the newest completed one is a day old or more.

        One implementation, two callers: the hourly check (which is what
        makes the schedule survive restarts — the tick counter does not) and
        the daily tier's first step (which is what keeps the mutating duties
        standing on a snapshot at most ~24h old). Single-flight, so the two
        cannot start two backups, and shielded, so a caller that gives up
        waiting does not cancel the one already running.
        """
        from scripts.backup import hours_since_last_backup

        try:
            age = await asyncio.to_thread(hours_since_last_backup)
        except Exception as e:
            logger.warning("Could not read backup freshness: %s", e)
            return
        if age is not None and age < 24.0:
            return

        if self._backup_task is None or self._backup_task.done():
            self._backup_task = asyncio.create_task(self._take_backup())
            self.track_task(self._backup_task)
        try:
            await asyncio.shield(self._backup_task)
        except Exception:
            pass  # _take_backup logged it; the tier proceeds as it always has

    async def _take_backup(self) -> None:
        """One backup run, off the loop — VACUUM INTO plus a corpus copy is
        seconds of blocking IO, the same reason the WAL checkpoint moved."""
        from scripts.backup import run_backup

        try:
            result = await run_background(run_backup)
        except Exception as e:
            logger.warning("Backup failed: %s", e)
            return
        if result.get("skipped"):
            logger.debug("Backup skipped: %s", result["skipped"])
        else:
            logger.info(
                "Backup complete: %s (%d memory files, %d rotated out)",
                result["db"],
                result["memory_files"],
                len(result["rotated_out"]),
            )

    @staticmethod
    def _checkpoint() -> None:
        """WAL checkpoint, announced so a rebuild waits for it."""
        from db import models as db
        from db.exclusive import writing

        with writing("maintenance:checkpoint", wait=DB_GATE_WAIT_S):
            db.checkpoint()

    async def _tick(self) -> None:
        """Execute stratified maintenance duties.

        Snooze is NOT run here — see _run_snooze and TICK_TIMEOUT.
        """
        from db import models as db
        from sessions.manager import get_manager

        manager = get_manager()
        tick = self._tick_count

        # Every tick (60s): reap dead subscribers, prune completed tasks
        reaped_subs = manager.reap_dead_subscribers()
        if reaped_subs:
            self._stats["subscribers_reaped"] += reaped_subs

        # Prune completed tracked tasks
        done = {t for t in self._tracked_tasks if t.done()}
        self._tracked_tasks -= done
        self._stats["tasks_completed"] += len(done)

        # Every 5 ticks (5 min): session reaping, orphan cleanup, partial cleanup
        if tick % 5 == 0:
            # Collect cron-protected session IDs
            protected: set[str] = set()
            try:
                import json

                # The scheduler owns where jobs are persisted — read its
                # constant rather than duplicating the path here.
                from core.extensions.scheduling import CRON_PATH

                if CRON_PATH.exists():
                    jobs = json.loads(CRON_PATH.read_text())
                    for job in jobs:
                        # Heartbeat jobs park session_id=None and carry the real
                        # id under heartbeat_session_id — without it their host
                        # session gets reaped out from under the heartbeat.
                        for key in ("session_id", "heartbeat_session_id"):
                            sid = job.get(key)
                            if sid:
                                protected.add(sid)
            except Exception as e:
                logger.warning("Failed to read cron protection list: %s", e)

            # Kernel reap BEFORE session reap (plan 2b): kernel_idle_seconds
            # (1500) < session max_idle (1800), so a session's kernel is
            # snapshotted+gone before the session object itself is popped —
            # never an orphaned child process. Off-loop: a dill snapshot is
            # seconds of blocking IO and must not hold the tick.
            try:
                from core.kernel import get_kernel_registry

                _kreg = get_kernel_registry()
                if _kreg.any_reapable():
                    _ktask = asyncio.create_task(asyncio.to_thread(_kreg.reap_idle))
                    self._tracked_tasks.add(_ktask)
            except Exception as e:
                logger.warning("Kernel reap scheduling failed: %s", e)

            reaped = manager.reap_idle_sessions(max_idle=1800, protected_ids=protected)
            if reaped:
                self._stats["sessions_reaped"] += reaped
                logger.info("Reaped %d idle sessions", reaped)

            # MCP upkeep: suspend idle stdio servers (child reaped, tools
            # kept, next call respawns) and schedule periodic tools/list
            # refreshes for servers that never send listChanged. Both are
            # cheap sync calls that only flip events / spawn tasks.
            try:
                from core.extensions.mcp.manager import get_mcp_manager_if_started

                _mcp = get_mcp_manager_if_started()
                if _mcp is not None:
                    _mcp.reap_idle()
                    _mcp.refresh_due()
            except Exception as e:
                logger.warning("MCP maintenance failed: %s", e)

            # Partial message cleanup
            cleaned = db.cleanup_old_partials(max_age_hours=1)
            if cleaned:
                self._stats["partials_cleaned"] += cleaned

        # Every 60 ticks (1 hour): WAL checkpoint. Off-loop — a checkpoint
        # can hold the DB busy for seconds on a large WAL, which froze every
        # session's SSE when run on the loop.
        if tick % 60 == 0:
            try:
                await asyncio.to_thread(self._checkpoint)
                logger.debug("WAL checkpoint complete")
            except Exception as e:
                logger.warning("WAL checkpoint failed: %s", e)

            # Daily backup, checked hourly. This lived in the 24h tier,
            # keyed on tick % 1440 — but the tick counter starts at zero on
            # every process start, so a box that restarts daily (every deploy
            # day) never reached tick 1440 and silently skipped its backups
            # for as long as the deploy streak lasted (4 days on the live
            # box). Due-ness comes from the newest COMPLETED snapshot's own
            # name-encoded timestamp, which survives restarts.
            await self._ensure_recent_backup()

        # The 24h tier is NOT here any more. It ran inside this coroutine,
        # under wait_for(TICK_TIMEOUT), with exactly one await in it — so the
        # timeout could only fire at the memory health check, and firing
        # abandoned the incremental vacuum and five prunes behind it. See
        # _run_daily_tier, scheduled from _heartbeat.


# Module singleton
_runner: MaintenanceRunner | None = None


def get_maintenance() -> MaintenanceRunner:
    global _runner
    if _runner is None:
        _runner = MaintenanceRunner()
    return _runner
