"""Pernix — Worker management: any chat session can spawn parallel workers.

Workers run in fresh context (Ralph pattern). Each worker gets its own scout.
Communication: parent↔worker only. Worker↔worker forbidden.
Workers cannot spawn sub-workers (enforced by executor via denied_session_types).
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import threading
import time
from pathlib import Path

from config import settings
from db import models as db

logger = logging.getLogger("pernix.ext.orchestration")

# Lock to make the active-count check + create_session atomic, preventing
# concurrent spawn_worker calls from both passing the limit check before
# either has committed a new session to the DB.
_spawn_lock = threading.Lock()

# Statuses that mean a worker is no longer occupying a slot.
#
# "unknown" is load-bearing: SessionManager.get_status returns it for any
# session no longer resident in memory, and workers are reaped after
# ~1800s idle. Omitting it makes every completed-then-reaped worker count
# as active forever, so a long-lived parent eventually cannot spawn at all.
_WORKER_INACTIVE_STATUSES = frozenset({"idle", "error", "deleted", "unknown"})


def _count_active_workers(manager, parent) -> int:
    """Workers of `parent` still occupying a slot.

    Single definition shared by both spawn gates below. They used to carry
    separate inline tuples that disagreed about "unknown", so the capacity
    warning and the max_concurrent_workers limit counted different things.
    """
    return sum(
        1 for wid in list(parent.worker_ids) if manager.get_status(wid).get("status") not in _WORKER_INACTIVE_STATUSES
    )


def spawn_worker(
    task_description: str,
    title: str = "",
    model: str = "",
    auto_resume_parent: bool = False,
    spec: str = "",
    _context: dict | None = None,
) -> str:
    """Spawn a worker session for a subtask. Returns worker session ID.

    If model is specified, the worker runs on that model instead of the default.
    Useful for delegating to specialized models (e.g. vision, code).

    spec: optional worker_spec id from the [WORKER SPECS] catalog (an
    approved adaptive-layer template). Supplies instructions, a model, and a
    gate set; explicit model=/title= arguments override the spec's.

    auto_resume_parent: if True, this worker is added to the parent's watch-set
    so the parent auto-resumes when all watched workers complete. Use together
    with await_workers(suspend=True) to suspend the parent until results arrive.
    """
    ctx = _context or {}
    parent_id = ctx.get("session_id", "")
    if not parent_id:
        return "Error: No parent session context"

    from sessions.manager import get_manager

    manager = get_manager()

    # Resolve the spec FIRST (plan follow-on: worker_spec consumption) so a
    # spec-supplied model flows through the normal validation below and an
    # unknown spec fails before any session exists.
    spec_data = None
    if spec:
        from core.adaptive.specs import load_worker_spec

        spec_data = load_worker_spec(spec)
        if spec_data is None:
            return f"Error: No active worker_spec '{spec}'. See the [WORKER SPECS] catalog for valid ids."
        model = model or spec_data.get("model", "")

    # State precondition: spawning is only legal during an active agent
    # turn (PROCESSING). Any other state (AWAITING_WORKERS, FINALIZING,
    # IDLE_READY) means the spawn is racing the parent's lifecycle and
    # the new worker would land in an inconsistent watch-set.
    parent = manager.get(parent_id)
    if parent:
        from sessions import state_v2 as sv2

        parent_state = sv2._current_state(parent)
        if parent_state is not sv2.SessionStateV2.PROCESSING:
            return (
                f"Error: Cannot spawn worker — parent is in state "
                f"{parent_state.value}, not processing. spawn_worker can "
                "only be called from inside an active agent turn."
            )

    # Warn if LLM slots are saturated (check before creating session)
    if parent:
        try:
            from core.llm.client import _get_semaphore_stats

            stats = _get_semaphore_stats()
            active_workers = _count_active_workers(manager, parent)
            if active_workers >= stats["capacity"]:
                return (
                    f"Warning: {active_workers} worker(s) already active but only "
                    f"{stats['capacity']} LLM slot(s) available. Additional workers will "
                    f"queue and likely timeout. Await current workers first, or spawn fewer."
                )
        except Exception:
            pass

    # Resolve and validate model before creating the session so we can return
    # early on error without creating an orphaned session.
    if model:
        try:
            from core.llm.client import get_llm_client

            client = get_llm_client()
            registry = client.router.registry
            resolved = registry.resolve_model_id(model)
            if resolved != model:
                logger.info("Worker model '%s' resolved to '%s'", model, resolved)
                model = resolved
            # Verify provider routing won't send to wrong backend
            provider = registry.resolve_provider(model)
            if provider == "ollama" and "/" not in model:
                # Bare name routed to Ollama — check if Ollama actually has it
                info = registry.get_model_info(model)
                if not info:
                    return (
                        f"Error: Model '{model}' not found in Ollama or OpenRouter. "
                        f"Use the fully-qualified name (e.g. 'x-ai/grok-2' for OpenRouter)."
                    )
        except Exception as e:
            logger.warning("Could not validate worker model '%s': %s", model, e)

    # Enforce limit atomically — count-check + create_session under a lock so
    # two concurrent spawn_worker calls can't both pass before either commits.
    worker_title = title or task_description[:50]
    with _spawn_lock:
        parent = manager.get(parent_id)
        if parent:
            active_count = _count_active_workers(manager, parent)
            if active_count >= settings.max_concurrent_workers:
                return f"Error: Max active workers ({settings.max_concurrent_workers}) reached. Wait for running workers to complete."

        # Create session inside the lock to atomically reserve the slot.
        worker_id = manager.create_session(
            title=worker_title,
            system_prompt="",
            session_type="worker",
            parent_session_id=parent_id,
        )

    summary_file = f".worker_{worker_id[:12]}_summary.md"
    spec_instructions = ""
    if spec_data and spec_data.get("instructions"):
        spec_instructions = f"Your role (from the '{spec_data['entry_id']}' template):\n{spec_data['instructions']}\n\n"
    system_prompt = (
        f"You are a focused worker agent. {spec_instructions}Your task:\n{task_description}\n\n"
        "Complete the task using tools as needed.\n"
        f"When done, write a {summary_file} file in the workspace with what you accomplished.\n"
    )
    if model:
        system_prompt += f"\nYou are running on model: {model}\n"

    # Spec gates attach to the worker session before its first prompt — the
    # post-task hook path runs them like any session's gates, and the
    # reflect clamp holds the worker to them (plan 3a machinery, unchanged).
    if spec_data and spec_data.get("gates"):
        gate_names = []
        for g in spec_data["gates"]:
            try:
                db.add_gate(worker_id, g["name"], g["command"], watch_paths=g.get("watch_paths") or [], scope="session")
                gate_names.append(g["name"])
            except Exception as e:
                logger.warning("Worker spec gate '%s' failed to attach: %s", g.get("name"), e)
        if gate_names:
            system_prompt += (
                f"\nYour work is verified by deterministic gates: {', '.join(gate_names)}. "
                "They must pass for the task to count as done.\n"
            )

    # Attachment visibility: workers CAN read attachment bytes from the shared
    # workspace (file_read / bash), but images are only auto-inlined as vision
    # blocks when the worker runs on a vision-capable model. If the parent's
    # last user message has image attachments and no model_override was set,
    # tell the worker what's available and how to access it.
    try:
        import re as _re

        parent_msgs = db.get_messages(parent_id)
        last_user = next(
            (m for m in reversed(parent_msgs) if m.get("role") == "user"),
            None,
        )
        if last_user:
            attached = _re.findall(
                r"\[attached:\s*([^\]\s]+(?:\s+[^\]]*)?)\]",
                last_user.get("content", "") or "",
            )
            if attached:
                names = ", ".join(n.split()[0].rstrip(",") for n in attached)
                if model:
                    system_prompt += (
                        f"\nParent attachments available in workspace: {names}. "
                        "Images are inlined for you if your model supports vision; "
                        "otherwise use file_read or call_model(image_path=...).\n"
                    )
                else:
                    system_prompt += (
                        f"\nParent attachments available in workspace: {names}. "
                        "You inherit the default model, which may not support vision. "
                        "To analyze images inline, re-spawn with model=<vision-capable>; "
                        "otherwise use file_read for bytes or call_model(image_path=...) for analysis.\n"
                    )
    except Exception as _e:
        logger.debug("worker attachment hint skipped: %s", _e)
    db.update_session(worker_id, system_prompt=system_prompt)

    # Set model override on worker session if specified. The "not yet done"
    # gate is now implicit in v2 — a freshly-created worker is in IDLE_READY
    # but has never run; check_workers/await_workers use the existence of
    # state_log rows (or its absence + last_activity_time) to distinguish
    # "never ran" from "ran and settled."
    worker_session = manager.get(worker_id)
    if worker_session and model:
        worker_session.model_override = model
    # Workers inherit the parent's live goal for token_usage stamping
    # (plan 3b): a goal's budget must see fan-out spend, and workers bill
    # to their own session_id — the flat goal_id SUM is what unifies them.
    if worker_session is not None:
        parent_session = manager.get(parent_id)
        worker_session.active_goal_id = getattr(parent_session, "active_goal_id", None)

    # Resolve event loop before any threadsafe operations.
    ctx = _context or {}
    loop = ctx.get("_loop")
    if not loop:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return "Error: Cannot spawn worker — no event loop available. Ensure the tool executor passes _loop in context."

    if parent:
        # Dispatch append to event loop so worker_ids is only mutated on the
        # event loop thread — prevents RuntimeError from concurrent iteration.
        loop.call_soon_threadsafe(parent.worker_ids.append, worker_id)
        if auto_resume_parent:

            def _add_watched_and_persist():
                parent._watched_worker_ids.add(worker_id)
                manager._persist_watched(parent)

            loop.call_soon_threadsafe(_add_watched_and_persist)

        # Extend the orchestrator's LLM budget. The base llm_session_timeout
        # is a wall-clock guard; for a parent that spawns workers and waits
        # on them, the wall-clock is dominated by the children, not the
        # parent's own LLM work. Without this, hand-rolled orchestrators
        # (cron jobs that use spawn_worker + await_workers directly, not
        # run_workflow) hit LLMSessionTimeoutError mid-flight and the
        # synthesis turn dies on the first scout/agent acquire — the
        # bc6e9824/cdbf08c5/8b6345bf cron failures.
        try:
            from core.llm.client import extend_session_budget as _extend

            base = float(settings.llm_session_timeout) if settings.llm_session_timeout > 0 else 0.0
            if base > 0:
                # +1 for this spawn (worker_ids append is queued, not yet
                # visible) + 1 for the synthesis/reconciliation turn after
                # workers report back. Each unit = one base_timeout.
                worker_count = len(parent.worker_ids) + 1
                extension = min((worker_count + 1) * base, 24 * 3600.0)
                _extend(parent_id, extension)
        except Exception as _ext_err:
            logger.debug("spawn_worker: failed to extend parent budget: %s", _ext_err)

    async def _start():
        try:
            await manager.prompt(worker_id, task_description)
        except Exception as e:
            # Spawn-time failure: manager.prompt raised before the worker
            # ever reached PROCESSING/IDLE_READY. The state-machine path
            # that calls _on_watched_worker_done will never fire, so a
            # parent waiting on this single worker would deadlock. Clean
            # up the watch-set and emit a failure event so resume can
            # happen via _on_watched_worker_done's stale-purge or directly.
            logger.error("Worker %s failed to start: %s", worker_id, e, exc_info=True)
            try:
                w = manager.get(worker_id)
                if w is not None:
                    w.error = str(e)
                    w.termination_reason = "error"
                parent_obj = manager.get(parent_id) if parent_id else None
                manager.emit(
                    parent_id,
                    {
                        "type": "worker.failed",
                        "worker_id": worker_id,
                        "error": str(e),
                    },
                )
                if parent_obj is not None and worker_id in parent_obj._watched_worker_ids:
                    parent_obj._watched_worker_ids.discard(worker_id)
                    manager._persist_watched(parent_obj)
                    if not parent_obj._watched_worker_ids:
                        await manager._resume_from_workers(parent_obj)
            except Exception as _cleanup:
                logger.error("Worker spawn-fail cleanup error for %s: %s", worker_id, _cleanup)

    asyncio.run_coroutine_threadsafe(_start(), loop)

    # Emit event to parent — include effective model so the UI can display it
    _effective_model = model or settings.llm_model
    manager.emit(
        parent_id,
        {
            "type": "worker.started",
            "worker_id": worker_id,
            "title": worker_title,
            "model": _effective_model,
        },
    )

    return f'Worker spawned: {worker_id} — "{worker_title}"'


def _worker_has_output(wid: str) -> bool:
    """Check if a worker produced assistant messages."""
    messages = db.get_messages(wid)
    return any(m["role"] == "assistant" and m.get("content") for m in messages)


def check_workers(_context: dict | None = None, _filter_ids: list | None = None) -> str:
    """Check status of all workers spawned by this session.

    _filter_ids: optional allow-list of worker IDs to include; defaults to all.
    """
    ctx = _context or {}
    parent_id = ctx.get("session_id", "")
    if not parent_id:
        return "Error: No session context"

    from sessions.manager import get_manager

    manager = get_manager()
    parent = manager.get(parent_id)
    if not parent:
        return "Error: Session not found in memory"

    if not parent.worker_ids:
        return "No workers spawned."

    from sessions import state_v2 as sv2

    lines = []
    done = 0
    failed = 0
    empty = 0
    filter_set = set(_filter_ids) if _filter_ids is not None else None
    wid_list = [w for w in parent.worker_ids if filter_set is None or w in filter_set]
    for wid in wid_list:
        worker_obj = manager.get(wid)
        title = db.get_session(wid).get("title", "?") if db.get_session(wid) else "?"

        # v2 state is authoritative. IDLE_READY means "no active turn."
        # But a just-created worker is also IDLE_READY before its first
        # transition fires — distinguish via task (set by manager.prompt).
        # Status payload doesn't carry v2 state directly, so read in-memory.
        if worker_obj is None:
            v2 = sv2.SessionStateV2.IDLE_READY
            idle = 0
            has_started = False
        else:
            v2 = sv2._current_state(worker_obj)
            idle = int(worker_obj.idle_seconds)
            # Only `_turn_id > 0` truly means a turn ran. AgentSession.task
            # is set the moment run_coroutine_threadsafe schedules the
            # task; using it here would mis-classify a freshly-spawned
            # worker (Task scheduled but not yet executed) as having
            # started, which the await_workers polling loop then treats
            # as "done" if it happens to be in IDLE_READY momentarily.
            has_started = getattr(worker_obj, "_turn_id", 0) > 0

        truly_done = (v2 is sv2.SessionStateV2.IDLE_READY) and has_started
        if truly_done:
            done += 1

        # Build diagnostic status
        parts = [v2.value]
        if v2 is sv2.SessionStateV2.FINALIZING:
            parts.append("finalizing (reflect/post-hooks)")
        elif v2 is sv2.SessionStateV2.AWAITING_USER:
            parts.append("waiting on user answer")
        elif v2 is sv2.SessionStateV2.PAUSED:
            parts.append("paused")
        elif v2 is sv2.SessionStateV2.CANCELLING:
            parts.append("cancelling")
        elif v2 is sv2.SessionStateV2.COMPACTING:
            parts.append("compacting context")
        elif v2 is sv2.SessionStateV2.IDLE_READY and not has_started:
            parts.append("queued (not yet started)")
        if worker_obj and worker_obj.error:
            parts.append(f"ERROR: {worker_obj.error[:100]}")
            failed += 1
        elif truly_done and not _worker_has_output(wid):
            parts.append("WARNING: no output produced")
            empty += 1
        if v2 not in (sv2.SessionStateV2.IDLE_READY, sv2.SessionStateV2.AWAITING_USER):
            parts.append(f"idle {idle}s")

        lines.append(f"- {wid[:8]} \"{title}\": {' | '.join(parts)}")

    header = f"Workers: {done}/{len(wid_list)} done"
    if failed:
        header += f", {failed} FAILED"
    if empty:
        header += f", {empty} empty (no output)"

    result_text = header + "\n" + "\n".join(lines)

    # Cross-pollinate completed worker findings to running siblings
    if done > 0 and done < len(parent.worker_ids):
        try:
            xp = cross_pollinate(_context=_context)
            if "Cross-pollinated" in xp:
                result_text += f"\n{xp}"
        except Exception as e:
            logger.debug("Cross-pollination skipped: %s", e)

    return result_text


def _latest_reflect(worker_id: str) -> dict | None:
    """Return the worker's most recent reflect row parsed to dict, or None.

    Reflect is the quality gate: if it didn't run, or didn't verdict 'pass',
    the worker's output should be served as UNVERIFIED so the parent can
    decide whether to trust it or inspect the full transcript.
    """
    try:
        messages = db.get_messages(worker_id)
    except Exception as e:
        # Silent failures here surface later as verdict='unknown' in the
        # manifest with no breadcrumb pointing back to the DB read. Log it.
        logger.warning("_latest_reflect: db.get_messages(%s) failed: %s", worker_id, e)
        return None
    for m in reversed(messages):
        if m.get("role") == "reflect":
            try:
                return json.loads(m.get("content") or "{}")
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning(
                    "_latest_reflect: malformed reflect row for worker %s: %s",
                    worker_id,
                    e,
                )
                return None
    return None


def _recover_output_from_worker_writes(worker_id: str, expected_basename: str) -> Path | None:
    """Find a file the worker actually wrote that matches the expected basename.

    When verdict=pass but the run-dir gate file is missing, the worker may
    have written the deliverable to a different path (e.g. an archive
    directory). Scan the worker's tool messages for file_write calls and
    return the first path whose basename matches expected_basename and
    whose file exists with substantive content (>100 bytes).

    Returns None if no match is found or the matched path no longer exists.
    Best-effort: failures are logged at debug — caller falls back to the
    normal pass-but-no-output failed branch.
    """
    if not expected_basename:
        return None
    target_basename = Path(expected_basename).name
    try:
        messages = db.get_messages(worker_id)
    except Exception as e:
        logger.debug("_recover_output_from_worker_writes: db read failed for %s: %s", worker_id, e)
        return None

    # Walk newest-first to prefer the most recent write.
    for msg in reversed(messages):
        if msg.get("role") != "tool":
            continue
        content = msg.get("content") or ""
        if "file_write" not in content and target_basename not in content:
            continue
        # Tool messages often include "Wrote N bytes to <path>" or similar.
        # Match a path whose basename equals our target.
        import re as _re

        for path_match in _re.finditer(r"(/[^\s\"'<>\n]+)", content):
            candidate = Path(path_match.group(1))
            if candidate.name != target_basename:
                continue
            if candidate.exists() and candidate.stat().st_size > 100:
                return candidate
    return None


def _recover_reflect_verdict(worker_id: str, ctx: dict | None) -> dict | None:
    """Synchronously re-run reflect for a worker whose post-hook reflect was lost.

    `_finalize_step` calls this when `_latest_reflect()` comes back empty —
    typically because reflect crashed inside the post-hook (sessions/hooks.py
    swallows the exception silently) or the worker terminated before reflect
    could run. We re-invoke reflect_on_session against the worker's transcript
    and persist the result as a fresh reflect row tagged with `_recovered=True`.

    Runs sync via the parent's event loop (workflow execution is on a worker
    thread). Returns the parsed reflect dict on success, or None if recovery
    itself failed — caller decides what to do with that.
    """
    try:
        from core.reflect import reflect_on_session
    except Exception as e:
        logger.warning("_recover_reflect_verdict: import failed: %s", e)
        return None

    loop = (ctx or {}).get("_loop")
    try:
        if loop is not None:
            future = asyncio.run_coroutine_threadsafe(
                reflect_on_session(worker_id),
                loop,
            )
            # 120s cap — reflect is bounded by max_tokens=2048, but the LLM
            # call itself can stall under load. We'd rather fall back to the
            # output-file check than block the workflow indefinitely.
            result = future.result(120)
        else:
            # No event loop available (sync test path or detached call). Spin
            # up a temporary one — won't happen in production workflow runs.
            result = asyncio.run(reflect_on_session(worker_id))
    except Exception as e:
        # Always log the exception class — TimeoutError() and CancelledError()
        # both stringify to '', which previously left lines reading literally
        # "...failed for X: " with no clue what went wrong.
        logger.warning(
            "_recover_reflect_verdict: reflect_on_session failed for %s: %s: %s",
            worker_id,
            type(e).__name__,
            e or "(no message)",
        )
        return None

    if result is None:
        return None

    reflect_event = {
        "verdict": result.verdict,
        "reasoning": result.reasoning,
        "diagnostic": result.diagnostic,
        "what_worked": result.what_worked,
        "what_failed": result.what_failed,
        "strategy": result.strategy,
        "missing": result.missing,
        "failure_cause": result.failure_cause,
        "confidence": result.confidence,
        "latency_ms": result.reflect_latency_ms,
        "_recovered": True,
    }
    try:
        db.add_message(worker_id, "reflect", json.dumps(reflect_event))
    except Exception as e:
        # Recovery still informs the manifest even if persist fails — the
        # next _latest_reflect() lookup just won't see it.
        logger.warning(
            "_recover_reflect_verdict: failed to persist recovered reflect for %s: %s",
            worker_id,
            e,
        )

    logger.info(
        "_recover_reflect_verdict: worker %s recovered verdict=%s",
        worker_id,
        result.verdict,
    )
    return reflect_event


def get_worker_result(worker_id: str, _context: dict | None = None) -> str:
    """Get the final output from a completed worker.

    Quality gate: if the worker's latest reflect verdict is not 'pass' (or no
    reflect ran), the returned content is wrapped in an UNVERIFIED/ESCALATED
    header with the reflect reasoning, so the parent knows the work needs
    another look or a transcript scan.
    """
    from pathlib import Path

    workspace = Path(settings.workspace_dir)

    # Reflect verdict — the quality gate. Used by every return path below.
    reflect = _latest_reflect(worker_id)
    verdict = (reflect or {}).get("verdict")
    reflect_reason = (reflect or {}).get("reasoning", "")

    # Check worker termination state (cancelled, errored, round-capped).
    from sessions.manager import get_manager as _get_mgr

    _worker_obj = _get_mgr().get(worker_id)
    term_reason = _worker_obj.termination_reason if _worker_obj else None

    def _gate_header() -> str:
        """Build a header that reflects the worker's trust state. Empty when
        reflect passed cleanly — everything else gets a prefix the parent
        cannot miss.
        """
        if verdict == "pass":
            return ""
        if verdict == "escalate":
            return (
                f"# ESCALATED (worker reflect: verdict=escalate)\n"
                f"# Reason: {reflect_reason or '(no reasoning provided)'}\n"
                f"# Consider get_worker_transcript({worker_id[:12]!r}) to inspect "
                f"the full work stream before trusting this output.\n\n"
            )
        if verdict == "retry":
            return (
                f"# UNVERIFIED (worker reflect: verdict=retry, retries exhausted)\n"
                f"# Reason: {reflect_reason or '(no reasoning provided)'}\n\n"
            )
        # No reflect row (cancelled / crashed before post-hooks)
        if term_reason == "cancelled":
            return "# CANCELLED (worker stopped before reflect ran)\n\n"
        if term_reason in ("error", "round_ceiling", "compaction_failed"):
            return f"# INCOMPLETE (worker terminated: {term_reason})\n\n"
        return "# UNVERIFIED (no reflect verdict recorded — quality not gated)\n\n"

    def _cap(full_text: str) -> str:
        """Truncate to 3000 chars WITH a visible marker — silently cutting a
        worker's report mid-sentence left the parent with no signal that
        content was lost or where to find the rest."""
        if len(full_text) <= 3000:
            return full_text
        return (
            full_text[:3000] + f"\n[truncated at 3000 of {len(full_text)} chars — call "
            f"get_worker_transcript({worker_id[:12]!r}) for the full output]"
        )

    # Exact sentinel prefixes _finalize_worker stamps. Matching any leading
    # "#" here suppressed the quality gate whenever a worker began its own
    # summary with a markdown heading ("# Results") — unverified output was
    # then returned to the parent as trusted.
    _SENTINELS = (
        "# INCOMPLETE (",
        "# CANCELLED (",
        "# ERROR (",
        "# ESCALATED (",
        "# UNVERIFIED (",
        "# AUTO-STAMPED (",
    )

    # Per-worker summary file (new convention). The file itself is trusted
    # (either written by the worker, or auto-stamped with a marker header).
    per_worker = workspace / f".worker_{worker_id[:12]}_summary.md"
    if per_worker.exists():
        body = per_worker.read_text()
        # If the summary already carries a sentinel marker from _finalize_worker,
        # don't double up — just return as-is (the stamp already encodes state).
        if body.startswith(_SENTINELS):
            return _cap(body)
        return _gate_header() + _cap(body)

    # Backward compat: shared summary.md from pre-fix workers
    legacy_path = workspace / "summary.md"
    if legacy_path.exists():
        return _gate_header() + _cap(legacy_path.read_text())

    # Fallback: last assistant message, always wrapped in a quality header.
    messages = db.get_messages(worker_id)
    for m in reversed(messages):
        if m["role"] == "assistant" and m.get("content"):
            return _gate_header() + _cap(m["content"])

    # No output at all
    if _worker_obj and _worker_obj.error:
        return f"Worker {worker_id[:8]} FAILED with error: {_worker_obj.error}. Consider retrying with retry_worker()."
    return f"Worker {worker_id[:8]} produced no output. It may have failed silently or timed out. Consider retrying with retry_worker()."


def get_worker_transcript(
    worker_id: str,
    include_tool_results: bool = True,
    max_chars: int = 30000,
    _context: dict | None = None,
) -> str:
    """Read the full message stream of a worker session.

    Safety valve for when get_worker_result returns an UNVERIFIED/ESCALATED
    summary: lets the parent scan what the worker actually did (assistant
    texts, tool calls, and tool results) and extract the real findings.

    Output format: one line per message, `[role] content`, truncated to
    max_chars characters with a trailing `[truncated]` marker when needed.
    """
    try:
        messages = db.get_messages(worker_id)
    except Exception as e:
        return f"Error reading worker {worker_id[:8]} messages: {e}"

    if not messages:
        return f"Worker {worker_id[:8]} has no messages."

    lines: list[str] = []
    for m in messages:
        role = m.get("role", "?")
        if role == "tool" and not include_tool_results:
            continue
        content = (m.get("content") or "").replace("\r", "")
        if role == "assistant":
            tc_raw = m.get("tool_calls")
            if tc_raw:
                try:
                    tcs = json.loads(tc_raw) if isinstance(tc_raw, str) else tc_raw
                    names = [
                        tc.get("name") or tc.get("function", {}).get("name", "?")
                        for tc in (tcs if isinstance(tcs, list) else [])
                    ]
                    if names:
                        lines.append(f"[assistant:tool_calls] {', '.join(names)}")
                except (json.JSONDecodeError, TypeError):
                    pass
            if content:
                lines.append(f"[assistant] {content}")
        elif role == "tool":
            # Truncate per-tool-result to avoid one huge output eating the budget
            lines.append(f"[tool] {content[:800]}")
        elif role == "reflect":
            try:
                r = json.loads(content)
                lines.append(f"[reflect] verdict={r.get('verdict')} " f"reasoning={r.get('reasoning','')[:200]}")
            except (json.JSONDecodeError, TypeError):
                lines.append(f"[reflect] {content[:200]}")
        elif role == "scout":
            try:
                r = json.loads(content)
                approach = r.get("approach") or r.get("approach_guidance") or ""
                lines.append(f"[scout] approach={approach[:300]}")
            except (json.JSONDecodeError, TypeError):
                lines.append(f"[scout] {content[:200]}")
        elif role == "system":
            lines.append(f"[system] {content[:300]}")
        elif role == "user":
            lines.append(f"[user] {content[:1000]}")

    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[:max_chars] + "\n[truncated]"
    return out


def await_workers(
    stale_threshold: int = 120,
    worker_ids: list | None = None,
    min_done: int = 0,
    suspend: bool = False,
    _context: dict | None = None,
) -> str:
    """Wait for workers to complete. By default blocks until all done (or timeout).

    worker_ids: optional list of specific worker IDs to watch. Defaults to all
        workers spawned by this session.
    min_done: if > 0, return as soon as at least this many workers have finished
        (Gap 3 — partial-completion unblock).
    suspend: if True, transition the parent session to AWAITING_WORKERS and exit
        the agent loop immediately. The parent will auto-resume (starting a new
        scout turn) once all watched workers complete. Use with spawn_worker
        auto_resume_parent=True for full async delegation (Gap 2).

    When suspend=False: blocks via polling (3s intervals, max 30 minutes).
    When suspend=True: returns immediately after registering the watch-set.
    """
    ctx = _context or {}
    parent_id = ctx.get("session_id", "")
    if not parent_id:
        return "Error: No session context"

    from sessions.manager import get_manager

    manager = get_manager()
    parent = manager.get(parent_id)
    if not parent:
        return "No workers to wait for."

    # Race #1 (worker_ids.append queued on event loop): spawn_worker uses
    # loop.call_soon_threadsafe to append to parent.worker_ids. The append
    # is scheduled, not executed synchronously. If the caller spawned
    # workers and called await_workers immediately on the same thread,
    # parent.worker_ids may still be empty when we read it. We drain any
    # pending callbacks via run_coroutine_threadsafe(sleep(0)).result so
    # the appends land before we proceed.
    loop = ctx.get("_loop")
    if worker_ids and loop and not all(wid in parent.worker_ids for wid in worker_ids):
        deadline = time.time() + 5.0
        while time.time() < deadline:
            try:
                asyncio.run_coroutine_threadsafe(
                    asyncio.sleep(0),
                    loop,
                ).result(2)
            except Exception:
                break
            if all(wid in parent.worker_ids for wid in worker_ids):
                break
            time.sleep(0.05)
    if not parent.worker_ids:
        return "No workers to wait for."

    # Race #2 (worker created with Task but turn hasn't started): when
    # the agent task is scheduled on the loop, AgentSession.task is set
    # to a Task object IMMEDIATELY (run_coroutine_threadsafe creates
    # the Task synchronously). But the task hasn't actually run yet, so
    # state is still IDLE_READY and _turn_id is still 0. The polling
    # loop's "done" check used to fire on `task is not None or _turn_id > 0`
    # — for a freshly-spawned worker, task != None made has_started True,
    # state IDLE_READY made the check trigger, the worker was marked
    # done before its first transition. Wave 1 of run fdfe1872 hit this:
    # select-videos worker spawned at 17:10:39, await_workers returned
    # ~2s later, _finalize_step ran on a worker with empty transcript,
    # reflect verdict=retry, retry exhausted, escalate.
    # We give the loop another short grace window to actually start the
    # agent tasks for the watched workers — mirroring how the drain above
    # handles the worker_ids.append race.
    if worker_ids and loop:
        deadline = time.time() + 5.0
        while time.time() < deadline:
            from sessions import state_v2 as sv2

            unstarted = []
            for wid in worker_ids:
                w = manager.get(wid)
                if w is None:
                    continue
                if sv2._current_state(w) is sv2.SessionStateV2.IDLE_READY and getattr(w, "_turn_id", 0) == 0:
                    unstarted.append(wid)
            if not unstarted:
                break
            try:
                asyncio.run_coroutine_threadsafe(
                    asyncio.sleep(0),
                    loop,
                ).result(2)
            except Exception:
                break
            time.sleep(0.05)

    # --- Suspend mode (Gap 2) -------------------------------------------
    if suspend:
        from core.events import call_on_loop
        from sessions import state_v2 as sv2

        # The ENTIRE suspend sequence runs as one callable on the event
        # loop. Two reasons: (1) transition() is loop-affine by contract;
        # (2) _on_watched_worker_done fires on the loop — if a watched
        # worker finished between this thread computing still_running and
        # registering the watch-set, its done-callback found an empty set
        # and never fired again, suspending the parent on a worker that
        # already completed (recovery only via the reaper, minutes later).
        # Running compute+register atomically on the loop closes that race.
        def _suspend_on_loop() -> str:
            # Idempotency guard: if the parent is already AWAITING_WORKERS,
            # a second call would cumulatively .update() new IDs into the
            # watch-set (potentially adding workers that were spawned for
            # a different purpose). Refuse rather than silently corrupt the
            # set — the LLM should call check_workers() and let the existing
            # suspension resolve.
            if sv2._current_state(parent) is sv2.SessionStateV2.AWAITING_WORKERS:
                already = len(getattr(parent, "_watched_worker_ids", set()))
                return (
                    f"Already suspended on {already} worker(s). The previous "
                    "await_workers(suspend=True) call is still pending — wait "
                    "for the parent to auto-resume rather than re-issuing."
                )
            target_ids: set = set(worker_ids) if worker_ids else set(parent.worker_ids)
            if not target_ids:
                return "Error: no worker IDs to watch"

            # Filter out already-completed workers. Completed workers have already
            # fired _on_watched_worker_done and will never do so again. Including them
            # in the watch-set permanently stalls it — the set never empties and the
            # parent never resumes.
            still_running: set = set()
            for wid in target_ids:
                w = manager.get(wid)
                if w is None:
                    continue  # reaped = done
                w_v2 = sv2._current_state(w)
                # See await_workers blocking-mode rationale: only `_turn_id > 0`
                # truly indicates a turn started. `w.task is not None` fires
                # too early (Task scheduled but not yet executed).
                has_started = getattr(w, "_turn_id", 0) > 0
                if w_v2 is sv2.SessionStateV2.IDLE_READY and has_started:
                    continue  # already done
                still_running.add(wid)

            already_done = len(target_ids) - len(still_running)
            if not still_running:
                return (
                    f"All {len(target_ids)} watched worker(s) have already completed. "
                    "Call get_worker_result() to retrieve their outputs."
                )

            # Register watch-set on the parent so _on_watched_worker_done can fire.
            parent._watched_worker_ids.update(still_running)
            manager._persist_watched(parent)
            # Transition PROCESSING → AWAITING_WORKERS so the agent loop exits cleanly.
            current_v2 = sv2._current_state(parent)
            if current_v2 is sv2.SessionStateV2.PROCESSING:
                sv2.transition(parent, sv2.SessionStateV2.AWAITING_WORKERS, "workers-dispatched")
            done_note = f" ({already_done} already completed)" if already_done else ""
            return (
                f"Session suspended — watching {len(still_running)} worker(s){done_note}. "
                "Parent will auto-resume with get_worker_result() context once all finish."
            )

        return call_on_loop(_suspend_on_loop, loop=ctx.get("_loop"))

    # --- Blocking poll mode (existing behavior + Gap 3 enhancements) -----
    loop = ctx.get("_loop")
    max_wait = 1800  # 30 minutes
    start = time.time()
    effective_min_done = max(0, min_done)

    # Touch the parent session so its idle_seconds reflects that work IS
    # happening, even though there's no user/LLM activity. The reaper
    # otherwise treats this PROCESSING session as stuck after 300s and
    # transitions it to IDLE_READY mid-workflow. (run_workflow also takes
    # an explicit background ref — this is the secondary defense and keeps
    # diagnostic queries that read idle_seconds honest.)
    parent.touch()

    while time.time() - start < max_wait:
        # Snapshot to avoid RuntimeError from concurrent event-loop appends.
        if worker_ids:
            worker_snapshot = [wid for wid in worker_ids if wid in parent.worker_ids]
        else:
            worker_snapshot = list(parent.worker_ids)

        from sessions import state_v2 as sv2

        done_count = 0
        pending_count = 0
        stalled = []
        # Stale detection only applies to states where the worker is in an
        # interactive tool-call/LLM-streaming loop. Post-terminal states
        # (FINALIZING runs reflect/eval/distill, COMPACTING runs context
        # compression, CANCELLING is awaiting graceful shutdown) consist of
        # bounded LLM calls that legitimately take 60-180s without bumping
        # last_activity_time. A 120s stale threshold there caused the
        # workflow engine to abandon a worker mid-reflect, leaving
        # `_finalize_step` to read verdict='unknown' and stamp the manifest
        # `failed` even though reflect would land 'pass' moments later.
        # (Repro: workflow run 024c370f, 2026-04-26 — crawl-subs.)
        # AWAITING_USER is also gated: in a workflow / cron context there
        # is no human who can answer ask_user, so a worker that hits this
        # state would otherwise stall the entire wave for max_wait (30 min)
        # waiting for an answer that never comes. The workflow author's
        # intent is documented (see ai-tech-daily-brief WORKFLOW.md
        # notify-user step) but workers don't always honor it. Treating
        # AWAITING_USER as stale lets the orchestrator finalize the step
        # after the threshold rather than hanging indefinitely.
        STALE_GATED_STATES = (
            sv2.SessionStateV2.PROCESSING,
            sv2.SessionStateV2.SCOUTING,
            sv2.SessionStateV2.AWAITING_USER,
        )
        for wid in worker_snapshot:
            w = manager.get(wid)
            if w is None:
                done_count += 1  # reaped = done
                continue
            v2 = sv2._current_state(w)
            # Has the worker actually completed at least one turn?
            # Previously this was `task is not None or _turn_id > 0`, but
            # AgentSession.task is set IMMEDIATELY when run_coroutine_threadsafe
            # schedules the agent task on the loop — before the task has
            # actually run to transition the state. So between spawn and
            # first transition (typically <100ms but real), a worker briefly
            # appears as `IDLE_READY + has_started=True` and is wrongly
            # marked done. Only `_turn_id > 0` truly indicates a turn ran.
            # (Real failure: run fdfe1872 wave 1 select-videos, 2026-04-27 —
            # finalized 2s after spawn, escalated, halted the workflow.)
            has_started = getattr(w, "_turn_id", 0) > 0
            # Terminal: IDLE_READY after at least one turn started.
            # AWAITING_USER is explicitly NOT terminal.
            if v2 is sv2.SessionStateV2.IDLE_READY and has_started:
                done_count += 1
                continue
            pending_count += 1
            if v2 in STALE_GATED_STATES:
                idle = int(w.idle_seconds)
                if idle > stale_threshold:
                    stalled.append(wid)

        all_done = pending_count == 0
        min_satisfied = effective_min_done > 0 and done_count >= effective_min_done

        if all_done or min_satisfied:
            fail_ids = []
            empty_ids = []
            for wid in worker_snapshot:
                w = manager.get(wid)
                if w and w.error:
                    fail_ids.append(wid[:8])
                elif not _worker_has_output(wid):
                    empty_ids.append(wid[:8])
            result = check_workers(_context=_context, _filter_ids=worker_snapshot)
            if min_satisfied and not all_done:
                result = f"[Partial: {done_count}/{len(worker_snapshot)} done]\n" + result
            if fail_ids:
                result += f"\n⚠ {len(fail_ids)} FAILED: {', '.join(fail_ids)} — use retry_worker() to retry."
            if empty_ids:
                result += f"\n⚠ {len(empty_ids)} produced NO OUTPUT: {', '.join(empty_ids)} — check errors or retry."
            return result

        if stalled:
            # Only abandon the wave when every pending worker is stalled.
            # Previously a single stalled wave-mate caused the whole wait to
            # return early, which made the orchestrator finalize all eligible
            # steps — including healthy workers that were still actively
            # producing output. That triggered redundant ~100s recovery
            # reflects on those healthy workers and ultimately tore them
            # down. Healthy workers should keep being awaited; only stalled
            # ones get reported.
            healthy_pending = pending_count - len(stalled)
            if healthy_pending <= 0:
                return (
                    f"Warning: all {len(stalled)} pending worker(s) appear "
                    f"stalled (idle > {stale_threshold}s).\n" + check_workers(_context=_context)
                )
            # Log once per minute so we surface the stalled worker without
            # spamming. The orchestrator's _finalize_step path will inspect
            # each worker's actual state when this wait ultimately returns.
            if not hasattr(parent, "_await_stalled_logged_at"):
                parent._await_stalled_logged_at = 0.0
            now_ts = time.time()
            if now_ts - parent._await_stalled_logged_at > 60:
                logger.warning(
                    "await_workers: %d/%d worker(s) stalled (>%ds idle): "
                    "%s — continuing to wait on %d healthy peer(s).",
                    len(stalled),
                    pending_count,
                    stale_threshold,
                    ", ".join(s[:8] for s in stalled),
                    healthy_pending,
                )
                parent._await_stalled_logged_at = now_ts

        if loop:
            # The sleep coroutine runs on the event loop; the .result() wait
            # blocks this worker thread until it completes. The timeout cushion
            # has to absorb genuine event-loop pressure (sibling worker scouts
            # doing sync DB I/O / prompt construction during their first slice
            # before yielding) — a tight 3.5s cushion would surface as
            # `concurrent.futures.TimeoutError` (whose str() is empty, so it
            # propagated out as a literal "Error: " from the tool dispatcher and
            # orphaned the workflow). 30s is a generous cap that still falls
            # well inside the outer max_wait loop. If even 30s is exceeded the
            # loop is genuinely wedged; fall back to a thread-side sleep so the
            # workflow doesn't die — the next iteration will re-check workers.
            try:
                asyncio.run_coroutine_threadsafe(asyncio.sleep(3), loop).result(30)
            except TimeoutError:
                logger.warning(
                    "await_workers: event-loop sleep exceeded 30s — loop "
                    "appears wedged. Falling back to thread sleep."
                )
                time.sleep(3)
        else:
            time.sleep(3)
        # Refresh activity stamp every poll so the reaper's idle_seconds
        # check stays current. (Backup to the background_ref protection in
        # run_workflow — keeps non-workflow callers covered too.)
        parent.touch()

    return f"Timeout after {max_wait}s.\n" + check_workers(_context=_context)


def message_worker(worker_id: str, message: str, _context: dict | None = None) -> str:
    """Send a fire-and-forget message to a worker.

    Routing is state-aware:
      * IDLE_READY or AWAITING_USER → `manager.prompt()` — starts (or queues
        into) a new turn. For AWAITING_USER this behaves like an answer,
        chained via parent_turn_id by the state machine.
      * SCOUTING/PROCESSING/COMPACTING/PAUSE_REQUESTED/PAUSED/FINALIZING →
        `inject_user_message()` — appends a `user` row to `db.messages` so
        the worker sees the message on its next `compile_context` call
        without queuing a new turn. This is the supervisor-injection pattern
        `docs/workflow.md` describes; previously it was always queued.
      * CANCELLING → rejected with a clear error; the user must re-prompt
        after cancel completes.
    """
    from sessions import state_v2 as sv2
    from sessions.manager import get_manager

    manager = get_manager()

    worker = manager.get(worker_id)
    if worker is None:
        return f"Error: Worker {worker_id[:8]} not in memory"

    current = sv2._current_state(worker)

    if current is sv2.SessionStateV2.CANCELLING:
        return f"Refused: worker {worker_id[:8]} is cancelling. " f"Wait for IDLE_READY and retry."

    # Start a new turn when the worker is idle-like.
    if current in (sv2.SessionStateV2.IDLE_READY, sv2.SessionStateV2.AWAITING_USER):
        ctx = _context or {}
        try:
            loop = ctx.get("_loop") or asyncio.get_running_loop()
        except RuntimeError:
            return "Error: No event loop"
        asyncio.run_coroutine_threadsafe(manager.prompt(worker_id, message), loop)
        return f"Message sent to worker {worker_id[:8]} (will start new turn)"

    # Mid-turn inject: write a user row that the compiler will pick up on
    # the next tool round. No state transition — this is not a new turn.
    db.add_message(worker_id, "user", message)
    worker.emit_event(
        {
            "type": "message.injected",
            "source": "message_worker",
            "preview": message[:120],
        }
    )
    return f"Injected message into worker {worker_id[:8]} " f"(state={current.value}; visible next tool round)"


def cancel_worker(worker_id: str, _context: dict | None = None) -> str:
    """Cancel a running worker."""
    from sessions.manager import get_manager

    manager = get_manager()
    session = manager.get(worker_id)
    if not session:
        return f"Worker {worker_id} not found in memory"
    if session.task and not session.task.done():
        session.task.cancel()
        return f"Worker {worker_id[:8]} cancelled"
    return f"Worker {worker_id[:8]} is not running"


def pause_worker(worker_id: str, _context: dict | None = None) -> str:
    """Pause a worker at its next pre-round checkpoint.

    Clears the pause_event (the cooperative signal) and transitions the v2
    state to PAUSE_REQUESTED. The worker's agent loop observes the cleared
    event at `core/agent.py:341` and transitions PAUSE_REQUESTED → PAUSED
    before blocking on `await session.pause_event.wait()`. Pause does not
    interrupt a tool already in flight.
    """
    from db import models as _m
    from sessions import state_v2 as sv2
    from sessions.manager import get_manager

    session = get_manager().get(worker_id)
    if not session:
        return f"Worker {worker_id} not found"

    # State read + pause_event.clear + transition run as one loop callable —
    # this tool executes on a worker thread, and transition() is loop-affine.
    def _pause_on_loop() -> str:
        current = sv2._current_state(session)
        if current is not sv2.SessionStateV2.PROCESSING:
            return f"Worker {worker_id[:8]} is in state {current.value}; " f"pause only applies to PROCESSING workers"
        session.pause_event.clear()
        try:
            sv2.transition(session, sv2.SessionStateV2.PAUSE_REQUESTED, "pause-requested")
        except Exception as e:
            logger.error("pause-requested transition failed for %s: %s", worker_id, e)
        return f"Worker {worker_id[:8]} will pause at next checkpoint"

    from core.events import call_on_loop

    return call_on_loop(_pause_on_loop, loop=(_context or {}).get("_loop"))


def resume_worker(worker_id: str, _context: dict | None = None) -> str:
    """Resume a paused (or pause-requested) worker.

    Sets the pause_event. If the worker has already reached PAUSED, it will
    transition back to PROCESSING at its own pace (via the agent loop's
    resume branch). If still in PAUSE_REQUESTED (pause never observed),
    transition back directly here.
    """
    from db import models as _m
    from sessions import state_v2 as sv2
    from sessions.manager import get_manager

    session = get_manager().get(worker_id)
    if not session:
        return f"Worker {worker_id} not found"

    # Loop-marshaled for the same reason as pause_worker: transition() is
    # loop-affine, and setting pause_event must not interleave with the
    # agent loop's own PAUSE_REQUESTED→PAUSED observation.
    def _resume_on_loop() -> str:
        session.pause_event.set()
        current = sv2._current_state(session)
        if current is sv2.SessionStateV2.PAUSE_REQUESTED:
            try:
                sv2.transition(session, sv2.SessionStateV2.PROCESSING, "resume")
            except Exception as e:
                logger.error("resume (from pause-requested) failed for %s: %s", worker_id, e)
        # If current == PAUSED, the agent loop will transition on its own.
        return f"Worker {worker_id[:8]} resumed"

    from core.events import call_on_loop

    return call_on_loop(_resume_on_loop, loop=(_context or {}).get("_loop"))


def set_worker_state(worker_id: str, paused: bool, _context: dict | None = None) -> str:
    """Pause or resume a worker. Pass paused=true to pause, paused=false to resume."""
    if paused:
        return pause_worker(worker_id, _context=_context)
    return resume_worker(worker_id, _context=_context)


def retry_worker(
    worker_id: str,
    new_instructions: str = "",
    reason: str = "",
    _context: dict | None = None,
) -> str:
    """Retry a failed worker with fresh context. Spawns a replacement."""
    ctx = _context or {}
    parent_id = ctx.get("session_id", "")

    # Get old worker's output
    old_output = get_worker_result(worker_id)[:2000]
    old_session = db.get_session(worker_id)
    old_title = old_session.get("title", "worker") if old_session else "worker"

    # Cancel old worker
    cancel_worker(worker_id)

    # Build retry task
    task = "[Retry of previous worker that failed]\n"
    if reason:
        task += f"Reason for retry: {reason}\n"
    if old_output:
        task += f"Previous output:\n{old_output}\n"
    if new_instructions:
        task += f"\nNew instructions: {new_instructions}\n"
    else:
        task += "\nPlease try again with a different approach.\n"

    return spawn_worker(task, title=f"Retry: {old_title}", _context=_context)


def cross_pollinate(_context: dict | None = None) -> str:
    """Share completed worker findings with still-running siblings.

    LogAct-inspired supervisor pattern: when one worker discovers a solution,
    propagate it to other active workers so they don't rediscover the same thing.
    Triggered on natural checkpoints (check_workers / await_workers), not polling.

    Delivery: injects a system message into the running worker's message history
    so it appears in the next context compilation. Uses session_messages table
    to track what was already sent (dedup).

    NOTE: cross_pollinate writes directly to `session_messages` (memory/DB only).
    It does NOT go through `manager.prompt()`, does NOT trigger a new turn, and
    does NOT produce a state_v2 transition. A future maintainer wondering why
    a message appears in a worker's history without a corresponding
    session.state_changed event should look here first.
    """
    ctx = _context or {}
    parent_id = ctx.get("session_id", "")
    if not parent_id:
        return "Error: No session context"

    from sessions.manager import get_manager

    manager = get_manager()
    parent = manager.get(parent_id)
    if not parent or len(parent.worker_ids) < 2:
        return "Cross-pollination requires 2+ workers."

    # Classify workers: completed (with output) vs still running
    completed = []
    running = []
    for wid in parent.worker_ids:
        status = manager.get_status(wid)
        state = status.get("status", "unknown")
        if state in ("idle", "unknown") and _worker_has_output(wid):
            completed.append(wid)
        elif state in ("scouting", "processing"):
            running.append(wid)

    if not completed or not running:
        return "No cross-pollination needed (no completed+running workers simultaneously)."

    # Track what we've already cross-pollinated (source_worker → set of recipient_workers)
    # Use session_messages table as the dedup ledger
    from db.models import connect_sessions

    already_sent: set[tuple[str, str]] = set()
    with connect_sessions() as conn:
        rows = conn.execute(
            """SELECT sender_id, recipient_id FROM session_messages
               WHERE message_type = 'cross_pollinate'
               AND sender_id IN ({})""".format(",".join("?" * len(completed))),
            completed,
        ).fetchall()
        for r in rows:
            already_sent.add((r["sender_id"], r["recipient_id"]))

    # Extract key findings from completed workers and deliver to running ones.
    # Quality gate: only cross-pollinate from workers whose reflect verdict was
    # 'pass'. An escalated / retry / missing reflect means the work isn't
    # trusted — broadcasting it poisons siblings (real case: a preamble-only
    # worker was cross-pollinated and seeded confusion in parallel workers).
    sent_count = 0
    for cwid in completed:
        reflect = _latest_reflect(cwid)
        if not reflect or reflect.get("verdict") != "pass":
            logger.info(
                "Skipping cross-pollination from worker %s — reflect verdict=%s " "(only 'pass' is propagated)",
                cwid[:8],
                (reflect or {}).get("verdict"),
            )
            continue

        # Get a brief summary of the completed worker's output
        result = get_worker_result(cwid)
        if not result or result.startswith("Worker "):
            continue  # No useful output or error message

        session_info = db.get_session(cwid)
        title = session_info.get("title", "worker") if session_info else "worker"
        summary = result[:300]

        for rwid in running:
            if (cwid, rwid) in already_sent:
                continue

            finding_msg = (
                f'[Sibling worker finding — "{title}"]\n{summary}\n'
                f"Use this context if relevant to your task. Ignore if not applicable."
            )

            # Inject as a system message into the running worker's conversation
            db.add_message(rwid, "system", finding_msg)

            # Record in session_messages for dedup tracking
            db.send_session_message(
                sender_id=cwid,
                recipient_id=rwid,
                message_type="cross_pollinate",
                payload=title,
            )
            sent_count += 1

    if sent_count:
        return f"Cross-pollinated {sent_count} finding(s) from {len(completed)} completed worker(s) to {len(running)} running worker(s)."
    return "No new findings to cross-pollinate."


def notify_parent(
    message: str = "",
    findings_summary: str = "",
    _context: dict | None = None,
) -> str:
    """Push a message from this worker up to the parent session (Gap 4).

    Routing is state-aware (mirrors message_worker in reverse):
      * Parent IDLE_READY / AWAITING_WORKERS / AWAITING_USER → manager.prompt()
        starts a new turn with the notification as its message.
      * Parent mid-turn (SCOUTING/PROCESSING/etc.) → direct DB injection so the
        parent sees it on its next compile_context call without a new turn.
      * No parent → returns an error.

    Only callable from worker sessions (not in any denied_session_types set).
    """
    ctx = _context or {}
    session_id = ctx.get("session_id", "")

    from sessions.manager import get_manager

    manager = get_manager()
    worker_obj = manager.get(session_id)
    if not worker_obj or worker_obj.session_type != "worker":
        return "Error: notify_parent can only be called from a worker session"

    parent_id = worker_obj.parent_session_id
    if not parent_id:
        return "Error: this worker has no parent session"
    parent = manager.get(parent_id)
    if not parent:
        return f"Error: parent session {parent_id[:8]} not found (may have been reaped)"

    full_msg = message
    if findings_summary:
        full_msg = f"[Worker {session_id[:8]} notification]\n" f"{message}\n\n" f"Findings: {findings_summary}"

    from sessions import state_v2 as sv2

    parent_v2 = sv2._current_state(parent)
    loop = ctx.get("_loop")

    idle_states = (
        sv2.SessionStateV2.IDLE_READY,
        sv2.SessionStateV2.AWAITING_WORKERS,
        sv2.SessionStateV2.AWAITING_USER,
    )
    if parent_v2 in idle_states:
        if not loop:
            return "Error: no event loop in context — cannot schedule parent prompt"
        asyncio.run_coroutine_threadsafe(manager.prompt(parent_id, full_msg), loop)
        return f"Notification sent to parent {parent_id[:8]} (will start new turn)"
    else:
        # Mid-turn injection — visible on next compile_context
        db.add_message(parent_id, "user", full_msg)
        parent.emit_event(
            {
                "type": "message.injected",
                "source": "notify_parent",
                "worker_id": session_id,
                "preview": full_msg[:120],
            }
        )
        return f"Message injected into parent {parent_id[:8]} (visible next tool round)"


def _build_workflow_context_block(
    workflow_name: str,
    run_id: str,
    run_dir: str,
    current_step_id: str,
    steps_info: list[dict],
) -> str:
    """Build the [WORKFLOW CONTEXT] block injected into every worker's task."""
    lines = [
        "[WORKFLOW CONTEXT]",
        f"Workflow: {workflow_name} (run_id: {run_id})",
        f"Your step: {current_step_id}",
        f"Run directory: {run_dir}",
        f"Manifest: {run_dir}/manifest.json",
        "",
        "All steps in this workflow:",
    ]
    for info in steps_info:
        sid = info["id"]
        wid = info.get("worker_id", "")
        status = info.get("status", "pending")
        output_file = info.get("output_file", "")
        if sid == current_step_id:
            marker = "→"
            desc = f"{sid} (worker: {wid[:12] if wid else 'this worker'}) — this worker (you)"
        elif status == "complete":
            marker = "✓"
            desc = f"{sid} (worker: {wid[:12] if wid else '?'}) — complete"
            if output_file:
                desc += f", output: {output_file}"
        elif status == "running":
            marker = "⟳"
            desc = f"{sid} (worker: {wid[:12] if wid else '?'}) — running"
        else:
            marker = "○"
            desc = f"{sid} — pending (not yet started)"
        lines.append(f"  {marker} {desc}")

    lines += [
        "",
        "You may call get_worker_transcript(worker_id) to read a completed sibling's full output.",
        "You may call message_worker(worker_id, message) to ask a running sibling a question.",
    ]
    return "\n".join(lines)


