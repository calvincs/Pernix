"""Pernix — Tool execution with parallel/sequential support and health tracking."""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from dataclasses import dataclass, field

from config import settings
from core.tools.registry import ToolRegistry

logger = logging.getLogger("pernix.tools.executor")

# Distinguishes concurrent dispatches of the same tool so each call's
# subprocesses stay separable. count() is atomic under the GIL.
_call_id_counter = itertools.count()

# Tool execution runs on its own threads, never on asyncio's default executor.
#
# asyncio.to_thread() dispatches to the loop's default ThreadPoolExecutor,
# sized min(32, cpu_count + 4) — 20 threads on the deployment box. Every API
# route also hops that pool for its DB reads (`await asyncio.to_thread(db...)`
# in api/routers/*, ~150 call sites). Running tools there put the web UI in
# direct competition with agent work for the same 20 slots: one `bash` call can
# hold a slot for BASH_MAX_TIMEOUT (30 minutes) and http_get for ~150s (15s ×
# 10 redirect hops), so a handful of concurrent tool calls starved every API
# request. The symptom is a UI that looks hung while the event loop is idle and
# CPU sits near zero, recovering in batches as tool calls release slots.
#
# Two dedicated pools instead, so the default executor stays reserved for the
# API and framework:
#   _tool_executor      — ordinary tool calls. Bounded, so runaway tool fan-out
#                         cannot exhaust threads or spawn unbounded subprocesses.
#   _long_poll_executor — ToolDef.long_poll tools (await_workers, rlm_process),
#                         which block for 30-60 minutes waiting on
#                         OTHER work. They keep their own pool: parking them
#                         alongside ordinary calls would let a few orchestrations
#                         occupy every tool slot while the workers they wait on
#                         need those same slots to run their tools — the
#                         starvation deadlock this split exists to prevent.
_tool_executor = None
_long_poll_executor = None


def _get_tool_executor():
    global _tool_executor
    if _tool_executor is None:
        from concurrent.futures import ThreadPoolExecutor

        _tool_executor = ThreadPoolExecutor(
            max_workers=max(4, int(settings.tool_executor_workers)),
            thread_name_prefix="pernix-tool",
        )
    return _tool_executor


def _get_long_poll_executor():
    global _long_poll_executor
    if _long_poll_executor is None:
        from concurrent.futures import ThreadPoolExecutor

        _long_poll_executor = ThreadPoolExecutor(max_workers=16, thread_name_prefix="pernix-longpoll")
    return _long_poll_executor


@dataclass
class ToolExecutionResult:
    """Result from executing a single tool."""

    tool_name: str
    content: str
    was_error: bool
    latency_ms: int
    from_cache: bool = False
    metadata: dict = field(default_factory=dict)


# Grace added on top of a resolved dispatch timeout. The tool's own internal
# timeout (e.g. bash's process.communicate) must fire FIRST so the model gets
# the tool's own diagnostic instead of the executor's generic timeout error,
# and so the worker thread unwinds on its own.
_DISPATCH_TIMEOUT_GRACE_S = 5

# How long a dispatch may sit in a bounded pool's queue before we stop waiting
# for a thread. Generous, because a legitimately busy pool recovers: with 32
# tool slots, exceeding this means either a genuine fan-out storm or slots held
# by long occupants, and in both cases a clear "pool saturated" error beats a
# misleading per-tool timeout. Kept as a constant rather than a setting — it is
# a diagnostic backstop, and the actionable knob is the pool size itself.
_QUEUE_WAIT_CEILING_S = 300


