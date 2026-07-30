"""RLM (Recursive Language Models) extension for Pernix.

Inference-time scaffold for processing inputs far beyond the model's context
window (paper: arXiv 2512.24601): the input lives as a `context` variable in a
sandboxed child REPL; the root model iteratively writes ```repl``` code to
slice/analyze it and delegates chunk work to sub-LLM calls brokered by the
parent. Core adapted from https://github.com/alexzhang13/rlm (MIT License,
Copyright (c) 2025 Alex Zhang) — extracted and rewritten for Pernix, not a
dependency. See docs/internals/rlm.md.

Gating follows the Candor pattern: register() is a hard off-switch at startup
(restart to add/remove the tool); the tool function re-checks rlm_enabled at
call time so a hot toggle-off degrades to a clear error, never a run.
"""

import asyncio
import logging
import time
from datetime import datetime
from pathlib import Path

from config import settings
from core.extensions.rlm import runs
from core.extensions.rlm.child_env import MAX_SOURCE_BYTES, SOURCE_WARN_BYTES, stage_context
from core.extensions.rlm.engine import RLMEngine
from core.extensions.rlm.types import RLMBudgetExhausted, RLMCaps, RLMRunError, RLMRunResult
from core.tools.registry import ToolRegistry
from db import models as db

logger = logging.getLogger(__name__)

# Extra dispatch headroom so the engine's own deadline always fires first
# (BASH_MAX_TIMEOUT/dispatch-grace precedent).
_DISPATCH_GRACE = 60

# Upstream rlm_query semantics: the prompt (often a chunk assembled in the
# parent's REPL) IS the child's context; the task is generic.
_NESTED_TASK = "Answer the query contained in the context."


def _resolve_root_model() -> str:
    return settings.rlm_root_model or settings.llm_model


def _resolve_sub_model() -> str:
    return settings.rlm_sub_model or settings.background_model or settings.llm_model


def _session_identity(context: dict | None):
    """(session_obj, session_id, created_at_ts, priority) — agent.py's scheduler
    identity convention, so RLM traffic queues fairly as this session's work."""
    from core.llm.semaphore import PRIORITY_ORCHESTRATOR, PRIORITY_WORKER
    from sessions.manager import get_manager

    sid = (context or {}).get("session_id", "")
    session = get_manager().get(sid) if sid else None
    priority = PRIORITY_WORKER if session and session.session_type == "worker" else PRIORITY_ORCHESTRATOR
    created_at = float("inf")
    try:
        row = db.get_session(sid) if sid else None
        created_at = datetime.fromisoformat((row or {}).get("created_at", "").replace("Z", "+00:00")).timestamp()
    except Exception:
        pass
    return session, sid, created_at, priority


def _resolve_sources(source) -> tuple[str | None, list[Path], str]:
    """Map the tool's `source` arg to (inline_text, files, description).

    Every string is first tried as a workspace-relative path; a single string
    that resolves to no existing file is treated as inline text. In a list,
    every entry must be an existing file (a typo'd path in a list is an error,
    not silently-analyzed literal text).
    """
    from core.tools.paths import safe_read_path

    entries = source if isinstance(source, list) else [source]
    entries = [str(e) for e in entries if str(e).strip()]
    if not entries:
        raise ValueError("source is empty — pass workspace file path(s) or inline text")

    files: list[Path] = []
    for entry in entries:
        try:
            p = safe_read_path(entry)
        except ValueError:
            p = None
        if p is not None and p.is_file():
            files.append(p)
        elif len(entries) == 1:
            return entry, [], f"inline text ({len(entry)} chars)"
        else:
            raise ValueError(f"source file not found in workspace: {entry}")
    return None, files, ", ".join(f.name for f in files)


