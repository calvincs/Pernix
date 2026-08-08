"""Pernix — Tool execution with parallel/sequential support and health tracking."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from config import settings
from core.tools.registry import ToolRegistry

logger = logging.getLogger("pernix.tools.executor")

# Dedicated executor for long-poll tools — see ToolDef.long_poll. Sized for
# concurrent orchestrations, not throughput: each occupant is 99% blocked.
_long_poll_executor = None


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


def _kill_tool_subprocess(context: dict | None) -> None:
    """Kill the session's tracked subprocess after a dispatch timeout.

    asyncio.to_thread cannot be cancelled: when wait_for gives up, the worker
    thread stays blocked in the tool until the tool itself returns. For bash
    that means holding a shared-executor thread AND a live process tree for the
    remainder of the child's runtime. Killing the process group lets the thread
    unwind promptly. Best-effort — the tool clears _active_process in its own
    finally, so a None here just means the call already finished.
    """
    sid = (context or {}).get("session_id", "")
    if not sid:
        return
    try:
        from sessions.manager import get_manager

        session = get_manager().get(sid)
        proc = getattr(session, "_active_process", None) if session else None
        if proc is None or proc.poll() is not None:
            return
        from core.tools.builtin.core_tools import _kill_process_tree

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
    try:
        # Capture the running event loop so tools on worker threads can
        # schedule coroutines back onto it via run_coroutine_threadsafe().
        loop = asyncio.get_running_loop()
        ctx = dict(context) if context else {}
        ctx["_loop"] = loop
        if workspace_override:
            ctx["workspace_override"] = workspace_override
        if tool.long_poll:
            # Long-poll tools (await_workers, run_workflow) hold their thread
            # for up to 30-60 minutes while the workers they wait on need
            # threads from the SHARED to_thread pool for their own tools.
            # Enough concurrent orchestrations could occupy every shared slot:
            # workers' tools then queue, "time out" without executing, the
            # workers stall, and the blockers keep holding their threads —
            # starvation deadlock. A dedicated executor caps the blast radius.
            import functools

            raw = await asyncio.wait_for(
                loop.run_in_executor(
                    _get_long_poll_executor(),
                    functools.partial(registry.execute_sync, name, arguments, ctx),
                ),
                timeout=timeout,
            )
        else:
            raw = await asyncio.wait_for(
                asyncio.to_thread(registry.execute_sync, name, arguments, ctx),
                timeout=timeout,
            )
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
        # The to_thread worker is still blocked in the tool and cannot be
        # cancelled. Kill any subprocess it spawned so it unwinds instead of
        # holding a shared-executor thread for the child's full runtime.
        _kill_tool_subprocess(context)
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