def _resolve_timeout(tool, arguments: dict | None) -> int:
    """Resolve the dispatch timeout for one call.

    A tool whose schema exposes a `timeout` parameter must also declare
    `max_timeout` at registration; otherwise asyncio.wait_for below caps the
    call at the tool's default and the caller's override does nothing at all.

    The grace is added on EVERY path, not just the caller-override one. A
    default `bash` call gives dispatch and the tool the same budget
    (shell_timeout), but the dispatch clock starts before setup the tool does
    not count — cold workspace-venv creation alone has a 60s budget — so the
    dispatcher wins the race and the model gets the executor's generic timeout
    instead of bash's own diagnostic, with the worker thread still blocked.
    """
    base = tool.timeout
    ceiling = tool.max_timeout
    if ceiling <= 0:
        return base + _DISPATCH_TIMEOUT_GRACE_S
    try:
        requested = int((arguments or {}).get("timeout") or 0)
    except (TypeError, ValueError):
        requested = 0
    if requested <= 0:
        return base + _DISPATCH_TIMEOUT_GRACE_S
    return min(max(requested, base), ceiling) + _DISPATCH_TIMEOUT_GRACE_S


def _kill_tool_subprocess(context: dict | None, call_id: str) -> None:
    """Kill the subprocesses spawned by ONE dispatch call after its timeout.

    A worker thread cannot be cancelled: when wait_for gives up, the thread
    stays blocked in the tool until the tool itself returns. For bash that
    means holding a tool-executor thread AND a live process tree for the
    remainder of the child's runtime. Killing the process group lets the thread
    unwind promptly.

    Scoped to `call_id` rather than the whole session: sibling tool calls that
    are still running healthily must not be killed because this one timed out.
    Best-effort — the tool releases its registration in its own finally, so an
    empty list here just means the call already finished.
    """
    sid = (context or {}).get("session_id", "")
    if not sid:
        return
    try:
        from sessions.manager import get_manager

        session = get_manager().get(sid)
        if session is None:
            return
        from core.tools.builtin.core_tools import _kill_process_tree

        for proc in session.processes_for(call_id):
            if proc is None or proc.poll() is not None:
                continue
            _kill_process_tree(proc)
            logger.warning("Killed subprocess %s after tool dispatch timeout", proc.pid)
    except Exception as e:
        logger.debug("Post-timeout subprocess kill skipped: %s", e)


def _batch_timeout(indices: list[int], tool_calls: list[dict], registry: ToolRegistry) -> int:
    """Backstop timeout for the parallel gather.

    Every _execute_single already enforces its own per-call timeout and
    degrades to a per-tool error result, so this outer bound exists only to
    catch a gather that wedges outside those waits. Size it to the slowest
    tool actually in the batch so it can never fire first — if it does, the
    TimeoutError escapes execute_tool_round and destroys the whole round,
    including the results of calls that already completed.
    """
    slowest = 0
    for i in indices:
        tool = registry.get(tool_calls[i].get("name", ""))
        if tool is None:
            continue
        slowest = max(slowest, _resolve_timeout(tool, tool_calls[i].get("arguments", {})))
    return max(settings.tool_timeout, slowest) + _DISPATCH_TIMEOUT_GRACE_S


def _is_unattended_session(sid: str) -> bool:
    """Return True for cron/canary sessions and workers spawned from them.

    These run without a user present, so the ask_user → approve_dangerous_tool
    flow is not viable and the dangerous gate is skipped.
    """
    if not sid:
        return False
    from sessions.manager import get_manager

    s = get_manager().get(sid)
    if s is None:
        return False
    if s.session_type in ("cron", "canary"):
        return True
    if s.session_type == "worker" and s.parent_session_id:
        parent = get_manager().get(s.parent_session_id)
        return bool(parent and parent.session_type in ("cron", "canary"))
    return False


