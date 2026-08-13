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
        # parent's own LLM work. Without this, orchestrators built from
        # spawn_worker + await_workers (cron jobs, agent-driven fan-out)
        # hit LLMSessionTimeoutError mid-flight and the
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


def get_worker_result(worker_id: str, _context: dict | None = None) -> str:
    """Get the final output from a completed worker.

    Quality gate: if the worker's latest reflect verdict is not 'pass' (or no
    reflect ran), the returned content is wrapped in an UNVERIFIED/ESCALATED
    header with the reflect reasoning, so the parent knows the work needs
    another look or a transcript scan.
    """
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
    # transitions it to IDLE_READY mid-run. (A caller holding an explicit
    # background ref is the primary defense; this is the secondary one, and
    # it keeps diagnostic queries that read idle_seconds honest.)
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
        # last_activity_time. A 120s stale threshold there caused the caller
        # to abandon a worker mid-reflect and read verdict='unknown', marking
        # it failed even though reflect would land 'pass' moments later.
        # (Repro: run 024c370f, 2026-04-26 — crawl-subs, under the workflow
        # engine that has since been removed; the hazard is the threshold,
        # not the engine, so it applies to any await_workers caller.)
        # AWAITING_USER is also gated: in an orchestrated / cron context there
        # is no human who can answer ask_user, so a worker that hits this
        # state would otherwise stall the entire wave for max_wait (30 min)
        # waiting for an answer that never comes. A task can tell a worker
        # not to ask, but workers don't always honor it. Treating
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
            # finalized 2s after spawn, escalated, halted the whole run.)
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
            # orphaned the run). 30s is a generous cap that still falls
            # well inside the outer max_wait loop. If even 30s is exceeded the
            # loop is genuinely wedged; fall back to a thread-side sleep so the
            # caller doesn't die — the next iteration will re-check workers.
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
        # check stays current. (Backup to any background_ref the caller
        # holds — covers callers that hold none.)
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
        without queuing a new turn. This is the supervisor-injection
        pattern; previously it was always queued.
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
