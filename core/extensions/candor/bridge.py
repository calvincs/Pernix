"""Pernix — Candor bridge: every Pernix↔Candor interaction crosses this module.

Candor (the calibrated operational-memory substrate) is single-writer and NOT
thread-safe: its SQLite connection is bound to the thread that opened it and
its ledger state is unguarded. The bridge therefore owns exactly ONE
CandorSystem, confined to a dedicated single-worker executor.
asyncio.to_thread is NOT safe here — it rotates threads.

Failure is never fatal: every entry point is gated on settings.candor_enabled,
wrapped against exceptions, and a circuit breaker turns the bridge inert for
the rest of the process after repeated store failures.

Two write paths feed the store:
- record() — turn-end observations for already-admitted facts (sub-ms each).
  Observations for not-yet-admitted statements go to a pending JSONL buffer:
  Candor permanently drops evidence observed before its fact is admitted, and
  observe(ts=...) supports backfill, so buffering is lossless.
- run_maintenance() — snooze-time: assert buffered statements as candidates,
  run_gate() (the expensive sweep), drain the buffer with original event
  times, and periodically checkpoint so process restart doesn't refold the
  whole ledger.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from config import settings

logger = logging.getLogger("pernix.ext.candor")

# Statements seeded on first maintenance so runtime observations have an
# admitted fact to land on. Per-tool facts enter via the pending buffer the
# first time a tool is observed.
BASE_STATEMENTS: list[dict] = [
    {"pred": "tool_ok", "args": ["*"], "stmt_type": "frequency"},
    {"pred": "turn_ok", "args": ["*"], "stmt_type": "frequency"},
    {"pred": "reflect_verdict", "args": ["*"], "stmt_type": "categorical"},
    {"pred": "user_fact", "args": ["*"], "stmt_type": "frequency"},
]

_DRAIN_CHUNK_LINES = 200
_CHECKPOINT_EVERY_N_RUNS = 10
_MAX_CONSECUTIVE_FAILURES = 5


def _stmt_key(pred: str, args: list) -> tuple:
    return (pred, tuple(str(a) for a in args))


class CandorBridge:
    """Owns the process's single CandorSystem on a dedicated thread."""

    def __init__(self, store_dir: str | None = None):
        self._root = Path(store_dir or settings.candor_store_dir)
        self._exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="candor")
        self._system: Any = None
        self._broken = False
        self._closed = False
        self._consecutive_failures = 0
        # Statement keys with a confirmed admitted fact. Only touched on the
        # executor thread, so no lock is needed.
        self._known: set[tuple] = set()
        # Statement keys already asserted as candidates this process (avoids
        # re-asserting between gate runs).
        self._asserted: set[tuple] = set()
        self._maintenance_runs = 0
        self._brief_cache: str | None = None

    # ------------------------------------------------------------------
    # Lifecycle (executor thread only)
    # ------------------------------------------------------------------

    @property
    def _pending_path(self) -> Path:
        return self._root / "pending.jsonl"

    @property
    def _cursor_path(self) -> Path:
        return self._root / "pending.cursor"

    def _ensure_open(self):
        if self._system is None:
            from candor.system import CandorSystem  # zero-dep, stdlib-only

            self._root.mkdir(parents=True, exist_ok=True)
            self._system = CandorSystem(self._root / "store")
            # Default quotas (3000 obs / 500 candidates per actor per day)
            # throttle real traffic — provision before anything writes.
            self._system.set_actor_quota("agent:pernix", obs_per_epoch=100_000, cand_per_epoch=10_000)
            self._system.set_actor_quota("verifier:reflect", obs_per_epoch=100_000)
            self._system.set_actor_quota("agent:curiosity", cand_per_epoch=10_000)
            self._system.set_actor_quota("human:user", obs_per_epoch=100_000)
            logger.info("Candor store opened at %s", self._root / "store")
        return self._system

    def _guarded(self, fn: Callable, *args) -> Any:
        """Run fn(system, *args) on the executor thread with breaker accounting."""
        if self._broken or self._closed:
            return None
        try:
            system = self._ensure_open()
            result = fn(system, *args)
            self._consecutive_failures = 0
            return result
        except Exception as e:
            self._consecutive_failures += 1
            if self._consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                self._broken = True
                logger.error(
                    "Candor bridge disabled after %d consecutive failures (last: %s). " "Restart the process to retry.",
                    self._consecutive_failures,
                    e,
                )
            else:
                logger.warning(
                    "Candor call failed (%d/%d): %s", self._consecutive_failures, _MAX_CONSECUTIVE_FAILURES, e
                )
            return None

    async def _submit(self, fn: Callable, *args) -> Any:
        if self._broken or self._closed or not settings.candor_enabled:
            return None
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._exec, self._guarded, fn, *args)

    def _submit_sync(self, fn: Callable, *args, timeout: float = 30.0) -> Any:
        """Blocking submit for callers already OFF the event loop (tool executor threads)."""
        if self._broken or self._closed or not settings.candor_enabled:
            return None
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("CandorBridge sync call on the event loop — use the async API")
        return self._exec.submit(self._guarded, fn, *args).result(timeout=timeout)

    async def close(self) -> None:
        """Release the store's writer lock and stop the executor."""
        if self._closed:
            return
        self._closed = True
        if self._system is not None:
            loop = asyncio.get_running_loop()

            def _close_sync(system) -> None:
                system.close()

            try:
                await loop.run_in_executor(self._exec, _close_sync, self._system)
            except Exception as e:
                logger.warning("Candor close failed: %s", e)
            self._system = None
        self._exec.shutdown(wait=False)

    # ------------------------------------------------------------------
    # Write path — turn end
    # ------------------------------------------------------------------

    async def record(self, observations: list[dict]) -> dict | None:
        """Record turn observations. Admitted facts observe directly; the rest buffer.

        Each observation dict: {pred, args, stmt_type, outcome|value, ctx, actor, ts}.
        """
        return await self._submit(self._record_impl, observations)

    def record_nowait(self, observations: list[dict]) -> None:
        """Fire-and-forget record, safe from ANY thread including the event loop.

        Never blocks: it only enqueues onto the bridge executor. For callers
        (like MemoryStore mutations) where waiting for the result would be
        wrong in some contexts and pointless in all of them.
        """
        if self._broken or self._closed or not settings.candor_enabled or not observations:
            return
        try:
            self._exec.submit(self._guarded, self._record_impl, observations)
        except RuntimeError:
            pass  # executor already shut down (process teardown)

    def _record_impl(self, system, observations: list[dict]) -> dict:
        observed = buffered = 0
        buffer_lines: list[str] = []
        for obs in observations:
            key = _stmt_key(obs["pred"], obs["args"])
            stmt = {"pred": obs["pred"], "args": obs["args"]}
            if key not in self._known and system.fact_id_for(stmt) is not None:
                self._known.add(key)
            if key in self._known:
                system.observe(
                    stmt,
                    outcome=obs.get("outcome"),
                    ctx=obs.get("ctx") or {},
                    actor=obs.get("actor", "agent:pernix"),
                    value=obs.get("value"),
                    ts=obs.get("ts"),
                )
                observed += 1
            else:
                buffer_lines.append(json.dumps(obs, separators=(",", ":")))
                buffered += 1
        if buffer_lines:
            self._root.mkdir(parents=True, exist_ok=True)
            with self._pending_path.open("a", encoding="utf-8") as fh:
                fh.write("\n".join(buffer_lines) + "\n")
        return {"observed": observed, "buffered": buffered}

    # ------------------------------------------------------------------
    # Maintenance path — snooze
    # ------------------------------------------------------------------

    async def run_maintenance(self, should_abort: Callable[[], bool]) -> dict:
        """Seed candidates, run the gate, drain the pending buffer, checkpoint.

        Phases run as separate executor jobs; should_abort() is polled between
        them (and between drain chunks) so snooze cancellation stays prompt.
        Work is idempotent: the drain cursor is durable, and the worst case on
        a mid-chunk cut is one chunk observed twice.
        """
        stats: dict = {}
        if should_abort() or not settings.candor_enabled:
            return stats
        stats["seeded"] = await self._submit(self._seed_impl) or 0
        if should_abort():
            return stats
        gate_runs = await self._submit(self._gate_impl)
        stats["gate_decisions"] = gate_runs if gate_runs is not None else 0
        drained = 0
        while not should_abort():
            n = await self._submit(self._drain_chunk_impl, _DRAIN_CHUNK_LINES)
            if not n:
                break
            drained += n
        stats["drained"] = drained
        if drained and not should_abort():
            # One more sweep so the gate folds the backfilled evidence.
            await self._submit(self._gate_impl)
        if not should_abort():
            self._maintenance_runs += 1
            if self._maintenance_runs % _CHECKPOINT_EVERY_N_RUNS == 1:
                if await self._submit(self._checkpoint_impl):
                    stats["checkpointed"] = True
        return stats

    async def degraded_tools(self) -> list[dict]:
        """Calibrated below-threshold tools (adaptive producer feed, plan 4d)."""
        from core.extensions.candor.intel import collect_degraded_tools

        return await self._submit(collect_degraded_tools) or []

    def _seed_impl(self, system) -> int:
        """Assert base vocabulary + buffered statements as gate candidates."""
        pending_stmts: dict[tuple, dict] = {}
        for base in BASE_STATEMENTS:
            pending_stmts[_stmt_key(base["pred"], base["args"])] = base
        for obs in self._iter_pending():
            stmt = {"pred": obs["pred"], "args": obs["args"], "stmt_type": obs.get("stmt_type", "frequency")}
            pending_stmts.setdefault(_stmt_key(obs["pred"], obs["args"]), stmt)
        seeded = 0
        for key, stmt in pending_stmts.items():
            if key in self._known or key in self._asserted:
                continue
            if system.fact_id_for({"pred": stmt["pred"], "args": stmt["args"]}) is not None:
                self._known.add(key)
                continue
            system.assert_(stmt, source="pernix:runtime", actor="agent:pernix")
            self._asserted.add(key)
            seeded += 1
        return seeded

    def _gate_impl(self, system) -> int:
        runs = system.run_gate()
        # A gate run settles candidates either way; forget the asserted set so
        # rejected statements can re-enter later on fresh evidence.
        self._asserted.clear()
        return len(runs)

    def _iter_pending(self):
        """Yield buffered observations from the durable cursor onward."""
        if not self._pending_path.exists():
            return
        cursor = self._read_cursor()
        with self._pending_path.open("r", encoding="utf-8") as fh:
            fh.seek(cursor)
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

    def _read_cursor(self) -> int:
        try:
            return int(self._cursor_path.read_text().strip() or 0)
        except (OSError, ValueError):
            return 0

    def _drain_chunk_impl(self, system, limit: int) -> int:
        """Observe up to `limit` buffered lines; advance the cursor; truncate at EOF."""
        if not self._pending_path.exists():
            return 0
        cursor = self._read_cursor()
        processed = 0
        dropped = 0
        with self._pending_path.open("r", encoding="utf-8") as fh:
            fh.seek(cursor)
            while processed < limit:
                line = fh.readline()
                if not line:
                    break
                cursor = fh.tell()
                processed += 1
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obs = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                key = _stmt_key(obs["pred"], obs["args"])
                stmt = {"pred": obs["pred"], "args": obs["args"]}
                if key not in self._known and system.fact_id_for(stmt) is not None:
                    self._known.add(key)
                if key not in self._known:
                    # Gate declined (or hasn't seen) this statement — dropping
                    # is deliberate: observing an unadmitted fact loses the
                    # evidence silently anyway.
                    dropped += 1
                    continue
                system.observe(
                    stmt,
                    outcome=obs.get("outcome"),
                    ctx=obs.get("ctx") or {},
                    actor=obs.get("actor", "agent:pernix"),
                    value=obs.get("value"),
                    ts=obs.get("ts"),
                )
            at_eof = not fh.readline()
        self._cursor_path.write_text(str(cursor))
        if at_eof:
            self._pending_path.write_text("")
            self._cursor_path.write_text("0")
        if dropped:
            logger.info("Candor drain dropped %d observation(s) for unadmitted statements", dropped)
        return processed

    def _checkpoint_impl(self, system) -> bool:
        system.checkpoint()
        # Keep the two newest checkpoint snapshots; the rest are dead weight.
        ckpt_dir = self._root / "store" / "checkpoints"
        if ckpt_dir.exists():
            snaps = sorted(ckpt_dir.glob("*.sqlite3"), key=lambda p: p.stat().st_mtime, reverse=True)
            for old in snaps[2:]:
                try:
                    old.unlink()
                except OSError:
                    pass
        return True

    # ------------------------------------------------------------------
    # Read path — intel
    # ------------------------------------------------------------------

    async def intel_brief(self) -> str | None:
        """Build the scout-facing operational brief (live reads, executor thread)."""
        brief = await self._submit(self._brief_impl)
        if brief is not None:
            self._brief_cache = brief or None
        return self._brief_cache if brief is not None else None

    def _brief_impl(self, system) -> str:
        from core.extensions.candor.intel import build_brief

        return build_brief(system) or ""

    def cached_brief(self) -> str | None:
        """Last successfully built brief — safe from any thread, never blocks."""
        return self._brief_cache

    # ------------------------------------------------------------------
    # Async reads for the dream add-on (loop-safe; core/dream only)
    # ------------------------------------------------------------------

    async def predict(self, pred: str, args: list) -> dict | None:
        """Loop-safe twin of predict_sync for background (snooze) callers."""

        def _impl(system, pred, args):
            from core.extensions.candor.intel import describe_prediction

            return describe_prediction(system, pred, args)

        return await self._submit(_impl, pred, args)

    async def health_snapshot(self) -> dict | None:
        """Candor's own health() report — calibration, invariants, queue depth."""

        def _impl(system):
            return system.health()

        return await self._submit(_impl)

    # ------------------------------------------------------------------
    # Sync reads for agent tools (tool-executor threads only)
    # ------------------------------------------------------------------

    def predict_sync(self, pred: str, args: list, timeout: float = 20.0) -> dict | None:
        def _impl(system, pred, args):
            from core.extensions.candor.intel import describe_prediction

            return describe_prediction(system, pred, args)

        return self._submit_sync(_impl, pred, args, timeout=timeout)

    def why_sync(self, pred: str, args: list, timeout: float = 45.0) -> dict | None:
        def _impl(system, pred, args):
            fid = system.fact_id_for({"pred": pred, "args": args})
            if fid is None:
                return None
            return system.why(fid)

        return self._submit_sync(_impl, pred, args, timeout=timeout)

    def questions_sync(self, timeout: float = 20.0) -> list | None:
        def _impl(system):
            return system.questions()

        return self._submit_sync(_impl, timeout=timeout)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_bridge: CandorBridge | None = None
_bridge_lock = threading.Lock()


def get_candor_bridge() -> CandorBridge:
    global _bridge
    if _bridge is None:
        with _bridge_lock:
            if _bridge is None:
                _bridge = CandorBridge()
    return _bridge


async def shutdown_candor_bridge() -> None:
    """Close the store if a bridge was ever created. Called at app shutdown."""
    global _bridge
    if _bridge is not None:
        await _bridge.close()
        _bridge = None


def _reset_bridge_for_tests() -> None:
    global _bridge
    if _bridge is not None:
        _bridge._closed = True
        _bridge._exec.shutdown(wait=False)
    _bridge = None
