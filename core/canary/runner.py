"""Pernix — Canary runner: headless full-pipeline execution + scoring.

Each run: temp workspace (plan 1g override) → session_type="canary" session →
manager.prompt (the cron precedent) → wait for the turn to finish (including
reflect retries) → score by re-running the canary's gates against the final
workspace state → canary_runs row → cleanup (gates deleted, temp dir removed).

Sweeps run canaries sequentially — canary_max_concurrent stays 1 until the
model-concurrency story says otherwise; a sweep is a background measurement,
not a throughput problem.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from config import settings
from core.canary.parser import CanaryDef, load_canary, scan_canaries

logger = logging.getLogger("pernix.canary")

# Grace beyond the canary's own timeout for cancel to take effect before we
# give up waiting and score the run as-is (failed).
_CANCEL_GRACE_S = 30
_POLL_INTERVAL_S = 1.0
# Only used on the degraded no-task-handle path in _wait_for_turn_end: how long
# to give a turn to visibly leave IDLE_READY before assuming it already ended.
_START_GRACE_S = 5.0


@dataclass
class CanaryRunResult:
    task: str
    passed: bool
    trigger: str
    session_id: str = ""
    gate_results: list[dict] = field(default_factory=list)
    retries: int = 0
    tokens: int = 0
    duration_s: float = 0.0
    error: str = ""
    run_id: int | None = None
    flaky: bool = False

    @property
    def outcome(self) -> str:
        """pass | gate_fail | timeout | error | noop — the honest failure
        taxonomy. Only gate_fail means "the agent ran and the work was wrong";
        the others are wall-clock or harness trouble and must never feed the
        per-task tripwire."""
        if self.passed:
            return "pass"
        if self.error.startswith("timeout"):
            return "timeout"
        if self.error:
            return "error"
        if self.tokens == 0 and self.duration_s < 1.0:
            return "noop"
        return "gate_fail"

    def to_dict(self) -> dict:
        return {
            "task": self.task,
            "passed": self.passed,
            "outcome": self.outcome,
            "trigger": self.trigger,
            "session_id": self.session_id,
            "gates": self.gate_results,
            "retries": self.retries,
            "tokens": self.tokens,
            "duration_s": round(self.duration_s, 1),
            "error": self.error,
            "run_id": self.run_id,
            "flaky": self.flaky,
        }


def _seed_workspace(canary: CanaryDef, ws: Path) -> None:
    for rel, content in (canary.files or {}).items():
        target = ws / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


async def _wait_for_turn_end(session, deadline: float) -> bool:
    """Wait until the turn scheduled on `session` has actually finished.

    Returns True when the turn ended on its own; False on timeout (after
    which the caller has already requested cancel and granted grace).
    AWAITING_USER counts as an ending: a canary that asks a question into
    the void has failed its gates, which is exactly what gets recorded.

    **This must not be a bare "is the state parked?" poll.** `manager.prompt()`
    does not run the turn — its last act is
    `session.task = asyncio.create_task(_run_agent_safe(...))`, and the
    transition out of IDLE_READY happens *inside* that coroutine, which has
    not been entered when prompt() returns. A state-only poll therefore
    matched on its first check and reported "ended" before the agent had
    executed a single round: every canary scored FAIL against an untouched
    workspace in ~0.2s with 0 tokens, and the real turn ran on orphaned after
    run_canary's finally had already deleted the workspace under it. 99 runs,
    0 passes, for the life of the feature.

    So wait on the task handle, which is the only signal that is true exactly
    when the turn is over. Awaiting it also covers reflect retries and the
    queue drain, since `_finalize_turn` runs inside that same task.
    """
    from sessions import state_v2 as sv2

    parked = (sv2.SessionStateV2.IDLE_READY, sv2.SessionStateV2.AWAITING_USER)

    task = getattr(session, "task", None)
    if task is not None:
        if not task.done():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            done, _pending = await asyncio.wait({task}, timeout=remaining)
            if not done:
                return False
        # The task is finished. State normally already reads parked; give the
        # last transition a moment to land rather than trusting the ordering.
        while time.monotonic() < deadline:
            if sv2._current_state(session) in parked:
                return True
            await asyncio.sleep(_POLL_INTERVAL_S)
        return False

    # No task handle. In production `prompt()` always leaves one on an
    # accepted turn, so this means either a rejected prompt or a caller that
    # does not model the manager — worth saying out loud, because the failure
    # it degrades into is the silent one above.
    logger.warning(
        "Canary session %s has no task handle after prompt — falling back to "
        "state polling, which cannot tell 'not started yet' from 'finished'",
        getattr(session, "session_id", "?"),
    )
    start_deadline = min(deadline, time.monotonic() + _START_GRACE_S)
    while time.monotonic() < start_deadline:
        if sv2._current_state(session) not in parked:
            break  # the turn is visibly running; fall through to wait it out
        await asyncio.sleep(_POLL_INTERVAL_S)
    while time.monotonic() < deadline:
        if sv2._current_state(session) in parked:
            return True
        await asyncio.sleep(_POLL_INTERVAL_S)
    return False


async def run_canary(
    canary: CanaryDef | str,
    trigger: str = "manual",
    batch_id: str | None = None,
) -> CanaryRunResult:
    """Execute one canary end-to-end and record its canary_runs row."""
    from core.gates import run_gates
    from db import models as db
    from sessions.manager import get_manager
    from sessions.state import turn_state

    if isinstance(canary, str):
        loaded = load_canary(canary)
        if loaded is None:
            return CanaryRunResult(task=canary, passed=False, trigger=trigger, error="canary not found or invalid")
        canary = loaded

    manager = get_manager()
    start = time.monotonic()
    tmp = Path(tempfile.mkdtemp(prefix=f"canary-{canary.name[:24]}-"))
    sid = ""
    result = CanaryRunResult(task=canary.name, passed=False, trigger=trigger, flaky=canary.flaky)
    # Both consulted from `finally`, so they must exist before anything below
    # can raise. "Not started" and "ended" are the two states in which nothing
    # can still be reading the temp workspace.
    turn_started = False
    turn_ended = False
    try:
        _seed_workspace(canary, tmp)

        sid = manager.create_session(title=f"Canary: {canary.name}", session_type="canary")
        result.session_id = sid
        session = manager.get(sid)
        session.workspace_override = str(tmp)
        if canary.model:
            session.model_override = canary.model

        # Gates materialize as scope="canary" rows so the in-pipeline gate
        # hook (and the reflect clamp) exercises them like real gates.
        for g in canary.gates:
            db.add_gate(sid, g["name"], g["command"], watch_paths=g.get("watch_paths") or [], scope="canary")

        await manager.prompt(sid, canary.prompt)
        turn_started = True

        deadline = time.monotonic() + canary.timeout
        turn_ended = await _wait_for_turn_end(session, deadline)
        if not turn_ended:
            logger.warning("Canary '%s' exceeded %ds — requesting cancel", canary.name, canary.timeout)
            session.cancel_requested = True
            turn_ended = await _wait_for_turn_end(session, time.monotonic() + _CANCEL_GRACE_S)
            result.error = f"timeout after {canary.timeout}s"

        # Score: the canary's gates against the FINAL workspace state. This
        # is by definition the final attempt's outcome — reflect retries all
        # happened inside the turn we just waited out. run_gates resolves the
        # session's workspace_override itself.
        gate_results = await asyncio.to_thread(run_gates, sid, {}, 1)
        result.gate_results = [g.to_payload() for g in gate_results]
        result.passed = bool(gate_results) and all(g.passed for g in gate_results) and not result.error
        result.retries = int(turn_state(session).reflect_count or 0)
        try:
            result.tokens = int((db.get_session_usage(sid) or {}).get("total", 0))
        except Exception:
            pass
    except Exception as e:
        logger.exception("Canary '%s' run failed", canary.name)
        result.error = result.error or str(e)
    finally:
        result.duration_s = time.monotonic() - start
        # Gates are per-run scaffolding, never inherited by a later run.
        try:
            if sid:
                for g in canary.gates:
                    db.remove_gate(sid, g["name"])
        except Exception:
            pass
        try:
            s = manager.get(sid) if sid else None
            if s is not None:
                s.workspace_override = None
                s.model_override = None
        except Exception:
            pass
        # Only reclaim the workspace once the turn is genuinely over. Deleting
        # it under a still-running agent is what turned a refused cancel into
        # a worker flailing inside a directory that no longer existed, one
        # tool call per round, long after its sweep had reported. Leaking a
        # temp dir is the far cheaper failure — it is under the OS temp root,
        # and the path is logged so it can be reclaimed deliberately.
        if turn_ended or not turn_started:
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            logger.warning(
                "Canary '%s' never ended its turn — leaving %s in place rather " "than deleting it under a live agent",
                canary.name,
                tmp,
            )

    try:
        result.run_id = db.add_canary_run(
            task=canary.name,
            trigger=trigger,
            session_id=sid or None,
            gate_results_json=json.dumps(result.gate_results),
            passed=result.passed,
            retries=result.retries,
            tokens=result.tokens,
            duration_s=result.duration_s,
            batch_id=batch_id,
            outcome=result.outcome,
            error=result.error[:500],
        )
    except Exception as e:
        logger.error("Failed to record canary run '%s': %s", canary.name, e)

    logger.info(
        "Canary '%s' %s (%.0fs, %d retries, trigger=%s)",
        canary.name,
        "PASSED" if result.passed else f"FAILED[{result.outcome}]",
        result.duration_s,
        result.retries,
        trigger,
    )
    return result


def _due_this_sweep(canary: CanaryDef, sweep_index: int) -> bool:
    """Cadence filter for scheduled sweeps: run every Nth sweep.

    Deterministic and stable per canary — the phase is derived from the
    canary's own name, so demoted canaries spread across the rotation
    instead of all landing on the same sweep.
    """
    cadence = max(1, int(canary.cadence or 1))
    if cadence == 1:
        return True
    phase = sum(canary.name.encode("utf-8")) % cadence
    return sweep_index % cadence == phase


def _next_sweep_index() -> int:
    """Monotonic scheduled-sweep counter, durable across restarts."""
    from db import models as db

    try:
        current = int(db.get_snooze_state("canary_sweep_index") or "0")
    except (TypeError, ValueError):
        current = 0
    db.set_snooze_state("canary_sweep_index", str(current + 1))
    return current


async def run_sweep(
    trigger: str = "scheduled",
    batch_id: str | None = None,
    names: list[str] | None = None,
) -> list[CanaryRunResult]:
    """Run the whole suite (or a named subset) sequentially.

    Scheduled sweeps honour each canary's `cadence`; post_batch and manual
    sweeps never do. The post-batch sweep is the tripwire's active probe and
    must cover every canary that could regress, and a human asking for a run
    means now.
    """
    if not settings.canary_enabled:
        logger.info("Canary sweep skipped: canary_enabled is off")
        return []
    defs = scan_canaries()
    if names:
        wanted = set(names)
        defs = [d for d in defs if d.name in wanted]
    elif trigger == "scheduled":
        sweep_index = _next_sweep_index()
        due = [d for d in defs if _due_this_sweep(d, sweep_index)]
        deferred = len(defs) - len(due)
        if deferred:
            logger.info("Canary sweep #%d: %d canary(ies) deferred by cadence", sweep_index, deferred)
        defs = due
    if not defs:
        logger.info("Canary sweep: no canaries to run")
        return []
    results: list[CanaryRunResult] = []
    for d in defs:
        results.append(await run_canary(d, trigger=trigger, batch_id=batch_id))
    passed = sum(1 for r in results if r.passed)
    logger.info("Canary sweep complete: %d/%d passed (trigger=%s)", passed, len(results), trigger)
    return results
