"""Pernix — Tool execution with parallel/sequential support and health tracking."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from config import settings
from core.tools.registry import ToolRegistry

logger = logging.getLogger("pernix.tools.executor")


@dataclass
class ToolExecutionResult:
    """Result from executing a single tool."""

    tool_name: str
    content: str
    was_error: bool
    latency_ms: int
    from_cache: bool = False
    metadata: dict = field(default_factory=dict)


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
            content=f"Error: Tool '{name}' is currently disabled.",
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

    # Enforce safety_level="dangerous" gate (LogAct-inspired voting concept).
    # Both parent and worker sessions must pass this gate.
    # Workers are not trusted to bypass user confirmation — an LLM could
    # otherwise escalate by spawning a worker to call dangerous tools.
    if tool.safety_level == "dangerous":
        from config import settings

        if not settings.auto_approve_dangerous:
            return ToolExecutionResult(
                tool_name=name,
                content=(
                    f"Error: Tool '{name}' is classified as dangerous and requires "
                    f"explicit confirmation. Use ask_user to confirm with the user "
                    f"before calling this tool, or enable auto_approve_dangerous in settings."
                ),
                was_error=True,
                latency_ms=0,
            )

    timeout = tool.timeout
    start = time.monotonic()
    try:
        # Capture the running event loop so tools on worker threads can
        # schedule coroutines back onto it via run_coroutine_threadsafe().
        loop = asyncio.get_running_loop()
        ctx = dict(context) if context else {}
        ctx["_loop"] = loop
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
