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
from dataclasses import dataclass, field
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


def _count_active_workers(manager, parent, *, ignore: str = "") -> int:
    """Workers of `parent` still occupying a slot.

    Single definition shared by both spawn gates below. They used to carry
    separate inline tuples that disagreed about "unknown", so the capacity
    warning and the max_concurrent_workers limit counted different things.

    `ignore` excludes one worker that the caller has just cancelled but that
    has not finished unwinding: retry_worker cancels the old worker and
    immediately spawns its replacement, and at the cap the still-CANCELLING
    original made the retry fail with "Max active workers reached" instead
    of retrying.
    """
    return sum(
        1
        for wid in list(parent.worker_ids)
        if wid != ignore and manager.get_status(wid).get("status") not in _WORKER_INACTIVE_STATUSES
    )


def spawn_worker(
    task_description: str,
    title: str = "",
    model: str = "",
    kind: str = "",
    auto_resume_parent: bool = False,
    _context: dict | None = None,
    _replacing: str = "",
) -> str:
    """Spawn a worker session for a subtask. Returns worker session ID.

    If model is specified, the worker runs on that model instead of the default.
    Useful for delegating to specialized models (e.g. vision, code).

    kind selects a typed worker bundle (role instructions, exclusive tool
    allowlist, default model, verification criteria) — see kinds.py. An
    explicit `model` argument overrides the kind's default model.

    auto_resume_parent: if True, this worker is added to the parent's watch-set
    so the parent auto-resumes when all watched workers complete. Use together
    with await_workers(suspend=True) to suspend the parent until results arrive.
    """
    ctx = _context or {}
    parent_id = ctx.get("session_id", "")
    if not parent_id:
        return "Error: No parent session context"

    # Resolve the kind FIRST — an unknown kind must fail before any session
    # exists, with the valid names in the error so the model can self-correct
    # in the same round.
    from core.extensions.orchestration import kinds as _kinds

    worker_kind = None
    if kind:
        worker_kind = _kinds.resolve_kind(kind)
        if worker_kind is None:
            return f"Error: Unknown worker kind '{kind}'. Valid kinds: {', '.join(_kinds.list_kind_names())}."
        if not model:
            model = _kinds.resolve_kind_model(worker_kind)

    from sessions.manager import get_manager

    manager = get_manager()

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
            active_workers = _count_active_workers(manager, parent, ignore=_replacing)
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
            active_count = _count_active_workers(manager, parent, ignore=_replacing)
            if active_count >= settings.max_concurrent_workers:
                return f"Error: Max active workers ({settings.max_concurrent_workers}) reached. Wait for running workers to complete."

        # Create session inside the lock to atomically reserve the slot.
        # Workers inherit the parent's space (v33): space_id is persisted so
        # rehydration keeps membership, and create_session derives the
        # workspace_home — the worker writes in the space folder, compiles
        # space directives, routes memory to the space, shares its kernel.
        worker_id = manager.create_session(
            title=worker_title,
            system_prompt="",
            session_type="worker",
            parent_session_id=parent_id,
            space_id=getattr(parent, "space_id", None) if parent else None,
        )

    # Open this worker's first run BEFORE the charter is written: the charter
    # has to name the exact path the record will later be read from, or the
    # worker writes its report where nobody looks (H08). workspace_home is the
    # space folder its own relative writes resolve into.
    from core.extensions.orchestration import report as _report

    _worker_home = getattr(manager.get(worker_id), "workspace_home", None)
    _run = _report.begin_run(worker_id, workspace_home=_worker_home, reason="spawn")
    summary_file = _run["report_name"]
    system_prompt = (
        f"You are a focused worker agent. Your task:\n{task_description}\n\n"
        "Complete the task using tools as needed.\n"
        f"When done, write your report to {_run['report_path']} — the bare name "
        f"{summary_file} resolves there from your own file tools. That file is "
        "what your parent reads back; nothing else you write is collected.\n"
    )
    if worker_kind is not None:
        system_prompt += _kinds.kind_charter_block(worker_kind)
    if model:
        system_prompt += f"\nYou are running on model: {model}\n"

    # ARC-3 sweep finding #1: across nine solver workers, repl use was ZERO —
    # parents' "run script X" handoffs read as bash work, and every worker
    # paid the cold-start tax the kernel exists to remove. The steering the
    # main session gets from scout now rides the worker charter too.
    if settings.session_kernel_enabled:
        system_prompt += (
            "\nIf your task drives a live or stateful environment, or builds "
            "up state across steps (game engines, simulations, incremental "
            "computation), hold the live objects in your persistent repl "
            "kernel — variables survive across rounds and turns — and use "
            "bash only for one-shot commands and heavy subprocess compute.\n"
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
    # Persist kind + model on the row (migration v31) so a rehydrated worker
    # (server restart, idle reap, resume_worker) keeps its identity — the
    # in-memory fields below die with the process.
    db.update_session(
        worker_id,
        system_prompt=system_prompt,
        worker_kind=(worker_kind.name if worker_kind is not None else None),
        model_override=(model or None),
    )

    # Set model override on worker session if specified. The "not yet done"
    # gate is now implicit in v2 — a freshly-created worker is in IDLE_READY
    # but has never run; check_workers/await_workers use the existence of
    # state_log rows (or its absence + last_activity_time) to distinguish
    # "never ran" from "ran and settled."
    worker_session = manager.get(worker_id)
    if worker_session and model:
        worker_session.model_override = model
    # Kind allowlist: EXCLUSIVE, enforced by the schema builder and the
    # executor — the same two-point enforcement as scheduled-job charters.
    if worker_session is not None and worker_kind is not None and worker_kind.tool_allowlist:
        worker_session.tool_allowlist = frozenset(worker_kind.tool_allowlist)
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

    # Emit event to parent — include effective model + kind so the UI can display them
    _effective_model = model or settings.llm_model
    manager.emit(
        parent_id,
        {
            "type": "worker.started",
            "worker_id": worker_id,
            "title": worker_title,
            "model": _effective_model,
            "kind": worker_kind.name if worker_kind is not None else "",
        },
    )

    _kind_note = f" [{worker_kind.name}]" if worker_kind is not None else ""
    return f'Worker spawned: {worker_id} — "{worker_title}"{_kind_note}'


def _worker_has_output(wid: str) -> bool:
    """Check if a worker produced assistant messages."""
    messages = db.get_messages(wid)
    return any(m["role"] == "assistant" and m.get("content") for m in messages)


def _worker_has_live_process(w) -> bool:
    """True when the worker still has a subprocess of its own running."""
    if w is None:
        return False
    try:
        return any(proc is not None and proc.poll() is None for proc in w.all_processes())
    except Exception:
        return False


def _worker_idle_seconds(w) -> int:
    """Seconds since the worker last showed activity — 0 while one of its own
    subprocesses is still running.

    last_activity_time only moves on harness events (tool call start/finish,
    stream chunks), so a worker that handed a 20-minute build or solve to bash
    looked idle for the entire time it was working: check_workers reported
    "idle 900s" and await_workers' stall test abandoned the wave (field case,
    session 3dc5a307d751). A live child process is activity.
    """
    if w is None:
        return 0
    if _worker_has_live_process(w):
        return 0
    try:
        return int(w.idle_seconds)
    except Exception:
        return 0


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

    # The durable inventory, not the memory-only list: after a restart the
    # in-memory list is empty and this answered "No workers spawned." to a
    # parent with live children in the database (H09).
    inventory = manager.worker_inventory(parent)
    if not inventory:
        return "No workers spawned."

    from sessions import state_v2 as sv2

    lines = []
    done = 0
    failed = 0
    empty = 0
    filter_set = set(_filter_ids) if _filter_ids is not None else None
    wid_list = [w for w in inventory if filter_set is None or w in filter_set]
    for wid in wid_list:
        worker_obj = manager.get(wid)
        _row = db.get_session(wid)
        title = _row.get("title", "?") if _row else "?"
        _kind = (_row or {}).get("worker_kind") or ""
        if _kind:
            title = f"{title} [{_kind}]"

        # v2 state is authoritative. IDLE_READY means "no active turn."
        # But a just-created worker is also IDLE_READY before its first
        # transition fires — distinguish via task (set by manager.prompt).
        # Status payload doesn't carry v2 state directly, so read in-memory.
        if worker_obj is None:
            # Not resident: reaped, or the server restarted. The row knows what
            # memory does not — reading defaults here reported finished workers
            # as "queued (not yet started)" for the rest of the parent's life.
            try:
                v2 = sv2.SessionStateV2((_row or {}).get("state_v2") or "idle_ready")
            except ValueError:
                v2 = sv2.SessionStateV2.IDLE_READY
            idle = 0
            try:
                has_started = db.latest_turn_id(wid) > 0 or bool(db.recent_termination_reasons(wid, 1))
            except Exception:
                has_started = False
        else:
            v2 = sv2._current_state(worker_obj)
            idle = _worker_idle_seconds(worker_obj)
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
            # A live subprocess is work in progress, not silence.
            parts.append("running subprocess" if _worker_has_live_process(worker_obj) else f"idle {idle}s")

        lines.append(f"- {wid[:8]} \"{title}\": {' | '.join(parts)}")

    header = f"Workers: {done}/{len(wid_list)} done"
    if failed:
        header += f", {failed} FAILED"
    if empty:
        header += f", {empty} empty (no output)"

    result_text = header + "\n" + "\n".join(lines)

    # Cross-pollinate completed worker findings to running siblings
    if done > 0 and done < len(inventory):
        try:
            xp = cross_pollinate(_context=_context)
            if "Cross-pollinated" in xp:
                result_text += f"\n{xp}"
        except Exception as e:
            logger.debug("Cross-pollination skipped: %s", e)

    return result_text


def _worker_is_mid_turn(worker_obj) -> bool:
    """True while the worker's turn is still running. A running turn has no
    ending yet, so the durable log's answer belongs to a previous run."""
    if worker_obj is None:
        return False
    try:
        from sessions import state_v2 as sv2

        return sv2._current_state(worker_obj) is not sv2.SessionStateV2.IDLE_READY
    except Exception:
        return False


@dataclass
class TrustState:
    """What is actually known about a worker's current output.

    Assembled from records only — the run record, the state log, the grade
    rows and the artifact's digest at grading time. Never from the artifact's
    own first line: that is a line the worker can type, and a worker-authored
    `# AUTO-STAMPED (reflect=pass...)` used to suppress the real verdict.

    One builder serves get_worker_result, _finalize_worker's stamp and the
    parent's resume manifest, because three readers of one record that
    disagree is exactly the failure this replaces.
    """

    worker_id: str
    term_reason: str | None = None
    error: str = ""
    verdict: str | None = None
    reasoning: str = ""
    verification: str = ""
    verification_reason: str = ""
    missing: str = ""
    stale_verdict: str | None = None
    stale_seq: int = 0
    modified: bool = False
    retired: list = field(default_factory=list)

    _CAPS = ("round_ceiling", "stuck_loop", "budget_exhausted")

    def interruption_note(self) -> str:
        """How the turn ended, when it did not end by finishing. Emitted
        regardless of verdict: reflect grading partial output as fine does not
        un-truncate it, and `verdict == "pass"` used to return before this
        check ever ran."""
        r = self.term_reason
        if r == "cancelled":
            return "# CANCELLED (worker stopped before it finished)\n"
        if r in self._CAPS:
            return (
                f"# INCOMPLETE (worker terminated: {r} — a hard cap, not completion)\n"
                f"# Its final message should state what is unfinished; treat missing "
                f"deliverables as NOT DONE, and use get_worker_transcript({self.worker_id[:12]!r}) "
                f"or retry_worker to close the gap.\n"
            )
        if r == "error" or (r is None and self.error):
            if self.error:
                return f"# ERROR (worker exited with error: {self.error})\n"
            return "# INCOMPLETE (worker terminated: error)\n"
        if r in ("compaction_failed", "interrupted"):
            return f"# INCOMPLETE (worker terminated: {r})\n"
        return ""

    def _missing_note(self) -> str:
        """Evidence the grade itself said it never saw. Kept whatever the
        verdict is — it is the one line that names what to go and check."""
        return f"# Missing evidence: {self.missing[:300]}\n" if self.missing else ""

    def verification_note(self) -> str:
        """What the grader said about THIS run's output. Empty only for a
        clean, current, verified pass."""
        if self.verdict == "escalate":
            return (
                f"# ESCALATED (worker reflect: verdict=escalate)\n"
                f"# Reason: {self.reasoning or '(no reasoning provided)'}\n"
                + self._missing_note()
                + f"# Consider get_worker_transcript({self.worker_id[:12]!r}) to inspect "
                f"the full work stream before trusting this output.\n"
            )
        if self.verdict == "retry":
            return (
                f"# UNVERIFIED (worker reflect: verdict=retry, retries exhausted)\n"
                f"# Reason: {self.reasoning or '(no reasoning provided)'}\n" + self._missing_note()
            )
        if self.verdict == "pass":
            # A pass is a retry disposition, not a certificate. When the grade
            # says so — a confidence downgrade, a self-contradiction, a check
            # that could not run — the parent hears it here instead of finding
            # a marker clipped out of the end of `reasoning` (H11).
            if self.verification and self.verification != "verified":
                return (
                    f"# PASS BUT UNVERIFIED (reflect verdict=pass, verification={self.verification})\n"
                    f"# Why: {self.verification_reason or '(no reason recorded)'}\n"
                    + self._missing_note()
                    + f"# The work was not judged worth retrying; that is not the same as checked. "
                    f"Use get_worker_transcript({self.worker_id[:12]!r}) if the claim matters.\n"
                )
            return self._missing_note()
        if self.stale_verdict:
            return (
                f"# UNVERIFIED (this run has no grade of its own)\n"
                f"# The last recorded verdict is {self.stale_verdict!r}, and it graded run "
                f"{self.stale_seq} — different output. It does not carry.\n"
            )
        return "# UNVERIFIED (no reflect verdict recorded — quality not gated)\n"

    def mutation_note(self) -> str:
        """A verdict is a statement about bytes. If the bytes moved, it did not
        move with them, and the parent is entitled to know before it acts."""
        if not self.modified:
            return ""
        return (
            "# MODIFIED SINCE VERIFICATION (the report changed after it was graded — "
            "the verdict above describes the earlier bytes)\n"
        )

    def footer(self) -> str:
        """Superseded artifacts, named by run. They are kept rather than
        deleted, and labelled rather than served: an earlier run's report is
        that run's output, not this one's."""
        parts = [
            f"\n[run {entry.get('seq')}'s report is retained at {entry.get('path')} — "
            f"an earlier run's output, not this one's]"
            for entry in self.retired
        ]
        return "".join(parts)

    def header(self) -> str:
        head = self.interruption_note() + self.verification_note() + self.mutation_note()
        return head + "\n" if head else ""


def worker_trust(
    worker_id: str,
    *,
    term_reason: str | None = None,
    error: str = "",
    ref=None,
    include_history: bool = True,
) -> TrustState:
    """Build a worker's trust state from the durable record."""
    from core.extensions.orchestration import report as _report

    current, stale, stale_seq = _report.run_scoped_reflect(worker_id)
    grade = current or {}
    return TrustState(
        worker_id=worker_id,
        term_reason=term_reason,
        error=error or "",
        verdict=grade.get("verdict"),
        reasoning=grade.get("reasoning", "") or "",
        verification=grade.get("verification", "") or "",
        verification_reason=grade.get("verification_reason", "") or "",
        missing=grade.get("missing", "") or "",
        stale_verdict=(stale or {}).get("verdict"),
        stale_seq=stale_seq,
        modified=include_history and _report.graded_artifact_changed(worker_id, ref),
        retired=_report.retired_reports(worker_id) if include_history else [],
    )


def worker_termination_reason(worker_id: str, worker_obj=None) -> tuple[str | None, str]:
    """(termination_reason, error) for a worker, memory first, log behind.

    The in-memory object is the fresh source while a session is resident; after
    a reap or a restart the durable state log answers. A live turn has no
    ending yet, so it is never given a previous run's.
    """
    if worker_obj is None:
        from sessions.manager import get_manager as _get_mgr

        worker_obj = _get_mgr().get(worker_id)
    reason = worker_obj.termination_reason if worker_obj else None
    error = (worker_obj.error if worker_obj else "") or ""
    if reason is None and not _worker_is_mid_turn(worker_obj):
        try:
            recent = db.recent_termination_reasons(worker_id, 1)
            reason = recent[0] if recent else None
        except Exception as e:
            logger.debug("durable termination lookup failed for %s: %s", worker_id, e)
    return reason, error


def get_worker_result(worker_id: str, _context: dict | None = None) -> str:
    """Get the final output from a completed worker.

    Quality gate: the header is built from records — how the turn ended, the
    grade recorded for THIS run, and whether the artifact still matches the
    bytes that grade read. A verdict from an earlier run is reported as
    history, never applied as certification.
    """
    from core.extensions.orchestration import report as _report

    # Typed-kind deterministic gate (Feature 4): a cheap, unambiguous check of
    # the kind's core contract (e.g. research output names zero sources).
    # Warning-only — reflect stays the real gate.
    _kind_name = None
    try:
        _kind_name = (db.get_session(worker_id) or {}).get("worker_kind")
    except Exception:
        pass

    def _kind_warn(body: str) -> str:
        from core.extensions.orchestration.kinds import kind_gate_warning

        return kind_gate_warning(_kind_name, body) or ""

    from sessions.manager import get_manager as _get_mgr

    _worker_obj = _get_mgr().get(worker_id)
    term_reason, _err = worker_termination_reason(worker_id, _worker_obj)

    def _cap(full_text: str, ref=None) -> str:
        """Truncate to 3000 chars WITH a visible marker — silently cutting a
        worker's report mid-sentence left the parent with no signal that
        content was lost or where to find the rest.

        A preview that clips an artifact hands back the artifact. Both routes
        out of the cap were unsignposted until now: an absolute path reads the
        same from any space, and the transcript can be asked for its END."""
        if len(full_text) <= 3000:
            return full_text
        out = full_text[:3000] + f"\n[truncated at 3000 of {len(full_text)} chars"
        if ref is not None:
            out += f" — the complete report is on disk: file_read({str(ref.path)!r})"
        out += f", or get_worker_transcript({worker_id[:12]!r}, select='tail') for the end of the stream]"
        return out

    def _served(body: str) -> str:
        """Mark the result consumed on the way out.

        This call IS the consumption event, and retention needs to know it
        happened: an uncollected report is unfinished business, a collected
        one is a transcript that has done its job. Recorded only on the paths
        that actually return a result — telling the parent "no output" is not
        a reason to start anyone's deletion clock.
        """
        try:
            db.mark_worker_result_consumed(worker_id)
        except Exception as e:
            logger.debug("Could not mark worker %s result consumed: %s", worker_id, e)
        return body

    # The run record names one report path; discovery by the worker id in the
    # filename covers workers that predate the record; the shared summary.md is
    # adopted only with provenance, and says so when it is (H08).
    ref = _report.resolve_report(worker_id)
    trust = worker_trust(worker_id, term_reason=term_reason, error=_err, ref=ref)
    if ref is not None:
        try:
            body = ref.read()
        except OSError as _read_err:
            logger.warning("get_worker_result: could not read %s: %s", ref.path, _read_err)
            body = ""
        if body:
            # A stamp THIS harness wrote for THIS run already encodes the state
            # — don't double up. Recognized by the recorded digest, not by the
            # file's first line: that line is one a worker can author, and a
            # worker-authored sentinel used to suppress a real escalate.
            if _report.is_our_stamp(worker_id, ref):
                return _served(ref.label + _cap(body, ref) + trust.footer())
            return _served(ref.label + trust.header() + _kind_warn(body) + _cap(body, ref) + trust.footer())

    # Fallback: last assistant message, always wrapped in a quality header.
    messages = db.get_messages(worker_id)
    for m in reversed(messages):
        if m["role"] == "assistant" and m.get("content"):
            return _served(trust.header() + _kind_warn(m["content"]) + _cap(m["content"]) + trust.footer())

    # Retention took the transcript, but not the work. Checked before the "no
    # output" line below, which is what the parent used to be told about a
    # worker that had finished and filed a report: "produced no output. It may
    # have failed silently or timed out. Consider retrying" — three claims,
    # all false, about work that was sitting in a manifest.
    archived = _archived_result(worker_id)
    if archived:
        return archived

    # No output at all
    if _worker_obj and _worker_obj.error:
        return f"Worker {worker_id[:8]} FAILED with error: {_worker_obj.error}. Consider retrying with retry_worker()."
    return f"Worker {worker_id[:8]} produced no output. It may have failed silently or timed out. Consider retrying with retry_worker()."


def _archived_result(worker_id: str) -> str:
    """A pruned worker's result manifest, rendered for the parent, or "".

    The transcript is gone and cannot be retried into existence, so the header
    says what happened and points at the durable record rather than inviting a
    retry that would redo work already done.
    """
    try:
        manifest = db.get_worker_manifest(worker_id)
    except Exception as e:
        logger.warning("Could not read worker manifest for %s: %s", worker_id, e)
        return ""
    if not manifest:
        return ""
    header = (
        f"# ARCHIVED (worker transcript removed by retention on "
        f"{str(manifest.get('archived_at') or '')[:10]}; this is its preserved result manifest)\n"
        f"# Worker: {manifest.get('title') or 'untitled'} — last active "
        f"{str(manifest.get('last_active_at') or '')[:10]}"
        + (f", ended by {manifest['termination_reason']}" if manifest.get("termination_reason") else "")
        + f"\n# Result source: {manifest.get('result_source') or 'unknown'}. The full transcript is not "
        f"recoverable; retry_worker would redo the work, not retrieve it.\n\n"
    )
    body = manifest.get("result") or ""
    if not body.strip():
        return header + "(the manifest records no result text for this worker)\n"
    return header + body


def get_worker_transcript(
    worker_id: str,
    include_tool_results: bool = True,
    max_chars: int = 30000,
    select: str = "head",
    after_id: int = 0,
    before_id: int = 0,
    message_id: int = 0,
    _context: dict | None = None,
) -> str:
    """Read a worker's message stream, from either end, one page at a time.

    Safety valve for when get_worker_result returns an UNVERIFIED/ESCALATED
    summary, or clips a long report: lets the parent scan what the worker
    actually did (assistant texts, tool calls and their arguments, tool
    results, grades) and read the real findings.

    Every line is addressed: `[#<message id> role] content`. Those ids drive
    the paging.

    select: "head" (default, oldest-first from the start) or "tail" (the END
        of the stream). A worker's deliverable is the LAST thing it says, and
        head-first paging under a char budget could never reach it — the
        recovery route get_worker_result recommends returned early exploration.
    after_id / before_id: only messages with id above / below these.
    message_id: return exactly that one message, in full and unclipped. This
        is what a `[clipped ...]` pointer tells the caller to call.
    """
    try:
        messages = db.get_messages(worker_id)
    except Exception as e:
        return f"Error reading worker {worker_id[:8]} messages: {e}"

    if not messages:
        return f"Worker {worker_id[:8]} has no messages."

    if message_id:
        row = next((m for m in messages if int(m.get("id") or 0) == int(message_id)), None)
        if row is None:
            return f"Worker {worker_id[:8]} has no message #{message_id}."
        return "\n".join(_transcript_lines([row], include_tool_results=True, full=True)) or (
            f"Message #{message_id} has no renderable content."
        )

    scoped = [
        m
        for m in messages
        if (not after_id or int(m.get("id") or 0) > after_id) and (not before_id or int(m.get("id") or 0) < before_id)
    ]
    if not scoped:
        return f"Worker {worker_id[:8]} has no messages in that id range."

    lines = _transcript_lines(scoped, include_tool_results=include_tool_results, full=False)
    if not lines:
        return f"Worker {worker_id[:8]} has no renderable messages in that range."

    from_tail = str(select or "head").lower() == "tail"
    kept: list[str] = []
    budget = max(0, max_chars)
    used = 0
    for line in reversed(lines) if from_tail else lines:
        if used + len(line) + 1 > budget and kept:
            break
        kept.append(line)
        used += len(line) + 1
    if from_tail:
        kept.reverse()

    dropped = len(lines) - len(kept)
    out = "\n".join(kept)
    if dropped and from_tail:
        edge = _line_msg_id(kept[0]) if kept else 0
        out = (
            f"[{dropped} earlier line(s) omitted — get_worker_transcript(before_id={edge}, select='tail') for the page before this]\n"
            + out
        )
    elif dropped:
        edge = _line_msg_id(kept[-1]) if kept else 0
        out += f"\n[truncated — {dropped} later line(s) omitted; get_worker_transcript(after_id={edge}) continues, or select='tail' for the end]"
    return out


# Per-tool-result budget in a paged transcript: one 200KB grep result must not
# eat the whole page. The clipped line points at the row that holds the rest.
_TOOL_LINE_CHARS = 800


def _line_msg_id(line: str) -> int:
    """The message id a rendered line is addressed by, 0 if unparseable."""
    try:
        return int(line.split("#", 1)[1].split(" ", 1)[0].rstrip("]:"))
    except (IndexError, ValueError):
        return 0


def _tool_call_summary(tc_raw) -> str:
    """`name({args})` per call. Names alone were not enough to reconstruct
    what a worker did: two file_read calls look identical without their paths."""
    try:
        tcs = json.loads(tc_raw) if isinstance(tc_raw, str) else tc_raw
    except (json.JSONDecodeError, TypeError):
        return ""
    parts: list[str] = []
    for tc in tcs if isinstance(tcs, list) else []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
        name = fn.get("name") or tc.get("name") or "?"
        args = fn.get("arguments") if fn else tc.get("arguments")
        if not isinstance(args, str):
            try:
                args = json.dumps(args or {})
            except (TypeError, ValueError):
                args = ""
        parts.append(f"{name}({args[:300]})" if args and args != "{}" else str(name))
    return ", ".join(parts)


def _transcript_lines(messages: list, *, include_tool_results: bool, full: bool) -> list[str]:
    """Render rows to id-addressed lines. `full` disables every per-role clip —
    that is the single-message read a truncation pointer sends you to."""
    lines: list[str] = []

    def clip(text: str, limit: int, mid: int, what: str) -> str:
        if full or len(text) <= limit:
            return text
        return (
            f"{text[:limit]}\n[{what} clipped at {limit} of {len(text)} chars — "
            f"get_worker_transcript(message_id={mid}) for the whole row]"
        )

    for m in messages:
        role = m.get("role", "?")
        mid = int(m.get("id") or 0)
        if role == "tool" and not include_tool_results:
            continue
        content = (m.get("content") or "").replace("\r", "")
        if role == "assistant":
            calls = _tool_call_summary(m.get("tool_calls"))
            if calls:
                lines.append(f"[#{mid} assistant:tool_calls] {calls}")
            if content:
                lines.append(f"[#{mid} assistant] {content}")
        elif role == "tool":
            lines.append(f"[#{mid} tool] {clip(content, _TOOL_LINE_CHARS, mid, 'result')}")
        elif role == "reflect":
            try:
                r = json.loads(content)
                # verification rides beside the verdict, not inside the
                # reasoning: the downgrade marker lives at the END of that
                # string and the clip below is exactly where it disappeared.
                _ver = r.get("verification")
                _ver_txt = f" verification={_ver}" if _ver else ""
                lines.append(
                    f"[#{mid} reflect] verdict={r.get('verdict')}{_ver_txt} "
                    f"reasoning={clip(r.get('reasoning', ''), 200, mid, 'reasoning')}"
                )
            except (json.JSONDecodeError, TypeError):
                lines.append(f"[#{mid} reflect] {clip(content, 200, mid, 'grade')}")
        elif role == "scout":
            try:
                r = json.loads(content)
                approach = r.get("approach") or r.get("approach_guidance") or ""
                lines.append(f"[#{mid} scout] approach={clip(approach, 300, mid, 'approach')}")
            except (json.JSONDecodeError, TypeError):
                lines.append(f"[#{mid} scout] {clip(content, 200, mid, 'report')}")
        elif role == "system":
            lines.append(f"[#{mid} system] {clip(content, 300, mid, 'system note')}")
        elif role == "user":
            lines.append(f"[#{mid} user] {clip(content, 1000, mid, 'message')}")
    return lines


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
            # A worker whose spawn FAILED never starts a turn, so has_started
            # stays False and it sat here as "pending" until the caller's
            # full max_wait (30 minutes by default) elapsed — even though
            # spawn-time cleanup had already stamped it errored. The reaper
            # and the suspend path both treat this as terminal; this was the
            # one place that did not.
            if v2 is sv2.SessionStateV2.IDLE_READY and (w.error or w.termination_reason):
                done_count += 1
                continue
            pending_count += 1
            if v2 in STALE_GATED_STATES:
                idle = _worker_idle_seconds(w)
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
        # An IDLE_READY re-prompt is a fresh RUN, exactly like resume_worker:
        # without a boundary the previous run's artifact stays in place,
        # _finalize_worker short-circuits on it, and run N's report comes back
        # as run N+1's result. AWAITING_USER is the opposite case — answering a
        # question continues the SAME turn, so it keeps the same run.
        if current is sv2.SessionStateV2.IDLE_READY:
            from core.extensions.orchestration import report as _report

            _report.begin_run(worker_id, workspace_home=worker.workspace_home, reason="message_worker")
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

    # Cancellation is the parent explicitly giving up on this task, which is
    # what retention needs in order to ever release a worker it is otherwise
    # protecting as unfinished business. Recorded before the cancel itself so
    # it holds even if the loop-affine part below finds nothing to stop.
    try:
        db.mark_worker_abandoned(worker_id)
    except Exception as e:
        logger.debug("Could not mark worker %s abandoned: %s", worker_id, e)

    manager = get_manager()
    session = manager.get(worker_id)
    if not session:
        return f"Worker {worker_id} not found in memory"

    # This tool runs on a worker thread; Task.cancel() and the process
    # sweep are loop-affine, so marshal like pause_worker does.
    def _cancel_on_loop() -> str:
        if manager.cancel_session(session):
            return f"Worker {worker_id[:8]} cancelled"
        return f"Worker {worker_id[:8]} is not running"

    from core.events import call_on_loop

    return call_on_loop(_cancel_on_loop, loop=(_context or {}).get("_loop"))


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


def _resume_paused(session, worker_id: str, _context: dict | None = None) -> str:
    """Release a paused (or pause-requested) session — the original resume.

    Sets the pause_event. If the worker has already reached PAUSED, it will
    transition back to PROCESSING at its own pace (via the agent loop's
    resume branch). If still in PAUSE_REQUESTED (pause never observed),
    transition back directly here.
    """
    from sessions import state_v2 as sv2

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


def resume_worker(
    worker_id: str,
    note: str = "",
    auto_resume_parent: bool = False,
    _context: dict | None = None,
) -> str:
    """Resume a worker wherever it stopped (spec Feature 5).

    Three cases, one mental model — "bring the worker back to life":
      * PAUSED / PAUSE_REQUESTED → release the pause (original behavior).
      * Mid-turn states → nothing to resume; reports the live state.
      * Terminal (cancelled / errored / round-capped / reaped from memory /
        lost to a server restart) → REVIVE: rehydrate the session from the DB
        (message history, kind allowlist and pinned model persist), validate
        the model still resolves, clear the stale summary stamp, re-attach to
        the parent, and start a continuation turn carrying `note`.

    Non-worker sessions only ever get the pause-release path — the generic
    /resume API endpoint routes through here, and reviving an idle NORMAL
    session would start an unrequested turn.
    """
    from sessions import state_v2 as sv2
    from sessions.manager import get_manager

    manager = get_manager()
    session = manager.get(worker_id)
    row = db.get_session(worker_id)

    if session is not None and session.session_type != "worker":
        return _resume_paused(session, worker_id, _context)

    if session is not None:
        current = sv2._current_state(session)
        if current in (sv2.SessionStateV2.PAUSED, sv2.SessionStateV2.PAUSE_REQUESTED):
            return _resume_paused(session, worker_id, _context)
        if current is sv2.SessionStateV2.AWAITING_USER:
            return (
                f"Worker {worker_id[:8]} is waiting on a question, not paused. "
                "Answer it with message_worker(worker_id, <answer>)."
            )
        if current is not sv2.SessionStateV2.IDLE_READY:
            return f"Worker {worker_id[:8]} is {current.value} — already running; nothing to resume."
        if getattr(session, "_turn_id", 0) == 0 and session.termination_reason is None:
            return f"Worker {worker_id[:8]} is queued and has not started yet; nothing to resume."

    if row is None:
        return f"Worker {worker_id} not found"
    if (row.get("session_type") or "") != "worker":
        return f"Error: {worker_id[:8]} is not a worker session; resume_worker only revives workers."

    # --- Revival path -----------------------------------------------------
    ctx = _context or {}
    loop = ctx.get("_loop")
    if not loop:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return "Error: Cannot resume worker — no event loop available."

    parent_id = row.get("parent_session_id") or ""
    parent = manager.get(parent_id) if parent_id else None

    # Respect the same concurrency cap as spawn — a revival occupies a slot.
    if parent is not None:
        active = _count_active_workers(manager, parent)
        if active >= settings.max_concurrent_workers:
            return (
                f"Error: Max active workers ({settings.max_concurrent_workers}) reached — "
                "await or cancel running workers before resuming this one."
            )

    from core.events import call_on_loop

    def _revive_on_loop() -> str:
        # Hydrate (or reuse) the in-memory session. get_or_create restores
        # state_v2, model_override and the kind allowlist from the row.
        w = manager.get_or_create(worker_id)
        w_state = sv2._current_state(w)
        # A crash can persist a mid-turn state; no task survives a restart,
        # so forcing home is safe — mirrors the boot reconcile sweeps.
        task_alive = w.task is not None and not w.task.done()
        if w_state is not sv2.SessionStateV2.IDLE_READY:
            if task_alive:
                return f"Worker {worker_id[:8]} is {w_state.value} with a live turn; nothing to resume."
            try:
                sv2.transition(w, sv2.SessionStateV2.IDLE_READY, "reaper-unstick")
            except Exception as _e:
                return f"Error: could not reset worker state ({w_state.value}): {_e}"
        w.cancel_requested = False
        w.pause_event.set()
        w.error = None

        # Staleness guard: the pinned model may have been removed since the
        # worker last ran. Fall back to the default rather than dying on the
        # first LLM call — and say so.
        model_note = ""
        if w.model_override:
            try:
                from core.llm.client import get_llm_client

                registry = get_llm_client().router.registry
                resolved = registry.resolve_model_id(w.model_override)
                if not registry.get_model_info(resolved):
                    model_note = (
                        f" NOTE: your previous model '{w.model_override}' is no longer "
                        "available; you are on the default model now."
                    )
                    w.model_override = None
                    db.update_session(worker_id, model_override=None)
            except Exception as _e:
                logger.debug("resume_worker model validation skipped: %s", _e)

        # Re-attach to the parent so check_workers/await_workers see it.
        if parent is not None and worker_id not in parent.worker_ids:
            parent.worker_ids.append(worker_id)
        # Watch-set parity with spawn_worker(auto_resume_parent=True): the
        # revived worker's completion wakes the parent via the normal
        # _on_watched_worker_done path.
        if parent is not None and auto_resume_parent:
            parent._watched_worker_ids.add(worker_id)
            manager._persist_watched(parent)

        # Open run N+1. The previous run's artifact is retired (versioned),
        # not deleted: it would otherwise shadow everything the resumed run
        # produces, and deleting a valid report to make room loses work the
        # parent may still want. The record also moves the transcript boundary,
        # which is what stops run N's verdict certifying run N+1's output.
        from core.extensions.orchestration import report as _report

        _new_run = _report.begin_run(worker_id, workspace_home=w.workspace_home, reason="resume_worker")

        prior = w.termination_reason or ("unknown — memory was reaped or the server restarted")
        w.termination_reason = None
        if parent_id:
            manager.emit(
                parent_id,
                {
                    "type": "worker.resumed",
                    "worker_id": worker_id,
                    "title": row.get("title") or "worker",
                    "kind": row.get("worker_kind") or "",
                    "prior_termination": prior,
                },
            )
        return prior + "|" + model_note + "|" + _new_run["report_path"]

    outcome = call_on_loop(_revive_on_loop, loop=loop)
    if outcome.startswith(("Error:", "Worker ")):
        return outcome
    prior, _sep, _rest = outcome.partition("|")
    model_note, _sep2, report_path = _rest.partition("|")

    # Budget parity with spawn_worker: a parent that revives a worker and then
    # awaits it needs its LLM wall-clock extended past the child's runtime,
    # or the synthesis turn dies on its first acquire.
    if parent is not None:
        try:
            from core.llm.client import extend_session_budget as _extend

            base = float(settings.llm_session_timeout) if settings.llm_session_timeout > 0 else 0.0
            if base > 0:
                _extend(parent_id, min(2 * base, 24 * 3600.0))
        except Exception as _ext_err:
            logger.debug("resume_worker: failed to extend parent budget: %s", _ext_err)

    resume_msg = (
        f"[resumed by resume_worker] Your previous run ended: {prior}.{model_note}\n"
        + (f"Operator note: {note}\n" if note else "")
        + "Your full prior transcript is above (compacted if long). Review what "
        "is already done, then CONTINUE the original task to completion — do not "
        f"start over. The previous report was retired; write this run's report to "
        f"{report_path} when done."
    )
    asyncio.run_coroutine_threadsafe(get_manager().prompt(worker_id, resume_msg), loop)
    return f"Worker {worker_id[:8]} revived (previous end: {prior}) — continuation turn started."


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
    old_kind = (old_session or {}).get("worker_kind") or ""

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

    # The replacement inherits the original's typed kind so its allowlist and
    # verification gate survive the retry.
    # _replacing: the worker we just cancelled is still unwinding, so at the
    # cap it would otherwise count against its own replacement and the retry
    # would fail with "Max active workers reached".
    return spawn_worker(task, title=f"Retry: {old_title}", kind=old_kind, _context=_context, _replacing=worker_id)


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
    from core.extensions.orchestration import report as _report

    for cwid in completed:
        # Run-scoped, like every other reader: a pass that graded an earlier
        # run of a resumed worker is not a licence to broadcast this run's
        # findings to its siblings.
        reflect, _stale, _seq = _report.run_scoped_reflect(cwid)
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

    from core.extensions.orchestration.kinds import builtin_kind_names

    reg.register(
        name="spawn_worker",
        func=spawn_worker,
        description=(
            "Spawn a worker agent for a subtask. Worker runs in fresh context with its own scout. "
            "Optionally runs on a specific model (e.g. vision model). Returns worker ID. "
            f"kind selects a typed worker bundle ({', '.join(builtin_kind_names())}) — a role "
            "preamble, a task-appropriate tool allowlist and a verification gate; prefer a kind "
            "over a free-form charter when one fits. "
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
                "kind": {
                    "type": "string",
                    "description": (
                        "Optional typed worker kind: "
                        + ", ".join(builtin_kind_names())
                        + " (or a custom kind from data/worker_kinds/). Empty = untyped."
                    ),
                },
                "auto_resume_parent": {
                    "type": "boolean",
                    "description": "Add worker to parent watch-set for auto-resume (default false)",
                },
            },
            "required": ["task_description"],
        },
        tags=orch_tags + ["spawn", "create", "start", "kind", "typed"],
        timeout=60,
        parallel_safe=False,
        safety_level="safe",
        **common,
    )
    reg.register(
        name="resume_worker",
        func=resume_worker,
        description=(
            "Resume a worker wherever it stopped. A paused worker is released; a "
            "cancelled, errored, round-capped, reaped or restart-lost worker is REVIVED: "
            "its persisted history, kind and model are rehydrated and a continuation "
            "turn starts from where it left off (optionally carrying your note). "
            "Cheaper than retry_worker when the prior partial work is worth keeping."
        ),
        parameters={
            "type": "object",
            "properties": {
                "worker_id": {"type": "string", "description": "Worker session ID"},
                "note": {
                    "type": "string",
                    "description": "Optional guidance injected into the continuation turn",
                },
                "auto_resume_parent": {
                    "type": "boolean",
                    "description": (
                        "Add the revived worker to the parent watch-set so the parent "
                        "auto-resumes when it finishes (default false; same contract as spawn_worker)"
                    ),
                },
            },
            "required": ["worker_id"],
        },
        tags=orch_tags + ["resume", "revive", "continue", "rehydrate", "restart"],
        timeout=30,
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
        idempotent=False,  # a poll's answer changes between rounds — never dedup-cache it
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
            "Read a worker's message stream (user, scout, assistant texts, tool "
            "calls with arguments, tool results, reflect), one id-addressed line "
            "per message. Use when get_worker_result returns an "
            "UNVERIFIED/ESCALATED summary or clips a long report. select='tail' "
            "reads the END of the stream — that is where a worker's deliverable "
            "is; after_id/before_id page through it; message_id reads one row whole."
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
                "select": {
                    "type": "string",
                    "enum": ["head", "tail"],
                    "description": (
                        "'head' (default) reads from the start; 'tail' reads the END — "
                        "use it to reach a long worker's final report."
                    ),
                },
                "after_id": {"type": "integer", "description": "Only messages with id above this"},
                "before_id": {"type": "integer", "description": "Only messages with id below this"},
                "message_id": {
                    "type": "integer",
                    "description": "Return exactly this message, unclipped (what a [clipped ...] pointer names)",
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
        idempotent=False,  # waiting again is a new wait, not a cached answer
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
        description=(
            "Pause or resume a worker. Pass paused=true to pause at next checkpoint, "
            "paused=false to resume (a terminated worker is revived — see resume_worker)."
        ),
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