async def _execute_single(
    name: str,
    arguments: dict,
    context: dict | None,
    registry: ToolRegistry,
) -> ToolExecutionResult:
    """Execute a single tool with its registered timeout."""
    tool = registry.get(name)
    if not tool:
        return ToolExecutionResult(
            tool_name=name,
            content=f"Error: Unknown tool '{name}'. Use discover_tools to find available tools.",
            was_error=True,
            latency_ms=0,
        )
    if registry.is_disabled(name):
        return ToolExecutionResult(
            tool_name=name,
            content=(f"Error: Tool '{name}' is disabled. " "Enable it in Explorer > Tools before use."),
            was_error=True,
            latency_ms=0,
        )

    # Enforce denied_session_types (plan §5, generalizing worker_allowed):
    # workers can't spawn sub-workers; canaries can't write memory; etc.
    sid = (context or {}).get("session_id", "")
    session_type = ""
    workspace_override: str | None = None
    if sid:
        from sessions.manager import get_manager
        from sessions.state import turn_state

        s = get_manager().get(sid)
        session_type = (s.session_type or "") if s else ""
        workspace_override = getattr(s, "workspace_override", None) if s else None
    # Retry effector (audit P1f): reflect can mechanically disable tools for
    # the current retry attempt; the schema filter removes them, this guard
    # catches a model that calls one anyway.
    _retry_excluded = turn_state(s).retry_excluded_tools if sid and s else None
    if _retry_excluded and name in _retry_excluded:
        return ToolExecutionResult(
            tool_name=name,
            content=(
                f"Error: Tool '{name}' is disabled for this retry attempt — the previous "
                "attempt failed because of how it was used. Follow the retry strategy "
                "without it."
            ),
            was_error=True,
            latency_ms=0,
        )

    if session_type and session_type in tool.denied_session_types:
        return ToolExecutionResult(
            tool_name=name,
            content=f"Error: Tool '{name}' cannot be used in {session_type} sessions.",
            was_error=True,
            latency_ms=0,
        )

    # Enforce safety_level="dangerous" gate.
    # Both parent and worker sessions must pass — workers cannot escalate
    # privilege by spawning a sub-agent to call dangerous tools.
    # Three approval paths:
    #   1. Global: settings.auto_approve_dangerous = True (disables gate entirely).
    #   2. Unattended: cron sessions and workers spawned from cron sessions skip
    #      the gate — no user is present to answer ask_user prompts.
    #   3. Per-session: user confirmed via ask_user + approve_dangerous_tool(),
    #      which sets session._approved_dangerous_tools. Persists for session lifetime.
    if tool.safety_level == "dangerous":
        from config import settings

        if not settings.auto_approve_dangerous and not _is_unattended_session(sid):
            # Check per-session approval granted by approve_dangerous_tool().
            # Non-persistent approvals are consumed (removed) on first use so
            # each distinct dangerous call requires its own ask_user + approval.
            _session_approved = False
            if sid:
                from sessions.manager import get_manager as _get_mgr

                _s = _get_mgr().get(sid)
                if _s:
                    _approvals: dict = getattr(_s, "_approved_dangerous_tools", {})
                    if name in _approvals:
                        entry = _approvals[name]
                        if entry.get("persistent", False):
                            # Persistent approvals are deliberately broad — the
                            # user consented to the stated scope covering
                            # repeated calls (e.g. "browse several pages").
                            _session_approved = True
                        else:
                            # Single-use approvals must actually cover this
                            # call: every significant string argument has to
                            # appear in the scope the user was shown. Without
                            # this, approving "delete skill foo" unlocks
                            # delete_skill(name="bar") — the gate would match
                            # on tool name alone.
                            _scope_text = " ".join(str(entry.get("scope", "")).lower().split())
                            _uncovered = [
                                k
                                for k, v in (arguments or {}).items()
                                if isinstance(v, str)
                                and len(v.strip()) >= 4
                                and " ".join(v.lower().split()) not in _scope_text
                            ]
                            if _uncovered:
                                # Leave the approval in place — it may match the
                                # call the user actually confirmed.
                                return ToolExecutionResult(
                                    tool_name=name,
                                    content=(
                                        f"Error: The approved scope ({entry.get('scope', '')!r}) does not "
                                        f"mention the value(s) of argument(s) {', '.join(sorted(_uncovered))} "
                                        f"in this call. Approval covers only the exact action the user "
                                        f"confirmed. Either call the tool with the approved values, or run "
                                        f"ask_user + approve_dangerous_tool again with a scope quoting the "
                                        f"exact command/URL/name you intend to use."
                                    ),
                                    was_error=True,
                                    latency_ms=0,
                                )
                            _session_approved = True
                            # Consume: this approval covers only this one call.
                            del _approvals[name]

            if not _session_approved:
                return ToolExecutionResult(
                    tool_name=name,
                    content=(
                        f"Error: Tool '{name}' requires explicit user approval for this specific call.\n"
                        f"Step 1 — call ask_user() describing EXACTLY what you will do "
                        f"(e.g. the command, URL, or file path — not just the tool name).\n"
                        f"Step 2 — after the user confirms, call "
                        f"approve_dangerous_tool(tool_name='{name}', scope='<exact description>') "
                        f"to unlock this specific action. Approval is consumed after one use; "
                        f"a different call to the same tool requires a new approval.\n"
                        f"Use persistent=True only for genuinely repetitive low-risk actions "
                        f"(e.g. 'browse several pages while researching').\n"
                        f"Alternatively, enable auto_approve_dangerous in Settings."
                    ),
                    was_error=True,
                    latency_ms=0,
                )

    # Route custom tools to the workspace venv before execution.
    # ensure_workspace_venv_on_path() is idempotent — no-op if already set.
    if tool.source == "custom":
        from core.tools.paths import ensure_workspace_venv_on_path

        ensure_workspace_venv_on_path()

    timeout = _resolve_timeout(tool, arguments)
    start = time.monotonic()
    call_id = f"{name}@{next(_call_id_counter)}"
    try:
        # Capture the running event loop so tools on worker threads can
        # schedule coroutines back onto it via run_coroutine_threadsafe().
        loop = asyncio.get_running_loop()
        ctx = dict(context) if context else {}
        ctx["_loop"] = loop
        # Identifies this dispatch to any subprocess the tool registers, so a
        # timeout kills this call's children and nobody else's.
        ctx["_call_id"] = call_id
        if workspace_override:
            ctx["workspace_override"] = workspace_override

        # Never asyncio.to_thread here: that is the default executor the API
        # depends on. See the pool comments at the top of this module.
        executor = _get_long_poll_executor() if tool.long_poll else _get_tool_executor()

        # Both pools are bounded, so a dispatch can sit in the queue before any
        # thread picks it up — and asyncio.wait_for starts its clock at SUBMIT,
        # not at first execution. Charging queue time against the tool's own
        # budget meant that under heavy fan-out a tool could be reported as
        # "timed out after 300s" without having executed a single line, which
        # is both wrong and un-actionable: the fix for a saturated pool is not
        # a longer per-tool timeout. So the wait is split — a ceiling on queue
        # time, then the tool's real timeout measured from the moment a thread
        # actually enters it.
        started = asyncio.Event()

        def _runner():
            # Runs on the pool thread. call_soon_threadsafe, not Event.set:
            # asyncio.Event is not thread-safe.
            loop.call_soon_threadsafe(started.set)
            return registry.execute_sync(name, arguments, ctx)

        fut = loop.run_in_executor(executor, _runner)
        waiter = asyncio.ensure_future(started.wait())
        try:
            await asyncio.wait({fut, waiter}, timeout=_QUEUE_WAIT_CEILING_S, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not waiter.done():
                waiter.cancel()

        if not started.is_set() and not fut.done():
            # Never got a thread inside the ceiling. Report saturation as
            # itself rather than as a tool timeout — the two have different
            # causes and different fixes. fut.cancel() drops the queued item
            # if the executor has not dequeued it yet; if it loses that race
            # the call runs to completion with nobody awaiting it, which is
            # the same exposure a dispatch timeout already carries.
            fut.cancel()
            latency = int((time.monotonic() - start) * 1000)
            registry.metrics[name].record_timeout(latency)
            logger.warning(
                "Tool '%s' never started: %s pool saturated for %ds (%s)",
                name,
                "long-poll" if tool.long_poll else "tool",
                _QUEUE_WAIT_CEILING_S,
                (
                    "long-poll pool is a fixed 16 — too many concurrent orchestrations"
                    if tool.long_poll
                    else "raise settings.tool_executor_workers"
                ),
            )
            return ToolExecutionResult(
                tool_name=name,
                content=(
                    f"Error: Tool '{name}' never started — the executor pool was saturated for "
                    f"{_QUEUE_WAIT_CEILING_S}s. This is thread exhaustion, not a slow tool."
                ),
                was_error=True,
                latency_ms=latency,
            )

        raw = await asyncio.wait_for(fut, timeout=timeout)
        latency = int((time.monotonic() - start) * 1000)

        # execute_sync may return (str, dict) for structured metadata
        if isinstance(raw, tuple) and len(raw) == 2:
            result, metadata = raw
        else:
            result, metadata = raw, {}

        was_error = (not result) or result.startswith("Error:")

        if was_error:
            registry.metrics[name].record_failure(result, latency)
        else:
            registry.metrics[name].record_success(latency)

        return ToolExecutionResult(
            tool_name=name,
            content=result,
            was_error=was_error,
            latency_ms=latency,
            metadata=metadata,
        )
    except asyncio.TimeoutError:
        latency = int((time.monotonic() - start) * 1000)
        registry.metrics[name].record_timeout(latency)
        # The worker thread is still blocked in the tool and cannot be
        # cancelled. Kill any subprocess it spawned so it unwinds instead of
        # holding a tool-executor thread for the child's full runtime.
        _kill_tool_subprocess(context, call_id)
        return ToolExecutionResult(
            tool_name=name,
            content=f"Error: Tool '{name}' timed out after {timeout}s",
            was_error=True,
            latency_ms=latency,
        )
    except asyncio.CancelledError:
        latency = int((time.monotonic() - start) * 1000)
        registry.metrics[name].record_failure("cancelled", latency)
        return ToolExecutionResult(
            tool_name=name,
            content=f"Error: Tool '{name}' was cancelled",
            was_error=True,
            latency_ms=latency,
        )
    except Exception as e:
        latency = int((time.monotonic() - start) * 1000)
        registry.metrics[name].record_failure(str(e), latency)
        return ToolExecutionResult(
            tool_name=name,
            content=f"Error: {e}",
            was_error=True,
            latency_ms=latency,
        )


async def execute_tool_round(
    tool_calls: list[dict],
    context: dict | None,
    registry: ToolRegistry,
) -> list[ToolExecutionResult]:
    """Execute a round of tool calls, parallelizing where safe.

    tool_calls format: [{"name": str, "arguments": dict}, ...]
    Parallel-safe tools run concurrently. Mutating tools run sequentially.

    ORDERING CONTRACT: `results[i]` is always the result of `tool_calls[i]`.
    Callers (core/agent.py) pair the two lists positionally to attach each
    result to its originating call's tool_call_id, so the returned order
    must mirror the input order — NOT the execution order. Parallel-safe
    calls are dispatched first for latency, but their results are written
    back into their original slots.
    """
    parallel_idx: list[int] = []
    sequential_idx: list[int] = []

    for i, tc in enumerate(tool_calls):
        name = tc.get("name", "")
        tool = registry.get(name)
        if tool and tool.parallel_safe:
            parallel_idx.append(i)
        else:
            sequential_idx.append(i)

    results: list[ToolExecutionResult | None] = [None] * len(tool_calls)

    # Run parallel-safe tools concurrently
    if parallel_idx:
        tasks = [
            _execute_single(
                tool_calls[i]["name"],
                tool_calls[i].get("arguments", {}),
                context,
                registry,
            )
            for i in parallel_idx
        ]
        parallel_results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=_batch_timeout(parallel_idx, tool_calls, registry),
        )
        for i, result in zip(parallel_idx, parallel_results):
            if isinstance(result, asyncio.CancelledError):
                raise result  # Propagate cancellation, don't swallow
            if isinstance(result, BaseException):
                results[i] = ToolExecutionResult(
                    tool_name=tool_calls[i]["name"],
                    content=f"Error: {result}",
                    was_error=True,
                    latency_ms=0,
                )
            else:
                results[i] = result

    # Run sequential tools in order
    for i in sequential_idx:
        results[i] = await _execute_single(
            tool_calls[i]["name"],
            tool_calls[i].get("arguments", {}),
            context,
            registry,
        )

    # Every slot is filled by construction: each index lands in exactly one
    # of the two buckets, and both loops assign unconditionally.
    final = [r for r in results if r is not None]
    await _bind_large_results(tool_calls, final, context)
    return final