def _make_rlm_fn(*, parent_engine, parent_run_id, depth, chat, sub_model, allowed, caps, cancel_check, session_id):
    """Build the broker's rlm_query callback: run a nested engine (own child
    process, nested run dir, shared ledger, remaining deadline) at depth+1.

    Runs on a broker handler thread, which already holds one concurrency slot —
    so nested fan-out stays bounded by the parent's semaphore. An engine at
    depth d gets a callback only when d+1 < rlm_max_depth; past that, the
    broker's built-in fallback degrades rlm_query to a plain llm_query.
    """
    child_depth = depth + 1

    def rlm_fn(prompt: str, model: str | None) -> str:
        nested_root = model or sub_model  # broker validated any override against the allowlist
        sub_id, sub_dir, sub_rel = runs.mint_run_dir(parent_run_dir=parent_engine.run_dir)
        staged = stage_context(sub_dir, text=prompt)
        nested = RLMEngine(
            run_dir=sub_dir,
            task=_NESTED_TASK,
            staged=staged,
            root_chat=lambda msgs, t: chat(msgs, nested_root, t),
            sub_chat=lambda p, m, t: chat([{"role": "user", "content": p}], m or sub_model, t),
            caps=caps,
            address_space_limit=settings.shell_address_space_limit_bytes,
            allowed_models=allowed,
            cancel_check=cancel_check,
            depth=child_depth,
            ledger=parent_engine.ledger,
            deadline=parent_engine.deadline,
        )
        if child_depth + 1 < settings.rlm_max_depth:
            nested.rlm_fn = _make_rlm_fn(
                parent_engine=nested,
                parent_run_id=sub_id,
                depth=child_depth,
                chat=chat,
                sub_model=sub_model,
                allowed=allowed,
                caps=caps,
                cancel_check=cancel_check,
                session_id=session_id,
            )
        runs.record_start(
            sub_id,
            sub_dir,
            sub_rel,
            session_id=session_id,
            task=prompt[:500],
            source_desc=f"rlm_query from run {parent_run_id}",
            root_model=nested_root,
            sub_model=sub_model,
            input_chars=staged.total_chars,
            parent_run_id=parent_run_id,
            depth=child_depth,
        )
        try:
            result = nested.run()
        except RLMRunError as e:
            runs.record_finish(sub_id, sub_dir, RLMRunResult(answer="", status="failed", partial=True, error=str(e)))
            return f"Error: nested RLM run {sub_id} failed - {e}"
        runs.record_finish(sub_id, sub_dir, result)
        if result.answer and result.answer.strip():
            return result.answer
        return f"Error: nested RLM run {sub_id} ended with status={result.status} and no answer"

    return rlm_fn


def rlm_process(task: str, source, model: str = "", _context: dict | None = None) -> str:
    """Run one RLM pass over a large input. Blocking; runs on a long-poll tool thread."""
    if not settings.rlm_enabled:
        return "Error: RLM is disabled (settings.rlm_enabled)."
    if not task or not task.strip():
        return "Error: task is required — what question should the RLM answer about the source?"

    from core.llm.client import extend_session_budget, get_llm_client
    from core.llm.semaphore import LLMSessionTimeoutError
    from core.tools.truncation import truncate_output

    try:
        inline_text, files, source_desc = _resolve_sources(source)
    except ValueError as e:
        return f"Error: {e}"

    total_bytes = len(inline_text.encode("utf-8", "ignore")) if inline_text else sum(f.stat().st_size for f in files)
    if total_bytes > MAX_SOURCE_BYTES:
        return f"Error: source is {total_bytes} bytes; the RLM cap is {MAX_SOURCE_BYTES}. Split the input."
    size_warning = (
        f"\n(note: large source, {total_bytes // (1024 * 1024)} MB — expect a slow run)"
        if total_bytes > SOURCE_WARN_BYTES
        else ""
    )

    ctx = dict(_context or {})
    loop = ctx.get("_loop")
    if loop is None:
        return "Error: no event loop in tool context — rlm_process must run via the tool executor."

    client = get_llm_client()
    session, sid, created_at, priority = _session_identity(ctx)
    root_model = _resolve_root_model()
    sub_model = model or _resolve_sub_model()
    allowed = {m for m in (root_model, sub_model, settings.background_model, settings.llm_model) if m}

    def _chat(messages: list[dict], use_model: str, timeout: float) -> str:
        future = asyncio.run_coroutine_threadsafe(
            client.chat(
                messages,
                model=use_model,
                max_tokens=settings.max_tokens,
                session_id=sid,
                session_created_at=created_at,
                session_priority=priority,
            ),
            loop,
        )
        try:
            return future.result(timeout=timeout).content
        except TimeoutError:
            future.cancel()  # release the coroutine (and its scheduler slot) on the loop
            raise
        except LLMSessionTimeoutError as e:
            raise RLMBudgetExhausted(str(e)) from e

    def root_chat(messages, timeout):
        return _chat(messages, root_model, timeout)

    def sub_chat(prompt, sub_model_override, timeout):
        return _chat([{"role": "user", "content": prompt}], sub_model_override or sub_model, timeout)

    def cancel_check() -> bool:
        return bool(session and getattr(session, "cancel_requested", False))

    # The run's wall clock is dominated by child/sub-call time, but every
    # sub-call bills to this session — extend upfront (spawn_worker precedent).
    if sid:
        try:
            extend_session_budget(sid, float(settings.rlm_timeout_seconds) + 120.0)
        except Exception as e:
            logger.debug("rlm_process: failed to extend session budget: %s", e)

    def on_child_spawn(popen):
        if session is not None:
            session._active_process = popen

    try:
        run_id, run_dir, run_rel = runs.mint_run_dir()
    except OSError as e:
        return f"Error: could not create RLM run dir: {e}"

    caps = RLMCaps(
        max_iterations=settings.rlm_max_iterations,
        max_subcalls=settings.rlm_max_subcalls,
        max_concurrent_subcalls=settings.rlm_max_concurrent_subcalls,
        timeout_seconds=float(settings.rlm_timeout_seconds),
        max_depth=settings.rlm_max_depth,
    )
    try:
        staged = stage_context(run_dir, text=inline_text, files=files or None)
    except OSError as e:
        return f"Error: failed to stage source into the run dir: {e}"

    runs.record_start(
        run_id,
        run_dir,
        run_rel,
        session_id=sid,
        task=task,
        source_desc=source_desc,
        root_model=root_model,
        sub_model=sub_model,
        input_chars=staged.total_chars,
    )
    engine = RLMEngine(
        run_dir=run_dir,
        task=task,
        staged=staged,
        root_chat=root_chat,
        sub_chat=sub_chat,
        caps=caps,
        address_space_limit=settings.shell_address_space_limit_bytes,
        allowed_models=allowed,
        cancel_check=cancel_check,
        on_child_spawn=on_child_spawn,
    )
    if settings.rlm_max_depth > 1:
        engine.rlm_fn = _make_rlm_fn(
            parent_engine=engine,
            parent_run_id=run_id,
            depth=0,
            chat=_chat,
            sub_model=sub_model,
            allowed=allowed,
            caps=caps,
            cancel_check=cancel_check,
            session_id=sid,
        )

    started = time.monotonic()
    try:
        result = engine.run()
    except RLMRunError as e:
        result = RLMRunResult(
            answer="",
            status="failed",
            duration=time.monotonic() - started,
            partial=True,
            error=str(e),
        )
        runs.record_finish(run_id, run_dir, result)
        return f"Error: RLM run {run_id} failed to start — {e}"
    finally:
        if session is not None:
            session._active_process = None

    runs.record_finish(run_id, run_dir, result)

    header = (
        f"[RLM run {run_id}: {result.status}, {result.iterations} iterations, "
        f"{result.subcalls} sub-calls, {result.duration:.0f}s]"
    )
    if result.partial:
        header += " (best-effort answer — the run did not submit a final answer before ending)"
    footer = f"\n\n(full trace: rlm/{run_id}/trace.jsonl in the workspace)"
    if result.status != "completed" and not (result.answer and result.answer.strip()):
        return f"Error: RLM run {run_id} ended with status={result.status} and no answer. {result.error}{footer}"
    truncated, meta = truncate_output(f"{header}{size_warning}\n{result.answer}{footer}", "rlm_process")
    return truncated, meta