_WORKER_ID_RE = __import__("re").compile(r"^Worker spawned:\s+(\S+)")

# Default retry budget for steps whose worker returns verdict="retry". Steps can
# override by declaring `max_retries: N` on the step in WORKFLOW.md — parser
# currently drops unknown fields, but we read it defensively below.
_DEFAULT_MAX_RETRIES = 1


def _emit_workflow_event(event: dict, session=None) -> None:
    """Emit a workflow.* event to the global bus and optionally to a session stream."""
    try:
        from core.events import get_event_bus

        get_event_bus().emit(event)
    except Exception as e:
        logger.debug("Failed to emit workflow event to global bus: %s", e)
    if session is not None:
        try:
            session.emit_event(event)
        except Exception as e:
            logger.debug("Failed to emit workflow event to session: %s", e)


def _build_step_task(
    wf,
    step,
    manifest: dict,
    run_dir: Path,
    workflow_name: str,
    run_id: str,
    inputs: str,
) -> tuple[str, str]:
    """Build (task_description, worker_title) for a workflow step.

    Inputs are injected into every step's task body (not just first-wave steps)
    so downstream workers can reference the original user inputs without having
    to plumb them through output files.
    """
    steps_info = [
        {
            "id": s.id,
            "worker_id": manifest["steps"][s.id].get("worker_id") or "",
            "status": manifest["steps"][s.id].get("status", "pending"),
            "output_file": s.output_file,
        }
        for s in wf.steps
    ]
    # Use the absolute path in all context strings so workers can use it
    # unambiguously in both bash (cwd=workspace) and file_read/file_write tool
    # calls (which resolve from the project root). The relative run_dir Path
    # is kept as-is for the engine's own file-existence checks.
    abs_run_dir = run_dir.resolve()

    ctx_block = _build_workflow_context_block(
        workflow_name=workflow_name,
        run_id=run_id,
        run_dir=str(abs_run_dir),
        current_step_id=step.id,
        steps_info=steps_info,
    )

    run_dir_note = f"Run directory for all outputs: {abs_run_dir}/"
    output_path = f"{abs_run_dir}/{step.output_file}"

    if step.type == "skill" and step.skill:
        skill_line = f"Load and follow the '{step.skill}' skill."
        instructions_part = f"\n{step.instructions}" if step.instructions else ""
        task_body = f"{skill_line}{instructions_part}\n" f"{step.description}\n" f"Write your output to: {output_path}"
    else:
        instructions_part = step.instructions or step.description
        task_body = f"{instructions_part}\nWrite your output to: {output_path}"

    prior_outputs: list[str] = []
    for dep_id in step.depends_on:
        dep_step = next((s for s in wf.steps if s.id == dep_id), None)
        if dep_step:
            prior_outputs.append(f"{abs_run_dir}/{dep_step.output_file}")
    if prior_outputs:
        task_body += "\n\nPrior step outputs available at:\n" + "\n".join(f"  - {p}" for p in prior_outputs)

    if inputs:
        task_body += f"\n\nWorkflow inputs: {inputs}"

    task_description = f"{ctx_block}\n\n{run_dir_note}\n\n{task_body}"
    worker_title = f"[{workflow_name}] {step.id}: {step.description[:50]}"
    return task_description, worker_title


