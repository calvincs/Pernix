"""Pernix — Context introspection endpoints."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from fastapi import APIRouter, HTTPException

from config import settings
from db import models as db

router = APIRouter(tags=["context"])


@dataclass(frozen=True)
class _CompileInputs:
    """Everything the compiler needs, snapshotted once.

    The session's model and budget overrides are mutable and live on the
    in-memory session — a settings change landing between two reads inside
    one request would compile against one budget and report another. Reading
    them once into a frozen record is the whole point of this type.
    """

    session_id: str
    model: str
    budget: int
    max_output: int
    supports_vision: bool
    supports_audio: bool
    tool_schemas: list[dict]

    def kwargs(self) -> dict:
        return {
            "session_id": self.session_id,
            "tool_schemas": self.tool_schemas,
            "context_budget": self.budget,
            "max_output_tokens": self.max_output,
            "model_name": self.model,
            "supports_vision": self.supports_vision,
            "supports_audio": self.supports_audio,
        }


async def _resolve(session_id: str) -> _CompileInputs:
    """Mirror the agent loop's own resolution — model, modalities, budget,
    output cap, tool set — for a session that exists.

    Both endpoints in this module claim to show what the agent sends, so
    both have to derive their inputs the way `core/agent.py` derives them
    (`_resolve_effective_model` + `core/llm/budget.py`). `/payload` used to
    call the compiler with the session id and nothing else, which meant it
    reported a system prompt the agent never builds, a budget nobody uses
    and — on a transcript longer than the real budget — a trim count of
    zero for messages that are in fact dropped. A fast endpoint that still
    lies is not a fix.

    404s on an unknown id. Compiling for a session that does not exist
    answered HTTP 200 with a full system prompt and tool schemas billed
    against nothing.
    """
    from core.llm.budget import derive_max_output, derive_model_budget, ensure_model_known
    from core.tools.registry import get_registry
    from sessions.manager import get_manager

    if not await asyncio.to_thread(db.get_session, session_id):
        raise HTTPException(404, detail=f"Session {session_id} not found")

    session = get_manager().get(session_id)
    # One read each: whatever these are at this instant is what the whole
    # response is computed from.
    model_override = session.model_override if session else None
    budget_override = session.context_budget_override if session else None

    model = model_override or settings.llm_model
    # Registry refresh on a miss, exactly as the agent does before deriving —
    # a model pulled onto the host after startup would otherwise report the
    # manual fallback window.
    await ensure_model_known(model)

    supports_vision = False
    supports_audio = False
    try:
        from core.llm.client import get_llm_client

        registry_models = get_llm_client().router.registry
        info = registry_models.get_model_info(registry_models.resolve_model_id(model))
        if info is not None:
            model = registry_models.resolve_model_id(model)
            supports_vision = bool(info.supports_vision)
            supports_audio = bool(info.supports_audio)
    except Exception:
        # An unreachable/unpopulated registry is the same answer the agent
        # gets from it: no declared modalities.
        pass

    registry = get_registry()
    return _CompileInputs(
        session_id=session_id,
        model=model,
        budget=budget_override or derive_model_budget(model) or settings.context_budget,
        max_output=derive_max_output(model),
        supports_vision=supports_vision,
        supports_audio=supports_audio,
        tool_schemas=registry.get_schemas([t.name for t in registry.enabled_tools()]),
    )


@router.get("/api/context/{session_id}")
async def context_breakdown(session_id: str):
    """Compiled context snapshot as actually sent to the LLM.

    Mirrors `compile_context()` so the status-bar indicator reflects the
    post-compaction, post-prune, post-trim payload — not the raw DB sum.

    Both halves run off the event loop. The compile reads every surviving
    message and tokenizes it; on a 13 MB transcript that held the loop for
    492 ms — 98 heartbeat ticks due, none delivered — while the UI polls
    this on every session selection and after every compaction. The counts
    beside it used to be a SECOND full read of the same history, thrown away
    after producing two integers; they are one indexed aggregate now.
    """
    from core.context.compiler import compile_context

    inputs = await _resolve(session_id)
    payload = await asyncio.to_thread(compile_context, **inputs.kwargs())
    message_count, compaction_count = await asyncio.to_thread(db.count_messages_and_compactions, session_id)

    history_budget = payload.history_budget or 1
    history_pct = round(100 * payload.metadata.history_tokens / history_budget)
    utilization_pct = round(100 * payload.token_count / max(inputs.budget, 1))

    compaction_threshold = settings.compaction_threshold
    critical_threshold = settings.context_critical_threshold
    ratio = payload.metadata.history_tokens / history_budget
    if ratio >= critical_threshold:
        status = "critical"
    elif ratio >= compaction_threshold:
        status = "approaching"
    else:
        status = "healthy"

    return {
        "session_id": session_id,
        "message_count": message_count,
        "total_tokens": payload.token_count,
        "history_tokens": payload.metadata.history_tokens,
        "system_tokens": payload.metadata.system_tokens,
        "tool_schema_tokens": payload.metadata.tool_schema_tokens,
        "budget": inputs.budget,
        "history_budget": payload.history_budget,
        "utilization_pct": utilization_pct,
        "history_pct": history_pct,
        "status": status,
        "needs_compaction": payload.needs_compaction,
        "has_compaction_summary": payload.has_compaction_summary,
        "compaction_count": compaction_count,
        "messages_trimmed": payload.metadata.messages_trimmed,
        "model": inputs.model,
        "thresholds": {
            "compaction": compaction_threshold,
            "critical": critical_threshold,
        },
    }


@router.get("/api/context/{session_id}/payload")
async def context_payload(session_id: str):
    """Full assembled context as it would be sent to the LLM (transparency endpoint).

    Same inputs as the status endpoint and as the agent: the session's model
    and budget overrides, the model's real window and output reservation,
    its declared modalities. Compiled off-loop for the same reason.
    """
    from core.context.compiler import compile_context

    inputs = await _resolve(session_id)
    payload = await asyncio.to_thread(compile_context, **inputs.kwargs())

    return {
        "system_prompt": payload.messages[0]["content"] if payload.messages else "",
        "messages": payload.messages,
        "tools": payload.tools,
        "token_breakdown": {
            "system": payload.metadata.system_tokens,
            "history": payload.metadata.history_tokens,
            "tools": payload.metadata.tool_schema_tokens,
            "total": payload.token_count,
            "budget": inputs.budget,
            "history_budget": payload.history_budget,
        },
        "model": inputs.model,
        "messages_included": payload.metadata.messages_included,
        "messages_trimmed": payload.metadata.messages_trimmed,
        "needs_compaction": payload.needs_compaction,
        "has_compaction_summary": payload.has_compaction_summary,
    }