def register(reg: ToolRegistry) -> None:
    """Register the agent-facing tool. Hard off-switch: nothing when disabled."""
    if not settings.rlm_enabled:
        logger.debug("RLM extension inactive (rlm_enabled=false)")
        return

    reg.register(
        name="rlm_process",
        description=(
            "Recursively process an input FAR TOO LARGE to read inline (beyond the context window, or too "
            "dense to skim with file_read pagination). A root model holds the input as a variable in a "
            "sandboxed Python REPL, slices it programmatically, and delegates chunks to sub-LLM calls — use "
            "it to fully analyze/summarize/search huge files, multi-document corpora, transcripts, logs, "
            "session-history dumps, or codebase concatenations, not just web content. "
            "WHEN: input over ~100K chars that needs dense whole-input understanding (every truncated "
            "file_read pagination loop is a signal to use this instead). NOT for: inputs that fit in "
            "context, or simple keyword lookups (use grep/file_read). "
            "HOW: stage big content as workspace file(s) first (file_write / fetched pages / transcripts), "
            "then call with source=path or source=[path, ...]; a single non-path string is treated as "
            "inline text. `task` is the question to answer about the source. Runs take minutes (caps: "
            f"{settings.rlm_max_iterations} turns, {settings.rlm_max_subcalls} sub-calls, "
            f"{settings.rlm_timeout_seconds}s) and return one answer plus a trace in workspace rlm/<run_id>/. "
            "The optional `model` overrides the sub-call model for this run."
        ),
        parameters={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The question/instruction the RLM should answer about the source",
                },
                "source": {
                    "description": "Workspace-relative file path, list of paths, or inline text",
                    "anyOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                },
                "model": {
                    "type": "string",
                    "description": "Optional sub-call model override (defaults to the RLM Sub-call role)",
                },
            },
            "required": ["task", "source"],
        },
        func=rlm_process,
        category="analysis",
        tags=["rlm", "long-context", "recursive", "summarize", "corpus", "transcript"],
        timeout=settings.rlm_timeout_seconds + _DISPATCH_GRACE,
        long_poll=True,
        parallel_safe=False,
        worker_allowed=True,
        safety_level="caution",
        source="extension",
    )
