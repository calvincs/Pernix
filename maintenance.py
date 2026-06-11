"""Pernix — Background maintenance heartbeat with stratified duties."""

from __future__ import annotations

import asyncio
import logging
import time

from config import settings

logger = logging.getLogger("pernix.maintenance")

TICK_INTERVAL = 60  # seconds


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
                    await asyncio.wait_for(self._tick(), timeout=30)
                except asyncio.TimeoutError:
                    logger.warning("Maintenance tick exceeded 30s timeout, skipping")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Maintenance tick error: %s", e, exc_info=True)

    async def _tick(self) -> None:
        """Execute stratified maintenance duties."""
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
                        sid = job.get("session_id")
                        if sid:
                            protected.add(sid)
            except Exception as e:
                logger.warning("Failed to read cron protection list: %s", e)

            reaped = manager.reap_idle_sessions(max_idle=1800, protected_ids=protected)
            if reaped:
                self._stats["sessions_reaped"] += reaped
                logger.info("Reaped %d idle sessions", reaped)

            # Partial message cleanup
            cleaned = db.cleanup_old_partials(max_age_hours=1)
            if cleaned:
                self._stats["partials_cleaned"] += cleaned

        # Every N ticks: Snooze cycle (idle-time memory consolidation)
        if tick % settings.snooze_interval_ticks == 0:
            try:
                from core.snooze import get_snooze

                await get_snooze().run_cycle()
            except Exception as e:
                logger.error("Snooze cycle error: %s", e, exc_info=True)

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
                    health = store.health_check(fix=True)
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
