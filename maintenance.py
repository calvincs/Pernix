"""Pernix — Background maintenance heartbeat with stratified duties."""

from __future__ import annotations

import asyncio
import logging
import time

from config import settings

logger = logging.getLogger("pernix.maintenance")

TICK_INTERVAL = 60  # seconds

# Bound on the fast duties only (subscriber reaping, session reaping, partial
# cleanup, checkpoint, hygiene). Snooze is deliberately NOT covered by this —
# it has its own, larger budget and runs outside the tick. See _run_snooze.
TICK_TIMEOUT = 30  # seconds

# Headroom over settings.snooze_max_cycle_seconds. run_cycle already bounds
# itself; this outer wait only catches a cycle wedged outside its own wait_for,
# so it must never be the one that fires first.
SNOOZE_TIMEOUT_GRACE = 15  # seconds


class MaintenanceRunner:
    """Background heartbeat that runs periodic maintenance tasks."""

    def __init__(self):
        self._task: asyncio.Task | None = None
        self._tracked_tasks: set[asyncio.Task] = set()
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
            "snooze": get_snooze().get_stats(),
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
                if self._tick_count % settings.snooze_interval_ticks == 0:
                    await self._run_snooze()
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
                from pathlib import Path

                cron_path = Path("data/cron_jobs.json")
                if cron_path.exists():
                    jobs = json.loads(cron_path.read_text())
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

            # Partial message cleanup
            cleaned = db.cleanup_old_partials(max_age_hours=1)
            if cleaned:
                self._stats["partials_cleaned"] += cleaned

        # Every 60 ticks (1 hour): WAL checkpoint. Off-loop — a checkpoint
        # can hold the DB busy for seconds on a large WAL, which froze every
        # session's SSE when run on the loop.
        if tick % 60 == 0:
            try:
                await asyncio.to_thread(db.checkpoint)
                logger.debug("WAL checkpoint complete")
            except Exception as e:
                logger.warning("WAL checkpoint failed: %s", e)

        # Every 1440 ticks (24 hours): memory maintenance, vacuum
        if tick % 1440 == 0:
            try:
                from core.memory.store import get_memory_store

                store = get_memory_store()
                if store:
                    health = await asyncio.to_thread(store.health_check, fix=True)
                    logger.info("Memory maintenance: %s", health)
            except Exception as e:
                logger.warning("Memory maintenance failed: %s", e)

            try:
                db.incremental_vacuum()
                logger.debug("Incremental vacuum complete")
            except Exception as e:
                logger.warning("Incremental vacuum failed: %s", e)

            # Cron cleanup (also done by snooze, but this ensures it happens even if snooze disabled)
            try:
                pruned_runs = db.prune_cron_runs()
                pruned_sessions = db.prune_cron_sessions()
                if pruned_runs or pruned_sessions:
                    logger.info("Cron cleanup: %d runs, %d sessions pruned", pruned_runs, pruned_sessions)
            except Exception as e:
                logger.warning("Cron cleanup failed: %s", e)

            # Data hygiene: prune orphaned/old rows from auxiliary tables
            try:
                pruned = db.prune_orphaned_token_usage(max_age_days=30)
                if pruned:
                    logger.info("Token usage cleanup: %d rows pruned", pruned)
                pruned = db.prune_old_session_messages(max_age_days=7)
                if pruned:
                    logger.info("Session messages cleanup: %d rows pruned", pruned)
                pruned = db.prune_old_questions(max_age_days=7)
                if pruned:
                    logger.info("Questions cleanup: %d rows pruned", pruned)
            except Exception as e:
                logger.warning("Data hygiene cleanup failed: %s", e)


# Module singleton
_runner: MaintenanceRunner | None = None


def get_maintenance() -> MaintenanceRunner:
    global _runner
    if _runner is None:
        _runner = MaintenanceRunner()
    return _runner
