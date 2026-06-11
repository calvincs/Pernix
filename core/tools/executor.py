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


def _is_unattended_session(sid: str) -> bool:
    """Return True for cron sessions and workers spawned from cron sessions.

    These run without a user present, so the ask_user → approve_dangerous_tool
    flow is not viable and the dangerous gate is skipped.
    """
    if not sid:
        return False
    from sessions.manager import get_manager

    s = get_manager().get(sid)
    if s is None:
        return False
    if s.session_type == "cron":
        return True
    if s.session_type == "worker" and s.parent_session_id:
        parent = get_manager().get(s.parent_session_id)
        return bool(parent and parent.session_type == "cron")
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

    # Enforce worker_allowed restriction (prevent workers from spawning sub-workers)
    sid = (context or {}).get("session_id", "")
    is_worker = False
    if sid:
        from sessions.manager import get_manager

        s = get_manager().get(sid)
        is_worker = bool(s and s.session_type == "worker")
    if not tool.worker_allowed and is_worker:
        return ToolExecutionResult(
            tool_name=name,
            content=f"Error: Tool '{name}' cannot be used in worker sessions.",
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

    timeout = tool.timeout
    start = time.monotonic()
    try:
        # Capture the running event loop so tools on worker threads can
        # schedule coroutines back onto it via run_coroutine_threadsafe().
        loop = asyncio.get_running_loop()
        ctx = dict(context) if context else {}
        ctx["_loop"] = loop
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
    """
    parallel = []
    sequential = []

    for tc in tool_calls:
        name = tc.get("name", "")
        tool = registry.get(name)
        if tool and tool.parallel_safe:
            parallel.append(tc)
        else:
            sequential.append(tc)

    results: list[ToolExecutionResult] = []

    # Run parallel-safe tools concurrently
    if parallel:
        tasks = [_execute_single(tc["name"], tc.get("arguments", {}), context, registry) for tc in parallel]
        parallel_results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=settings.tool_timeout,
        )
        for tc, result in zip(parallel, parallel_results):
            if isinstance(result, asyncio.CancelledError):
                raise result  # Propagate cancellation, don't swallow
            if isinstance(result, BaseException):
                results.append(
                    ToolExecutionResult(
                        tool_name=tc["name"],
                        content=f"Error: {result}",
                        was_error=True,
                        latency_ms=0,
                    )
                )
            else:
                results.append(result)

    # Run sequential tools in order
    for tc in sequential:
        result = await _execute_single(tc["name"], tc.get("arguments", {}), context, registry)
        results.append(result)

    return results