def _spawn_step(
    wf,
    step,
    manifest: dict,
    run_dir: Path,
    workflow_name: str,
    run_id: str,
    inputs: str,
    ctx: dict,
) -> str:
    """Spawn a worker for one step. Returns worker_id or empty string on failure.

    On spawn failure, writes the failure into the manifest (status=spawn_failed)
    so downstream dependency checks see it.
    """
    task_description, worker_title = _build_step_task(
        wf,
        step,
        manifest,
        run_dir,
        workflow_name,
        run_id,
        inputs,
    )
    # Per-step model override from WORKFLOW.md. Steps with `model: <name>`
    # in their frontmatter will spawn workers on that model instead of
    # the default llm_model. Useful for the few steps in a workflow that
    # need a stronger model (synthesize on a 122B) without paying for
    # the bigger model on every step.
    step_model = getattr(step, "model", "") or ""
    result = spawn_worker(
        task_description=task_description,
        title=worker_title,
        model=step_model,
        auto_resume_parent=False,
        _context=ctx,
    )
    if result.startswith("Error:") or result.startswith("Warning:"):
        logger.warning("Workflow '%s' step '%s' spawn failed: %s", workflow_name, step.id, result)
        manifest["steps"][step.id]["status"] = "spawn_failed"
        manifest["steps"][step.id]["spawn_error"] = result[:500]
        return ""
    m = _WORKER_ID_RE.match(result)
    if not m:
        logger.warning(
            "Workflow '%s' step '%s' spawn returned unrecognized result: %s", workflow_name, step.id, result[:200]
        )
        manifest["steps"][step.id]["status"] = "spawn_failed"
        manifest["steps"][step.id]["spawn_error"] = result[:500]
        return ""
    worker_id = m.group(1)
    manifest["steps"][step.id]["worker_id"] = worker_id
    manifest["steps"][step.id]["status"] = "running"
    return worker_id