# Binding (prompt-as-a-variable, plan 2c) applies to ANY tool's oversized
# result except the few where it is pointless or harmful. An exclusion set,
# not an allowlist: a big payload is a big payload whatever produced it, and
# tools added later get binding by default instead of silently missing it.
#
#   repl          — output is already kernel-side; binding it would copy the
#                   kernel's own print back into the kernel.
#   rlm_process   — returns a synthesized answer over source material, not the
#                   material; there is nothing to slice.
#   ask_user / notify_* — conversational turns, never data to slice.
_BINDING_EXCLUDED = frozenset(
    {
        "repl",
        "rlm_process",
        "ask_user",
        "notify_user",
        "notify_parent",
        # Instruction-shaped outputs: the model must READ these whole, in
        # order — a head/tail stub of a 20K SKILL.md silently drops the
        # middle 90% of the procedure. Data-shaped outputs (bash, grep,
        # file_read, http_get, …) stay binding-eligible.
        "load_skill",
        "read_skill_instructions",
        "read_skill_resource",
        "discover_tools",
        "get_worker_result",
        "get_worker_transcript",
    }
)

_BIND_HEAD_CHARS = 2_000
_BIND_TAIL_CHARS = 800


async def _bind_large_results(tool_calls: list[dict], results: list[ToolExecutionResult], context: dict | None) -> None:
    """Post-pass: oversized results from any non-excluded tool are loaded
    into the session kernel as tool_result_<n> variables, spilled to a
    durable sidecar file (the transcript stays reconstructible — this is a
    view transform with a durable copy, not a discard), and replaced
    in-context by a head/tail stub with a pointer.

    Runs OUTSIDE per-call timeouts. Any failure leaves the result untouched.
    """
    from config import settings

    if not settings.session_kernel_enabled:
        return
    threshold = int(settings.large_result_bind_threshold)
    session_id = (context or {}).get("session_id", "")
    if not session_id or threshold <= 0:
        return

    for tc, res in zip(tool_calls, results):
        if tc.get("name") in _BINDING_EXCLUDED or res.was_error:
            continue
        content = res.content or ""
        if len(content) <= threshold:
            continue
        try:
            # The WHOLE sequence runs off the loop. get_or_create can trip an
            # LRU eviction whose shutdown snapshot is up to SNAPSHOT_TIMEOUT
            # (300s) of blocking dill IO, and next_bind_ordinal scans the
            # payloads dir — neither may run on the event loop.
            def _spill_and_bind(sid=session_id, c=content):
                from core.kernel import get_kernel_registry

                k = get_kernel_registry().get_or_create(sid)
                v = f"tool_result_{k.next_bind_ordinal()}"
                p = k.payloads_dir / f"{v}.txt"
                k.payloads_dir.mkdir(parents=True, exist_ok=True)
                p.write_text(c, encoding="utf-8")
                k.bind_variable(v, c)
                return v, p

            var, payload_path = await asyncio.to_thread(_spill_and_bind)
        except Exception as e:
            logger.warning("Result binding failed for %s: %s", tc.get("name"), e)
            continue

        # A result that a tool already truncated is a preview over its own
        # disk spill, not the whole payload — don't call the bound copy "full".
        label = "result preview" if (res.metadata or {}).get("truncated") else "full result"
        res.metadata = {
            **(res.metadata or {}),
            "bound_var": var,
            "payload_path": str(payload_path),
            "orig_chars": len(content),
        }
        res.content = (
            f"{content[:_BIND_HEAD_CHARS]}\n"
            f"… [{len(content):,} chars total — middle omitted] …\n"
            f"{content[-_BIND_TAIL_CHARS:]}\n"
            f"[{label} bound as `{var}` in the session kernel (durable copy: "
            f"{payload_path}). Slice/search it with the repl tool instead of re-reading, "
            f'e.g. repl(code="print({var}[:1000])").]'
        )
        logger.info(
            "Bound %s result (%d chars) as %s for session %s",
            tc.get("name"),
            len(content),
            var,
            session_id[:12],
        )
