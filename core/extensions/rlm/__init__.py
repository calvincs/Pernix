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
import json
import logging
import re
import shutil
import threading
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

# Session-budget headroom on top of the run's wall clock: root/sub calls in
# flight at the deadline, salvage synthesis, and result bookkeeping.
_BUDGET_GRACE = 120.0

# continue_from targets root-level run dirs only (mint_run_dir: token_hex(4)).
_RUN_ID_RE = re.compile(r"^[0-9a-f]{8}$")

# Bounds on the `prior` payload loaded into the child for a continuation —
# the frame protocol caps at 64MB; stay far under it and under child RAM.
_PRIOR_MAX_SUBCALLS = 200
_PRIOR_CHAR_BUDGET = 6_000_000

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
            is_file = p is not None and p.is_file()
        except ValueError:
            p, is_file = None, False
        except OSError:
            # is_file() itself raises for un-stat-able "paths" — e.g. inline
            # text over the 255-byte filename limit. That's just inline text.
            p, is_file = None, False
        if is_file:
            files.append(p)
        elif len(entries) == 1:
            return entry, [], f"inline text ({len(entry)} chars)"
        else:
            raise ValueError(f"source file not found in workspace: {entry}")
    return None, files, ", ".join(f.name for f in files)


def _resolve_prior_run(continue_from: str) -> tuple[str | None, Path | None, dict | None]:
    """Validate a continue_from run id -> (error, prior_dir, manifest).

    Deliberately strict on the id format: the value lands in a filesystem
    path, so anything but a bare 8-hex run id is rejected outright."""
    rid = continue_from.strip().lower()
    if not _RUN_ID_RE.match(rid):
        return f"Error: continue_from must be an 8-hex-char RLM run id, got {continue_from!r}.", None, None
    prior_dir = (Path(settings.workspace_dir) / "rlm" / rid).resolve()
    try:
        manifest = json.loads((prior_dir / "manifest.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return (
            f"Error: no RLM run {rid} found in the workspace — its artifacts may have been "
            "purged by retention. Drop continue_from and start fresh.",
            None,
            None,
        )
    except (OSError, json.JSONDecodeError) as e:
        return f"Error: could not read RLM run {rid}'s manifest: {e}", None, None
    if manifest.get("status") == "running":
        return f"Error: RLM run {rid} is still running — wait for it to finish before continuing from it.", None, None
    return None, prior_dir, manifest


def _load_prior(prior_dir: Path, manifest: dict) -> tuple[dict, str]:
    """Build the `prior` REPL variable and the continuation note for the root.

    Reloads the prior run's durable outputs — draft, returned answer, and
    every completed sub-call prompt/response pair from payloads.jsonl. This
    is continuation-by-artifact, deliberately NOT state resurrection: REPL
    variables are cheap to recompute from context; sub-call output is the
    thing that cost LLM time. Runs predating payloads.jsonl degrade to
    draft/answer only."""

    def _read(name: str) -> str:
        try:
            return (prior_dir / name).read_text(encoding="utf-8")
        except OSError:
            return ""

    draft = _read("draft.txt")
    answer = _read("answer.txt")
    try:
        lines = (prior_dir / "payloads.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    subcalls: list[dict] = []
    total = 0
    for line in reversed(lines):  # newest first under the budget, then restore order
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("kind") != "subcall" or not entry.get("ok"):
            continue
        prompt, response = str(entry.get("prompt") or ""), str(entry.get("response") or "")
        if len(subcalls) >= _PRIOR_MAX_SUBCALLS or total + len(prompt) + len(response) > _PRIOR_CHAR_BUDGET:
            break
        subcalls.append({"prompt": prompt, "response": response, "model": entry.get("model")})
        total += len(prompt) + len(response)
    subcalls.reverse()

    run_id = manifest.get("run_id", prior_dir.name)
    meta = {
        "run_id": run_id,
        "status": manifest.get("status", "unknown"),
        "iterations": manifest.get("iterations", 0),
        "subcalls": manifest.get("subcalls", 0),
        "task": manifest.get("task", ""),
        # Relative to the NEW run dir (the child's cwd); both live under rlm/.
        "trace_path": f"../{prior_dir.name}/trace.jsonl",
    }
    prior_vars = {"draft": draft, "answer": answer, "subcalls": subcalls, "meta": meta}

    draft_line = (
        f"- prior['draft']: its last running answer draft ({len(draft)} chars)."
        if draft.strip()
        else "- prior['draft']: empty (it kept no draft)."
    )
    note = "\n".join(
        [
            f"CONTINUATION: this run continues RLM run {run_id} on the same source "
            f"(ended {meta['status']} after {meta['iterations']} iterations, {meta['subcalls']} sub-calls). "
            "Its REPL variables are gone, but its durable outputs are pre-loaded in the REPL variable "
            "`prior` (a dict):",
            f"- prior['subcalls']: {len(subcalls)} completed sub-LLM results as {{prompt, response, model}} — "
            "this work is already paid for; reuse it instead of re-issuing the same calls.",
            draft_line,
            f"- prior['answer']: the answer it returned ({len(answer)} chars).",
            "- prior['meta']: run metadata; prior['meta']['trace_path'] is its trace, readable via open().",
            "Recompute cheap derivations (indices, boundaries, counts) in one cell if needed, but do NOT "
            "redo analysis `prior` already contains — verify it briefly, fill the gaps, and build on it.",
        ]
    )
    return prior_vars, note


def _emit_session_event(sid: str, event: dict) -> None:
    """Best-effort SSE emit onto the parent session's stream. Safe from any
    thread — manager.emit → session.emit_event marshals delivery onto the
    event loop (the same path worker.started/done use). No-ops when the
    session is not resident (it always is mid-turn) or on any failure:
    observability never breaks a run."""
    if not sid:
        return
    try:
        from sessions.manager import get_manager

        get_manager().emit(sid, event)
    except Exception:
        logger.debug("rlm: failed to emit %s event", event.get("type"), exc_info=True)


def _first_line(s: str, limit: int = 140) -> str:
    stripped = (s or "").strip()
    return stripped.splitlines()[0][:limit] if stripped else ""


def _activity_detail(etype: str, event: dict) -> str | None:
    """One human-readable line per trace event for the chip/strip UI."""
    if etype == "root":
        return _first_line(event.get("response_preview", ""))
    if etype == "cell":
        tag = "final answer" if event.get("final") else f"{event.get('duration', 0)}s"
        return f"{_first_line(event.get('code', ''))} · {tag}"
    if etype == "subcall":
        model = event.get("model") or "sub-model"
        outcome = "ok" if event.get("ok") else f"error: {_first_line(event.get('error', ''), 60)}"
        return f"{model} · {outcome} · {event.get('duration', 0)}s"
    if etype == "notice":
        return str(event.get("notice", ""))
    if etype == "synthesis":
        return "synthesizing final answer"
    return None


def _make_progress_fn(run_id: str, ui_session_id: str | None, sid: str):
    """Fan trace events out to the parent session's SSE stream and keep the
    run row's counters live. Called from the engine thread (root/cell), broker
    handler threads (subcall), and the heartbeat thread — hence the lock.
    Nested engines get no progress_fn, so every event here is depth 0."""
    lock = threading.Lock()
    counters = {"iterations": 0, "subcalls": 0}

    def progress_fn(event: dict) -> None:
        etype = event.get("type", "")
        with lock:
            if etype == "root":
                counters["iterations"] = int(event.get("iteration", 0)) + 1
            elif etype == "subcall":
                counters["subcalls"] += 1
            iterations, subcalls = counters["iterations"], counters["subcalls"]
        if etype in ("root", "subcall"):
            try:
                db.update_rlm_run_progress(run_id, iterations, subcalls)
            except Exception:
                logger.debug("rlm: progress row update failed for %s", run_id, exc_info=True)
        if not sid:
            return
        if etype == "heartbeat":
            _emit_session_event(
                sid,
                {
                    "type": "rlm.heartbeat",
                    "run_id": run_id,
                    "ui_session_id": ui_session_id,
                    "iterations": event.get("iteration", iterations),
                    "subcalls": event.get("subcalls", subcalls),
                    "in_flight": event.get("in_flight", 0),
                    "quiet_seconds": event.get("quiet_seconds", 0),
                    "elapsed": event.get("elapsed", 0),
                },
            )
            return
        if etype == "end":
            return  # rlm.done (emitted by the tool with the full result) covers it
        detail = _activity_detail(etype, event)
        if detail is None:
            return
        _emit_session_event(
            sid,
            {
                "type": "rlm.activity",
                "run_id": run_id,
                "ui_session_id": ui_session_id,
                "kind": etype,
                "iteration": event.get("iteration"),
                "detail": detail,
                "iterations": iterations,
                "subcalls": subcalls,
            },
        )

    return progress_fn


def _finalize_run_ui(sid: str, ui_session_id: str | None, run_id: str, result: RLMRunResult) -> None:
    """Park the sidebar view session and tell the parent stream the run ended."""
    if ui_session_id:
        try:
            db.update_session(
                ui_session_id,
                state="idle",
                subtitle=(
                    f"{result.status} · {result.iterations} it · " f"{result.subcalls} calls · {result.duration:.0f}s"
                ),
            )
        except Exception:
            logger.debug("rlm: could not finalize view session %s", ui_session_id, exc_info=True)
    _emit_session_event(
        sid,
        {
            "type": "rlm.done",
            "run_id": run_id,
            "ui_session_id": ui_session_id,
            "status": result.status,
            "iterations": result.iterations,
            "subcalls": result.subcalls,
            "duration": round(result.duration, 1),
            "partial": result.partial,
            "error": (result.error or "")[:300],
        },
    )


def _make_rlm_fn(*, parent_engine, parent_run_id, depth, chat, sub_model, allowed, caps, cancel_check, session_id):
    """Build the broker's rlm_query callback: run a nested engine (own child
    process, nested run dir, shared ledger, shared concurrency limiter,
    remaining deadline) at depth+1.

    Runs on a broker handler thread that deliberately holds NO concurrency
    slot; total in-flight sub-calls stay bounded because every depth's broker
    shares the ROOT engine's SubcallLimiter. An engine at depth d gets a
    callback only when d+1 < caps.max_depth; past that, the broker's built-in
    fallback degrades rlm_query to a plain llm_query.
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
            limiter=parent_engine.limiter,
            deadline=parent_engine.deadline,
        )
        if child_depth + 1 < caps.max_depth:
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
            caps=caps,
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


def rlm_process(task: str, source, model: str = "", continue_from: str = "", _context: dict | None = None) -> str:
    """Run one RLM pass over a large input. Blocking; runs on a long-poll tool thread."""
    if not settings.rlm_enabled:
        return "Error: RLM is disabled (settings.rlm_enabled)."
    if not task or not task.strip():
        return "Error: task is required — what question should the RLM answer about the source?"

    from core.llm.client import ensure_session_budget, get_llm_client, session_seconds_remaining
    from core.llm.semaphore import LLMSessionTimeoutError
    from core.tools.truncation import truncate_output

    try:
        inline_text, files, source_desc = _resolve_sources(source)
    except ValueError as e:
        return f"Error: {e}"

    total_bytes = len(inline_text.encode("utf-8", "ignore")) if inline_text else sum(f.stat().st_size for f in files)
    if total_bytes > MAX_SOURCE_BYTES:
        return f"Error: source is {total_bytes} bytes; the RLM cap is {MAX_SOURCE_BYTES}. Split the input."

    prior_dir = prior_manifest = None
    if continue_from:
        err, prior_dir, prior_manifest = _resolve_prior_run(continue_from)
        if err:
            return err
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
    # root and sub call bills to this session's LLM budget. Top the budget up
    # so the run's FULL window is still on the clock however much of the turn
    # is already spent — extend_session_budget grants headroom relative to the
    # base timeout only, so for back-to-back runs in one turn the re-grant was
    # a silent no-op and later runs died budget_exhausted mid-flight or at
    # iteration 0 (session a45fa830cef9). If the top-up didn't take, refuse
    # here — before staging context, spawning a child, or minting run rows —
    # with an answer the agent can act on instead of retrying.
    if sid:
        needed = float(settings.rlm_timeout_seconds) + _BUDGET_GRACE
        try:
            ensure_session_budget(sid, needed)
            remaining = session_seconds_remaining(sid)
        except Exception as e:
            # Fail open: the budget guard is an availability protection, and a
            # genuinely exhausted budget still errors on the first LLM call.
            logger.debug("rlm_process: session budget check failed open: %s", e)
            remaining = float("inf")
        if remaining < needed:
            return (
                f"Error: this session has ~{remaining:.0f}s of LLM time budget left, but an RLM run "
                f"needs up to {needed:.0f}s. The run was refused before staging anything. Do not call "
                "rlm_process again this turn — it will fail the same way. Report the results you "
                "already have (including partial answers from earlier runs) instead."
            )

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

    continuation_note = None
    if prior_manifest is not None:
        prior_hash = prior_manifest.get("source_sha256")
        if prior_hash and staged.sha256 and prior_hash != staged.sha256:
            # A continuation over different material would present the prior
            # run's findings as if they described THIS source. Refuse, and
            # leave nothing behind (no DB row or UI session exists yet).
            shutil.rmtree(run_dir, ignore_errors=True)
            return (
                f"Error: continue_from={prior_manifest.get('run_id', continue_from)} refused — the staged "
                "source differs from that run's source (content hash mismatch). Pass the exact same source, "
                "or drop continue_from to start fresh."
            )
        if not prior_hash:
            logger.debug("rlm_process: prior run %s has no source hash; continuing unverified", continue_from)
        prior_vars, continuation_note = _load_prior(prior_dir, prior_manifest)
        staged.extra_vars["prior"] = prior_vars

    # Sidebar anchor: a message-less session_type='rlm' child of the calling
    # session, so the run nests under its parent like a worker. Pure navigation
    # chrome — the run's content stays on disk; the viewer reads it via
    # /api/rlm/runs/*. DB-only on purpose: a resident AgentSession could never
    # run a turn and would only clutter the manager/reaper.
    ui_session_id = None
    if sid:
        try:
            ui_session_id = db.create_session(
                title=f"RLM: {' '.join(task.split())[:60]}",
                session_type="rlm",
                parent_session_id=sid,
            )
            db.update_session(ui_session_id, state="processing", subtitle=source_desc[:200])
        except Exception:
            logger.exception("rlm_process: could not create RLM view session")
            ui_session_id = None

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
        ui_session_id=ui_session_id,
        caps=caps,
        source_sha256=staged.sha256 or None,
        continued_from=continue_from or None,
    )
    _emit_session_event(
        sid,
        {
            "type": "rlm.started",
            "run_id": run_id,
            "ui_session_id": ui_session_id,
            "task_preview": task[:140],
            "source": source_desc,
            "root_model": root_model,
            "sub_model": sub_model,
            "max_iterations": caps.max_iterations,
            "max_subcalls": caps.max_subcalls,
            "timeout_seconds": caps.timeout_seconds,
        },
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
        progress_fn=_make_progress_fn(run_id, ui_session_id, sid),
        continuation_note=continuation_note,
    )
    if caps.max_depth > 1:  # caps is the single source of truth (built from settings above)
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
        _finalize_run_ui(sid, ui_session_id, run_id, result)
        return f"Error: RLM run {run_id} failed to start — {e}"
    finally:
        if session is not None:
            session._active_process = None

    runs.record_finish(run_id, run_dir, result)
    _finalize_run_ui(sid, ui_session_id, run_id, result)

    header = (
        f"[RLM run {run_id}: {result.status}, {result.iterations} iterations, "
        f"{result.subcalls} sub-calls, {result.duration:.0f}s]"
    )
    if result.partial:
        header += " (best-effort answer — the run did not submit a final answer before ending)"
    footer = f"\n\n(full trace: rlm/{run_id}/trace.jsonl in the workspace)"
    # Advertise continuation whenever the run left something worth reusing —
    # session a45fa830cef9's agent restarted from scratch because nothing told
    # it the dead run's work was recoverable.
    salvageable = bool(result.answer and result.answer.strip()) or (run_dir / "draft.txt").exists()
    if result.status != "completed" and salvageable:
        footer += (
            f"\n(partial work is saved — to build on it instead of restarting, call rlm_process again with "
            f'continue_from="{run_id}", the same source, and a task narrowed to what remains)'
        )
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
            "The optional `model` overrides the sub-call model for this run. If a run ends partial "
            "(timeout/caps), its durable outputs are saved — call again with continue_from=<run_id>, the "
            "same source, and a narrowed task to build on that work instead of restarting."
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
                "continue_from": {
                    "type": "string",
                    "description": (
                        "Run id of a prior rlm_process run on the SAME source to continue. Its draft, "
                        "answer, and completed sub-LLM outputs are pre-loaded into the REPL as `prior`, so "
                        "paid-for work is reused rather than repeated. The source must be identical "
                        "(verified by content hash)."
                    ),
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
        safety_level="caution",
        source="extension",
    )