def _upstream_ready(step, manifest: dict, run_dir: Path) -> tuple[bool, str]:
    """Check whether all of step.depends_on are complete with output files present.

    Returns (ready, reason_if_not). A dependency that is failed/skipped/
    spawn_failed, or that is "complete" but whose output file is missing, blocks
    the downstream step. Missing-output check catches confabulated passes.
    """
    for dep_id in step.depends_on:
        dep_info = manifest["steps"].get(dep_id, {})
        dep_status = dep_info.get("status", "pending")
        if dep_status != "complete":
            return False, f"dependency '{dep_id}' is {dep_status}"
        # Verify dep's output file actually exists — catches confabulated passes
        dep_output = dep_info.get("output_file")
        if dep_output and not (run_dir / dep_output).exists():
            return False, f"dependency '{dep_id}' has no output file at {dep_output}"
    return True, ""


def _finalize_step(
    step,
    worker_id: str,
    manifest: dict,
    run_dir: Path,
    ctx: dict | None = None,
) -> str:
    """Resolve a step's final status from its worker's reflect verdict + output file.

    Returns one of: "complete", "failed", "escalated". Writes status + verdict
    into the manifest dict (caller persists).

    Verdict resolution order:
      1. Read the worker's most recent reflect row (`_latest_reflect`).
      2. If verdict is missing/unknown, attempt one synchronous recovery pass
         via `_recover_reflect_verdict` (re-runs reflect against the worker's
         transcript). This catches cases where the worker's post-hook reflect
         was swallowed by an exception in sessions/hooks.py.
      3. If verdict is STILL unknown after recovery, fall back to file evidence:
         output_file present → status="complete", reflect_verdict="unknown-but-complete"
         (downstream steps unblock — work shipped even if we can't verify it);
         output_file missing → status="failed", failure_reason="no-verdict-no-output".
      4. Cancelled workers with output present remain "cancelled-but-complete"
         (existing behavior, preserved verbatim).
    """
    info = manifest["steps"][step.id]
    reflect = _latest_reflect(worker_id) or {}
    verdict = reflect.get("verdict", "unknown")

    # ── Recovery: re-run reflect when the post-hook lost its verdict ──
    # 'unknown' means no reflect row at all (post-hook never wrote one).
    # 'error' means hooks.py wrote a sentinel because reflect crashed —
    # don't bother re-running; it'll likely fail the same way. Both fall
    # through to the output-file fallback below.
    if verdict == "unknown":
        recovered = _recover_reflect_verdict(worker_id, ctx)
        if recovered:
            reflect = recovered
            verdict = recovered.get("verdict", "unknown")
            info["reflect_recovered"] = True

    info["reflect_verdict"] = verdict
    info["reflect_reasoning"] = (reflect.get("reasoning") or "")[:500]

    if verdict == "escalate":
        info["status"] = "escalated"
        return "escalated"

    if verdict == "pass":
        # verdict == "pass" — verify the output file actually exists.
        # A pass with no output catches workers that confabulate success; we flip
        # them to failed so downstream dependents short-circuit rather than reading
        # a non-existent file.
        output_file = step.output_file
        if output_file and not (run_dir / output_file).exists():
            # Recovery before failing: workers sometimes write the deliverable
            # to a different but reasonable path (e.g. an archive directory)
            # while skipping the manifest-gate location. The work IS done; we
            # don't want to fail the step because the agent picked a different
            # filename. Scan the worker's recent file_write tool calls — if
            # any wrote a file with the same basename and that file exists with
            # substantive content, copy it to the expected run-dir location
            # and treat the step as complete. (Real failure: workflow run
            # 25920bcb, 2026-04-27 — synthesize wrote ai_tech_brief.md to the
            # archive path but not to the run-dir; the brief was correct, but
            # the gate failed.)
            recovered_path = _recover_output_from_worker_writes(
                worker_id,
                output_file,
            )
            if recovered_path:
                target = run_dir / output_file
                try:
                    import shutil as _shutil

                    _shutil.copyfile(recovered_path, target)
                    logger.warning(
                        "Workflow step '%s' verdict=pass: %s missing in run "
                        "dir but found at %s (worker wrote it to a different "
                        "path); copied to gate location.",
                        step.id,
                        output_file,
                        recovered_path,
                    )
                    info["status"] = "complete"
                    info["reflect_verdict"] = "pass-after-recovery-copy"
                    info["recovery_source_path"] = str(recovered_path)
                    return "complete"
                except OSError as _ce:
                    logger.warning(
                        "Workflow step '%s' recovery copy failed: %s",
                        step.id,
                        _ce,
                    )
                    # Fall through to the failed branch below.
            info["status"] = "failed"
            info["failure_reason"] = "pass-but-no-output"
            logger.warning("Workflow step '%s' verdict=pass but %s missing — flipping to failed", step.id, output_file)
            return "failed"

        info["status"] = "complete"
        return "complete"

    if verdict in ("unknown", "error"):
        # Recovery couldn't produce a verdict (or wasn't attempted because
        # reflect already crashed). Last-resort: trust the filesystem. The
        # worker may have been cancelled, may have crashed post-output, or
        # may have hit an exception path that ate its reflect. If the
        # deliverable is on disk we'd rather unblock downstream than fail
        # a workflow that actually shipped its work.
        from sessions.manager import get_manager as _mgr

        _worker_obj = _mgr().get(worker_id)
        _term = _worker_obj.termination_reason if _worker_obj else None
        output_file = step.output_file
        output_present = bool(output_file and (run_dir / output_file).exists())

        if _term == "cancelled":
            if output_present:
                info["status"] = "complete"
                info["reflect_verdict"] = "cancelled-but-complete"
                return "complete"
            info["status"] = "cancelled"
            info["failure_reason"] = "cancelled"
            return "failed"

        if output_present:
            info["status"] = "complete"
            # Preserve which path got us here so the manifest is auditable —
            # 'error' means reflect crashed; 'unknown' means it never ran.
            info["reflect_verdict"] = "error-but-complete" if verdict == "error" else "unknown-but-complete"
            logger.warning(
                "Workflow step '%s' worker %s verdict=%s but %s exists — "
                "treating as complete (downstream unblocked, quality not gated).",
                step.id,
                worker_id,
                verdict,
                output_file,
            )
            return "complete"

        info["status"] = "failed"
        info["failure_reason"] = "reflect-error-no-output" if verdict == "error" else "no-verdict-no-output"
        return "failed"

    if verdict == "retry":
        # File-evidence override: reflect's verdict is built from the agent's
        # final prose, but the agent often writes the deliverable BEFORE its
        # closing message. If the declared output_file exists with substantive
        # content, the work shipped — retrying would discard the file and
        # spawn a fresh worker that re-does ~minutes of work for no gain.
        # Real failure mode: workflow run 1ec11d2b, web-news worker
        # edf1b8a83c89 — wrote ai_tech_brief_web_news.json (6.8KB) at round 4,
        # then reflect read its "I'm still fetching..." prose and asked to
        # retry, costing ~3 minutes on a fresh worker.
        #
        # Budget gate: only skip the retry when the step has already exhausted its
        # retry budget. On the first attempt, let the retry loop fire so reflect's
        # quality feedback is actually acted on — the retry worker can read the
        # existing output file and improve or confirm it. Silently promoting to
        # "complete" on attempt 1 masked real quality regressions (e.g. a step that
        # only processed 2 of 5 required items passed as-is).
        output_file = step.output_file
        if output_file:
            output_path = run_dir / output_file
            if output_path.exists() and output_path.stat().st_size > 100:
                attempts_so_far = info.get("attempts", 1)
                max_retries_local = int(getattr(step, "max_retries", _DEFAULT_MAX_RETRIES) or _DEFAULT_MAX_RETRIES)
                if attempts_so_far > max_retries_local:
                    # Budget exhausted — file evidence is all we have; promote to complete.
                    logger.warning(
                        "Workflow step '%s' worker %s verdict=retry but %s exists "
                        "(%d bytes) and retry budget exhausted (%d/%d) — "
                        "overriding to complete on file evidence. Reflect: %s",
                        step.id,
                        worker_id,
                        output_file,
                        output_path.stat().st_size,
                        attempts_so_far,
                        max_retries_local,
                        (reflect.get("reasoning") or "")[:200],
                    )
                    info["status"] = "complete"
                    info["reflect_verdict"] = "retry-overridden-by-file-evidence"
                    info["reflect_verdict_original"] = "retry"
                    return "complete"
                else:
                    # Budget remains — let the retry loop act on reflect's feedback.
                    logger.info(
                        "Workflow step '%s' verdict=retry, %s exists (%d bytes) but "
                        "retry budget not exhausted (attempt %d/%d) — allowing retry.",
                        step.id,
                        output_file,
                        output_path.stat().st_size,
                        attempts_so_far,
                        max_retries_local,
                    )
                    # Fall through: info["status"] = "failed" below triggers retry loop.

    # verdict in {"retry" with no output, or any other unrecognised value} →
    # failed (caller's retry loop checks reflect_verdict == "retry" to decide
    # whether to respawn).
    info["status"] = "failed"
    info["failure_reason"] = verdict
    return "failed"


def run_workflow(
    name: str,
    inputs: str = "",
    _context: dict | None = None,
) -> str:
    """Execute a named workflow end-to-end.

    Spawns a worker per step in topological wave order (parallel within each
    wave). Dependency short-circuit: a step whose upstream failed/skipped is
    marked "skipped" and not spawned. Per-step retry: if the worker's reflect
    verdict is "retry", we call retry_worker up to step.max_retries (default 1)
    times. Verdict "escalate" halts the workflow. Output files are verified
    after verdict="pass" — a pass with no output is downgraded to failed.

    Emits workflow.* events via the global event bus for live UI updates.
    After completion, runs post-workflow reflect to generate skill proposals.
    """
    ctx = _context or {}

    from core.skills.registry import get_skill_registry
    from core.workflows.registry import get_workflow_registry

    wf_reg = get_workflow_registry()
    skill_reg = get_skill_registry()
    wf = wf_reg.get(name)
    if not wf:
        import difflib

        available = [w.name for w in wf_reg.all_workflows()]
        # Disambiguation 1: did the caller mean a skill with the same name?
        if skill_reg.exists(name):
            if skill_reg.is_disabled(name):
                return (
                    f"Error: '{name}' is a skill (not a workflow), and it is currently disabled. "
                    f"Enable it in Explorer > Skills before use. "
                    f"(Available workflows: {', '.join(available) or 'none'})"
                )
            return (
                f"Error: '{name}' is a SKILL, not a workflow. "
                f"Use load_skill(name='{name}') to activate it. "
                f"(Available workflows: {', '.join(available) or 'none'})"
            )
        # Disambiguation 2: typo on a workflow name?
        close_wf = difflib.get_close_matches(name, available, n=3, cutoff=0.6)
        if close_wf:
            return (
                f"Error: Workflow '{name}' not found. "
                f"Did you mean: {', '.join(close_wf)}? "
                f"(All workflows: {', '.join(available) or 'none'})"
            )
        # Disambiguation 3: typo on a skill name?
        # Suggest only enabled skills — pointing the agent at a disabled skill
        # would just trade one wrong-tool error for another.
        skill_names = [s.name for s in skill_reg.enabled_skills()]
        close_skill = difflib.get_close_matches(name, skill_names, n=2, cutoff=0.7)
        if close_skill:
            return (
                f"Error: Workflow '{name}' not found. "
                f"There is a similarly named skill: {', '.join(close_skill)}. "
                f"Use load_skill if you meant the skill. "
                f"(Available workflows: {', '.join(available) or 'none'})"
            )
        return (
            f"Error: Workflow '{name}' not found. "
            f"Available: {', '.join(available) or 'none'}. "
            f"Use discover_workflows() to search by capability."
        )

    missing = [s.skill for s in wf.steps if s.skill and not skill_reg.exists(s.skill)]
    if missing:
        return f"Error: Workflow references skills not in registry: {', '.join(missing)}"
    disabled = [s.skill for s in wf.steps if s.skill and skill_reg.is_disabled(s.skill)]
    if disabled:
        return (
            f"Error: Workflow references disabled skills: {', '.join(disabled)}. "
            "Enable them in Explorer > Skills before running this workflow."
        )

    run_id = secrets.token_hex(4)
    workspace_dir = Path(settings.workspace_dir)
    run_rel = f"workflows/{name}/{run_id}"
    run_dir = workspace_dir / "workflows" / name / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    from datetime import datetime, timezone

    manifest: dict = {
        "workflow": name,
        "run_id": run_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "inputs": inputs,
        "run_dir": str(run_dir),
        "steps": {
            s.id: {
                "id": s.id,
                "type": s.type,
                "skill": s.skill,
                "output_file": s.output_file,
                "depends_on": s.depends_on,
                "worker_id": None,
                "status": "pending",
                "attempts": 0,
            }
            for s in wf.steps
        },
    }
    _write_manifest(run_dir, manifest)

    db.create_workflow_run(
        run_id=run_id,
        workflow_name=name,
        run_dir=run_rel,
        step_count=len(wf.steps),
    )

    try:
        waves = wf.topological_waves()
    except Exception as e:
        db.finish_workflow_run(run_id, "failed", 0, 0, 0)
        return f"Error: Could not compute workflow execution order: {e}"

    # Extend the orchestrator session's LLM budget proportionally to the
    # workflow's natural wall-clock shape. The base llm_session_timeout (1800s
    # by default) is a wall-clock guard meant for normal interactive turns;
    # for an orchestrator that delegates to N waves of workers, each of which
    # can independently consume up to a full session budget, that cap is
    # ~N× too small. Each worker has its own fresh budget; we just need the
    # orchestrator to outlive the wait + reconcile. +1 covers the orchestrator's
    # own reconciliation rounds and any post-workflow agent / reflect calls.
    # Hard-capped at 24h to prevent runaway from pathological workflows.
    orch_session_id = ctx.get("session_id", "")
    if orch_session_id:
        try:
            from core.llm.client import extend_session_budget as _extend

            base_timeout = float(settings.llm_session_timeout) if settings.llm_session_timeout > 0 else 0.0
            if base_timeout > 0:
                extension = min(
                    (len(waves) + 1) * base_timeout,
                    24 * 3600.0,
                )
                new_cap = _extend(orch_session_id, extension)
                logger.info(
                    "run_workflow %s/%s: extended session %s LLM budget to %.0fs "
                    "(%d waves × %.0fs base + reconciliation)",
                    name,
                    run_id,
                    orch_session_id[:12],
                    new_cap,
                    len(waves),
                    base_timeout,
                )
        except Exception as _ext_err:
            logger.warning(
                "run_workflow %s/%s: failed to extend session budget: %s",
                name,
                run_id,
                _ext_err,
            )

    # Protect the orchestrator from `reap_idle_sessions`'s 300s-stuck-PROCESSING
    # unstick. run_workflow blocks for the entire wave loop; without a
    # background ref, the reaper transitions PROCESSING→IDLE_READY mid-flight
    # and the agent task can no longer report results to the user, even
    # though it's actively waiting on workers. Released in the finally block
    # at the end of run_workflow so it's also released on exception paths.
    _orch_session_obj = None
    if orch_session_id:
        try:
            from sessions.manager import get_manager as _get_manager

            _orch_session_obj = _get_manager().get(orch_session_id)
            if _orch_session_obj is not None:
                _orch_session_obj.add_background_ref()
        except Exception as _ref_err:
            logger.warning(
                "run_workflow %s/%s: failed to take background ref on %s: %s",
                name,
                run_id,
                orch_session_id[:12],
                _ref_err,
            )
            _orch_session_obj = None

    _emit_workflow_event(
        {
            "type": "workflow.started",
            "workflow": name,
            "run_id": run_id,
            "run_dir": str(run_dir),
            "step_count": len(wf.steps),
            "wave_count": len(waves),
        },
        session=_orch_session_obj,
    )

    try:
        escalated = False
        escalation_step: str = ""
        # Tracks any unexpected exception during the wave loop so the finally
        # block can mark the run failed instead of leaving the DB row stuck at
        # status='running'. Previously, an exception thrown out of await_workers
        # (e.g. concurrent.futures.TimeoutError with empty str()) would propagate
        # through, return literal "Error: " to the agent, AND orphan the run row.
        fatal_exc: Exception | None = None

        for wave_idx, wave in enumerate(waves):
            try:
                _emit_workflow_event(
                    {
                        "type": "workflow.wave_started",
                        "workflow": name,
                        "run_id": run_id,
                        "wave_idx": wave_idx,
                        "wave_size": len(wave),
                        "step_ids": [s.id for s in wave],
                    },
                    session=_orch_session_obj,
                )

                # ── Phase 1: determine which steps are eligible (upstream complete) ──
                eligible: list = []
                for step in wave:
                    ready, reason = _upstream_ready(step, manifest, run_dir)
                    if not ready:
                        manifest["steps"][step.id]["status"] = "skipped"
                        manifest["steps"][step.id]["skipped_reason"] = reason
                        logger.info("Workflow '%s' step '%s' skipped: %s", name, step.id, reason)
                        _emit_workflow_event(
                            {
                                "type": "workflow.step_skipped",
                                "workflow": name,
                                "run_id": run_id,
                                "step_id": step.id,
                                "reason": reason,
                            },
                            session=_orch_session_obj,
                        )
                        continue
                    eligible.append(step)

                # ── Phase 2: spawn eligible steps ──
                wave_worker_ids: list[str] = []
                step_to_worker: dict[str, str] = {}
                for step in eligible:
                    worker_id = _spawn_step(
                        wf,
                        step,
                        manifest,
                        run_dir,
                        name,
                        run_id,
                        inputs,
                        ctx,
                    )
                    manifest["steps"][step.id]["attempts"] = 1
                    _write_manifest(run_dir, manifest)
                    if worker_id:
                        wave_worker_ids.append(worker_id)
                        step_to_worker[step.id] = worker_id
                        _emit_workflow_event(
                            {
                                "type": "workflow.step_started",
                                "workflow": name,
                                "run_id": run_id,
                                "step_id": step.id,
                                "worker_id": worker_id,
                            },
                            session=_orch_session_obj,
                        )

                # ── Phase 3: wait for the wave ──
                if wave_worker_ids:
                    logger.info("Workflow '%s' wave %d: waiting for %d workers", name, wave_idx, len(wave_worker_ids))
                    try:
                        # 300s stale threshold (vs the default 120s for interactive
                        # await_workers). Workflow workers naturally take longer:
                        # scout retry on a slow Ollama model alone can run 100-180s,
                        # and the worker hasn't bumped last_activity yet because
                        # scout doesn't transition state mid-flight. Run e8c94b86
                        # (2026-04-27) lost a transcribe wave because scout retry
                        # took 143s, exceeded the 120s default by 23s, the worker
                        # was marked stalled and finalized before its first round.
                        await_workers(
                            worker_ids=wave_worker_ids,
                            suspend=False,
                            stale_threshold=300,
                            _context=ctx,
                        )
                    except Exception as wait_err:
                        # await_workers should never propagate; if it does, log and
                        # press on so _finalize_step can still inspect each worker's
                        # current state from the DB.
                        logger.exception(
                            "Workflow '%s' wave %d await_workers raised %s — " "proceeding with current worker states.",
                            name,
                            wave_idx,
                            type(wait_err).__name__,
                        )

                # ── Phase 4: resolve verdicts, retry if asked, detect escalation ──
                for step in eligible:
                    worker_id = step_to_worker.get(step.id, "")
                    if not worker_id:
                        # Spawn failed; status already set to "spawn_failed"
                        _emit_workflow_event(
                            {
                                "type": "workflow.step_completed",
                                "workflow": name,
                                "run_id": run_id,
                                "step_id": step.id,
                                "status": "spawn_failed",
                            },
                            session=_orch_session_obj,
                        )
                        continue

                    outcome = _finalize_step(step, worker_id, manifest, run_dir, ctx)

                    # Retry loop: verdict=retry + attempts under budget → retry_worker
                    max_retries = int(getattr(step, "max_retries", _DEFAULT_MAX_RETRIES) or _DEFAULT_MAX_RETRIES)
                    while (
                        outcome == "failed"
                        and manifest["steps"][step.id].get("reflect_verdict") == "retry"
                        and manifest["steps"][step.id]["attempts"] <= max_retries
                    ):
                        reason = manifest["steps"][step.id].get("reflect_reasoning", "retry requested by reflect")
                        logger.info(
                            "Workflow '%s' step '%s' retry %d/%d: %s",
                            name,
                            step.id,
                            manifest["steps"][step.id]["attempts"],
                            max_retries,
                            reason[:120],
                        )
                        _emit_workflow_event(
                            {
                                "type": "workflow.step_retry",
                                "workflow": name,
                                "run_id": run_id,
                                "step_id": step.id,
                                "attempt": manifest["steps"][step.id]["attempts"] + 1,
                                "reason": reason[:200],
                            },
                            session=_orch_session_obj,
                        )
                        retry_result = retry_worker(
                            worker_id=worker_id,
                            reason=reason,
                            _context=ctx,
                        )
                        m = _WORKER_ID_RE.match(retry_result)
                        if not m:
                            manifest["steps"][step.id]["status"] = "failed"
                            manifest["steps"][step.id]["failure_reason"] = f"retry-spawn-failed: {retry_result[:200]}"
                            break
                        worker_id = m.group(1)
                        manifest["steps"][step.id]["worker_id"] = worker_id
                        manifest["steps"][step.id]["attempts"] += 1
                        _write_manifest(run_dir, manifest)
                        try:
                            # See wave-level await_workers above for stale_threshold rationale.
                            await_workers(
                                worker_ids=[worker_id],
                                suspend=False,
                                stale_threshold=300,
                                _context=ctx,
                            )
                        except Exception as retry_wait_err:
                            logger.exception(
                                "Workflow '%s' step '%s' retry await_workers raised %s",
                                name,
                                step.id,
                                type(retry_wait_err).__name__,
                            )
                        outcome = _finalize_step(step, worker_id, manifest, run_dir, ctx)

                    _emit_workflow_event(
                        {
                            "type": "workflow.step_completed",
                            "workflow": name,
                            "run_id": run_id,
                            "step_id": step.id,
                            "worker_id": worker_id,
                            "status": manifest["steps"][step.id]["status"],
                            "verdict": manifest["steps"][step.id].get("reflect_verdict"),
                            "attempts": manifest["steps"][step.id]["attempts"],
                        },
                        session=_orch_session_obj,
                    )

                    if outcome == "escalated" and not escalated:
                        escalated = True
                        escalation_step = step.id

                _write_manifest(run_dir, manifest)

                if escalated:
                    # Halt the workflow: mark all remaining (pending) steps as skipped.
                    logger.warning("Workflow '%s' halted — step '%s' escalated", name, escalation_step)
                    for s in wf.steps:
                        if manifest["steps"][s.id]["status"] == "pending":
                            manifest["steps"][s.id]["status"] = "skipped"
                            manifest["steps"][s.id][
                                "skipped_reason"
                            ] = f"halted after escalation of '{escalation_step}'"
                    _write_manifest(run_dir, manifest)
                    break
            except Exception as wave_exc:
                # Defensive: any unexpected per-wave exception is captured so the
                # function can still tally outcomes and call finish_workflow_run
                # below. Without this, an exception here would bubble up to
                # registry.execute_sync, return literal "Error: " to the agent (because
                # str(TimeoutError())==''), AND leave the workflow_runs row stuck at
                # status='running' forever (no orphan recovery existed).
                fatal_exc = wave_exc
                logger.exception(
                    "Workflow '%s' wave %d aborted by unexpected exception (%s) — " "finalizing run as failed.",
                    name,
                    wave_idx,
                    type(wave_exc).__name__,
                )
                # Mark any in-flight 'running' steps as failed so the manifest tells
                # the truth instead of pointing the user at orphaned scratch state.
                for sid_, info_ in manifest["steps"].items():
                    if info_.get("status") in ("pending", "running"):
                        info_["status"] = "failed"
                        info_["failure_reason"] = (f"workflow aborted: {type(wave_exc).__name__}: {wave_exc}")[:500]
                _write_manifest(run_dir, manifest)
                break

        # ── Tally outcomes and finalize ──
        counts = {"complete": 0, "failed": 0, "skipped": 0, "spawn_failed": 0, "escalated": 0}
        for s in wf.steps:
            status = manifest["steps"][s.id]["status"]
            counts[status] = counts.get(status, 0) + 1
        steps_passed = counts["complete"]
        steps_failed = counts["failed"] + counts["spawn_failed"] + counts["escalated"]

        if fatal_exc is not None:
            # Unexpected abort overrides whatever else happened — even partial
            # progress is unreliable when the driver fell over mid-flight.
            final_status = "failed"
        elif escalated:
            final_status = "escalated"
        elif steps_failed == 0 and counts["skipped"] == 0:
            final_status = "complete"
        elif steps_passed > 0:
            final_status = "partial"
        else:
            final_status = "failed"
        db.finish_workflow_run(run_id, final_status, steps_passed, steps_failed, 0)

        # Aggregate quality warnings: steps where reflect flagged a problem even
        # if they ultimately completed (via retry or file-evidence override).
        quality_warnings = []
        for sid, sinfo in manifest["steps"].items():
            if sinfo.get("reflect_verdict_original"):
                quality_warnings.append(
                    {
                        "step_id": sid,
                        "reflect_verdict_original": sinfo["reflect_verdict_original"],
                        "reflect_reasoning": (sinfo.get("reflect_reasoning") or "")[:300],
                    }
                )
            elif sinfo.get("attempts", 1) > 1:
                quality_warnings.append(
                    {
                        "step_id": sid,
                        "reflect_verdict_original": "retry",
                        "reflect_reasoning": (sinfo.get("reflect_reasoning") or "")[:300],
                    }
                )
        manifest["quality_warnings"] = quality_warnings
        _write_manifest(run_dir, manifest)

        # Post-workflow reflect — generate skill improvement proposals
        proposal_count = 0
        try:
            from core.workflows.reflect import workflow_reflect

            proposal_count = workflow_reflect(run_dir / "manifest.json", wf, ctx)
            if proposal_count > 0:
                db.update_workflow_run_proposals(run_id, proposal_count)
        except Exception as e:
            logger.warning("Post-workflow reflect failed for '%s/%s': %s", name, run_id, e)

        _emit_workflow_event(
            {
                "type": "workflow.completed",
                "workflow": name,
                "run_id": run_id,
                "status": final_status,
                "steps_passed": steps_passed,
                "steps_failed": steps_failed,
                "steps_skipped": counts["skipped"],
                "proposal_count": proposal_count,
            },
            session=_orch_session_obj,
        )

        # Build summary
        status_glyphs = {
            "complete": "✓",
            "failed": "✗",
            "escalated": "⚠",
            "skipped": "⊘",
            "spawn_failed": "✗",
            "running": "⟳",
            "pending": "○",
        }
        step_lines = []
        for s in wf.steps:
            info = manifest["steps"][s.id]
            glyph = status_glyphs.get(info["status"], "?")
            verdict = info.get("reflect_verdict", info.get("skipped_reason", "-"))
            wid = info.get("worker_id") or ""
            attempts = info.get("attempts", 0)
            attempts_str = f" x{attempts}" if attempts > 1 else ""
            step_lines.append(
                f"  {glyph} {s.id} [{info['status']}{attempts_str}] "
                f"({verdict}) — worker {wid[:8] if wid else 'N/A'}"
            )

        summary = (
            f"Workflow '{name}' run {run_id}: {final_status}. "
            f"{steps_passed}/{len(wf.steps)} complete"
            + (f", {counts['skipped']} skipped" if counts["skipped"] else "")
            + (
                f", {counts['failed'] + counts['spawn_failed']} failed"
                if (counts["failed"] + counts["spawn_failed"])
                else ""
            )
            + (f", {counts['escalated']} escalated" if counts["escalated"] else "")
            + ".\n"
            + "\n".join(step_lines)
            + f"\nOutputs in: {run_dir}"
        )
        if proposal_count > 0:
            summary += f"\n{proposal_count} skill improvement proposal(s) generated — review in the Workflows tab."
        if fatal_exc is not None:
            # Surface the abort reason on the agent-visible result so the caller
            # doesn't see a confusing empty "Error:" — without this prefix, the
            # tool dispatcher would still wrap a propagating exception's empty
            # str() and confuse the LLM.
            summary = (
                f"Aborted: {type(fatal_exc).__name__}: {fatal_exc} "
                f"(driver-side failure — workflow run finalized as failed).\n" + summary
            )
        return summary
    finally:
        # Always release the orchestrator's background ref so the reaper can
        # collect it normally after the workflow finishes (or aborts).
        if _orch_session_obj is not None:
            try:
                _orch_session_obj.remove_background_ref()
            except Exception as _rel_err:
                logger.warning(
                    "run_workflow %s/%s: failed to release background ref: %s",
                    name,
                    run_id,
                    _rel_err,
                )


def cancel_workflow(run_id: str, _context: dict | None = None) -> str:
    """Cancel a running workflow: cancel all running workers, mark the run cancelled.

    Reads the manifest, calls cancel_worker on every step whose status is
    "running", and marks the workflow_runs row as cancelled. Does not touch
    steps that have already completed.
    """
    # Find the run's manifest by scanning the workspace. The run_id alone
    # isn't enough — we need the workflow name — so do a narrow scan.
    workspace_dir = Path(settings.workspace_dir)
    wf_runs = workspace_dir / "workflows"
    if not wf_runs.exists():
        return f"Error: No workflow runs directory at {wf_runs}"

    manifest_path: Path | None = None
    for wf_dir in wf_runs.iterdir():
        if not wf_dir.is_dir():
            continue
        candidate = wf_dir / run_id / "manifest.json"
        if candidate.exists():
            manifest_path = candidate
            break
    if manifest_path is None:
        return f"Error: Workflow run '{run_id}' not found in any workflow directory."

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as e:
        return f"Error: Could not read manifest for run '{run_id}': {e}"

    cancelled_workers: list[str] = []
    for step_id, info in manifest.get("steps", {}).items():
        if info.get("status") == "running" and info.get("worker_id"):
            wid = info["worker_id"]
            try:
                cancel_worker(wid, _context=_context)
                cancelled_workers.append(wid[:8])
                info["status"] = "cancelled"
            except Exception as e:
                logger.warning("cancel_workflow: failed to cancel worker %s: %s", wid, e)

    manifest["cancelled_at"] = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
    try:
        manifest_path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    except Exception as e:
        logger.warning("cancel_workflow: failed to persist manifest: %s", e)

    try:
        db.finish_workflow_run(
            run_id,
            "cancelled",
            sum(1 for i in manifest["steps"].values() if i.get("status") == "complete"),
            sum(1 for i in manifest["steps"].values() if i.get("status") in ("failed", "spawn_failed", "escalated")),
            0,
        )
    except Exception as e:
        logger.warning("cancel_workflow: DB update failed: %s", e)

    _emit_workflow_event(
        {
            "type": "workflow.cancelled",
            "workflow": manifest.get("workflow"),
            "run_id": run_id,
            "cancelled_workers": cancelled_workers,
        }
    )

    return (
        f"Workflow run {run_id} cancelled. "
        f"Cancelled {len(cancelled_workers)} running worker(s): "
        f"{', '.join(cancelled_workers) if cancelled_workers else 'none'}."
    )


def _write_manifest(run_dir: Path, manifest: dict) -> None:
    """Write manifest.json to the run directory."""
    try:
        (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    except Exception as e:
        logger.warning("Failed to write workflow manifest: %s", e)


def register(reg) -> None:
    """Register orchestration extension tools."""
    common = {"category": "orchestration", "source": "extension", "denied_session_types": {"worker"}}
    orch_tags = ["parallel", "worker", "orchestrate", "delegate", "concurrent", "spawn", "multi"]

    reg.register(
        name="spawn_worker",
        func=spawn_worker,
        description=(
            "Spawn a worker agent for a subtask. Worker runs in fresh context with its own scout. "
            "Optionally runs on a specific model (e.g. vision model). Returns worker ID. "
            "Set auto_resume_parent=True to add this worker to the parent watch-set so the parent "
            "auto-resumes when all watched workers finish (use with await_workers suspend=True). "
            "Concurrency cap: configurable via max_concurrent_workers (default 5). When the cap is "
            "hit, spawn returns an error immediately — there is no queue. For embarrassingly-parallel "
            "work like N chunks: spawn up to the cap, call await_workers (suspend=True for cron/long "
            "tasks), then spawn the next batch."
        ),
        parameters={
            "type": "object",
            "properties": {
                "task_description": {"type": "string", "description": "Detailed task for the worker"},
                "title": {"type": "string", "description": "Short title for the worker"},
                "model": {
                    "type": "string",
                    "description": "Optional: specific model ID for the worker (e.g. a vision model). Leave empty to use default.",
                },
                "auto_resume_parent": {
                    "type": "boolean",
                    "description": "Add worker to parent watch-set for auto-resume (default false)",
                },
                "spec": {
                    "type": "string",
                    "description": (
                        "Optional worker_spec id from the [WORKER SPECS] catalog. Supplies role "
                        "instructions, a model, and verification gates; explicit model/title override it."
                    ),
                },
            },
            "required": ["task_description"],
        },
        tags=orch_tags + ["spawn", "create", "start"],
        timeout=60,
        parallel_safe=False,
        safety_level="safe",
        **common,
    )
    reg.register(
        name="check_workers",
        func=check_workers,
        description="Check status of all workers (running/done/stalled).",
        parameters={"type": "object", "properties": {}},
        tags=orch_tags + ["status", "check", "monitor"],
        timeout=15,
        parallel_safe=True,
        **common,
    )
    reg.register(
        name="get_worker_result",
        func=get_worker_result,
        description=(
            "Get the final summary from a completed worker. If the worker's "
            "reflect verdict was not 'pass' the result is prefixed with an "
            "UNVERIFIED/ESCALATED header — call get_worker_transcript to "
            "inspect the full work stream before trusting the output."
        ),
        parameters={
            "type": "object",
            "properties": {"worker_id": {"type": "string", "description": "Worker session ID"}},
            "required": ["worker_id"],
        },
        tags=orch_tags + ["result", "output", "summary"],
        timeout=30,
        parallel_safe=True,
        **common,
    )
    reg.register(
        name="get_worker_transcript",
        func=get_worker_transcript,
        description=(
            "Read a worker's full message stream (user, scout, assistant texts, "
            "tool calls, tool results, reflect). Use when get_worker_result "
            "returns an UNVERIFIED/ESCALATED summary and you need to see what "
            "the worker actually did."
        ),
        parameters={
            "type": "object",
            "properties": {
                "worker_id": {"type": "string", "description": "Worker session ID"},
                "include_tool_results": {
                    "type": "boolean",
                    "description": "Include tool result messages (default true)",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Max total chars to return (default 30000)",
                },
            },
            "required": ["worker_id"],
        },
        tags=orch_tags + ["transcript", "history", "stream", "debug"],
        timeout=30,
        parallel_safe=True,
        **common,
    )
    reg.register(
        name="await_workers",
        func=await_workers,
        description=(
            "Wait for workers to complete. Three modes: "
            "(1) Default: blocks via 3s polling up to 30 minutes. "
            "(2) worker_ids + min_done: unblock as soon as N specific workers finish. "
            "(3) suspend=True: exit agent loop immediately; parent auto-resumes once all "
            "watched workers complete (requires spawn_worker auto_resume_parent=True)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "stale_threshold": {"type": "integer", "description": "Seconds of inactivity = stalled (default 120)"},
                "worker_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Specific worker IDs to wait for (default: all)",
                },
                "min_done": {
                    "type": "integer",
                    "description": "Return as soon as this many workers complete (0 = wait for all)",
                },
                "suspend": {
                    "type": "boolean",
                    "description": "Suspend parent session until workers complete instead of blocking (default false)",
                },
            },
        },
        tags=orch_tags + ["wait", "block", "sync", "suspend"],
        timeout=1800,
        parallel_safe=False,
        long_poll=True,
        **common,
    )
    reg.register(
        name="notify_parent",
        func=notify_parent,
        description=(
            "Push a message or findings summary from this worker up to the parent session. "
            "Use when you discover something the parent needs immediately, before all workers finish. "
            "Parent idle → starts new turn. Parent busy → injects into its context."
        ),
        parameters={
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Message to send to the parent"},
                "findings_summary": {"type": "string", "description": "Optional structured findings summary"},
            },
            "required": ["message"],
        },
        tags=orch_tags + ["notify", "message", "push", "communicate"],
        timeout=15,
        parallel_safe=False,
        safety_level="safe",
        category="orchestration",
        source="extension",
    )
    reg.register(
        name="message_worker",
        func=message_worker,
        description="Send a fire-and-forget message to a running worker.",
        parameters={
            "type": "object",
            "properties": {
                "worker_id": {"type": "string"},
                "message": {"type": "string"},
            },
            "required": ["worker_id", "message"],
        },
        tags=orch_tags + ["message", "send", "communicate"],
        timeout=15,
        parallel_safe=False,
        safety_level="safe",
        **common,
    )
    reg.register(
        name="cancel_worker",
        func=cancel_worker,
        description="Cancel a running worker.",
        parameters={"type": "object", "properties": {"worker_id": {"type": "string"}}, "required": ["worker_id"]},
        tags=orch_tags + ["cancel", "stop", "kill"],
        timeout=15,
        parallel_safe=False,
        safety_level="safe",
        **common,
    )
    reg.register(
        name="set_worker_state",
        func=set_worker_state,
        description="Pause or resume a worker. Pass paused=true to pause at next checkpoint, paused=false to resume.",
        parameters={
            "type": "object",
            "properties": {
                "worker_id": {"type": "string"},
                "paused": {"type": "boolean", "description": "true to pause, false to resume"},
            },
            "required": ["worker_id", "paused"],
        },
        tags=orch_tags + ["pause", "resume", "suspend", "continue", "unpause"],
        timeout=15,
        parallel_safe=False,
        **common,
    )
    reg.register(
        name="retry_worker",
        func=retry_worker,
        description="Retry a failed worker with fresh context. Spawns replacement with previous output as context.",
        parameters={
            "type": "object",
            "properties": {
                "worker_id": {"type": "string"},
                "new_instructions": {"type": "string", "description": "Updated instructions"},
                "reason": {"type": "string", "description": "Why retrying"},
            },
            "required": ["worker_id"],
        },
        tags=orch_tags + ["retry", "redo", "restart"],
        timeout=60,
        parallel_safe=False,
        safety_level="safe",
        **common,
    )
    reg.register(
        name="run_workflow",
        func=run_workflow,
        description=(
            "Execute a named workflow end-to-end. Spawns a worker per step in "
            "topological wave order (parallel within each wave). Retries steps "
            "whose reflect verdict is 'retry' (bounded); halts on 'escalate'; "
            "short-circuits downstream steps when an upstream dependency fails. "
            "Inputs are injected into every step's task. After completion, "
            "generates skill-improvement proposals from failed steps. "
            "Emits workflow.* SSE events for live progress."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Workflow name (must exist in data/workflows/)"},
                "inputs": {"type": "string", "description": "Free-form inputs available to every step"},
            },
            "required": ["name"],
        },
        tags=orch_tags + ["workflow", "pipeline", "chain", "automate"],
        timeout=3600,
        parallel_safe=False,
        safety_level="safe",
        long_poll=True,
        **common,
    )
    reg.register(
        name="cancel_workflow",
        func=cancel_workflow,
        description=(
            "Cancel a running workflow by run_id: cancels every step's running worker "
            "and marks the run cancelled. Completed steps are left alone. Use when a "
            "workflow is going off the rails and you want to stop it without manually "
            "cancelling each worker."
        ),
        parameters={
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "The 8-char run_id returned by run_workflow."},
            },
            "required": ["run_id"],
        },
        tags=orch_tags + ["workflow", "cancel", "stop"],
        timeout=30,
        parallel_safe=False,
        safety_level="safe",
        **common,
    )
