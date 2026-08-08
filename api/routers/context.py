"""Pernix — Context introspection endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from config import settings
from db import models as db

router = APIRouter(tags=["context"])


@router.get("/api/context/{session_id}")
async def context_breakdown(session_id: str):
    """Compiled context snapshot as actually sent to the LLM.

    Mirrors `compile_context()` so the status-bar indicator reflects the
    post-compaction, post-prune, post-trim payload — not the raw DB sum.
    """
    from core.context.compiler import compile_context
    from core.llm.budget import derive_max_output, derive_model_budget
    from core.tools.registry import get_registry
    from sessions.manager import get_manager

    session = get_manager().get(session_id)
    # Mirror the agent loop's budget resolution (override → model-derived →
    # fallback) so the status bar reports the budget the agent actually uses.
    model = (session.model_override if session else None) or settings.llm_model
    effective_budget = (
        (session.context_budget_override if session else None) or derive_model_budget(model) or settings.context_budget
    )

    registry = get_registry()
    tool_schemas = registry.get_schemas([t.name for t in registry.enabled_tools()])

    payload = compile_context(
        session_id=session_id,
        tool_schemas=tool_schemas,
        context_budget=effective_budget,
        max_output_tokens=derive_max_output(model),
    )

    raw_messages = db.get_messages(session_id)
    compaction_count = sum(1 for m in raw_messages if m["role"] == "compaction")
    message_count = len(raw_messages)

    history_budget = payload.history_budget or 1
    history_pct = round(100 * payload.metadata.history_tokens / history_budget)
    utilization_pct = round(100 * payload.token_count / max(effective_budget, 1))

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
        "budget": effective_budget,
        "history_budget": payload.history_budget,
        "utilization_pct": utilization_pct,
        "history_pct": history_pct,
        "status": status,
        "needs_compaction": payload.needs_compaction,
        "has_compaction_summary": payload.has_compaction_summary,
        "compaction_count": compaction_count,
        "messages_trimmed": payload.metadata.messages_trimmed,
        "thresholds": {
            "compaction": compaction_threshold,
            "critical": critical_threshold,
        },
    }


@router.get("/api/context/{session_id}/payload")
async def context_payload(session_id: str):
    """Full assembled context as it would be sent to the LLM (transparency endpoint)."""
    from core.context.compiler import compile_context
    from core.tools.registry import get_registry

    registry = get_registry()
    core_tools = [t.name for t in registry.enabled_tools()]
    tool_schemas = registry.get_schemas(core_tools)

    payload = compile_context(
        session_id=session_id,
        tool_schemas=tool_schemas,
    )

    return {
        "system_prompt": payload.messages[0]["content"] if payload.messages else "",
        "messages": payload.messages,
        "tools": payload.tools,
        "token_breakdown": {
            "system": payload.metadata.system_tokens,
            "history": payload.metadata.history_tokens,
            "tools": payload.metadata.tool_schema_tokens,
            "total": payload.token_count,
            "budget": settings.context_budget,
        },
        "messages_included": payload.metadata.messages_included,
        "messages_trimmed": payload.metadata.messages_trimmed,
        "needs_compaction": payload.needs_compaction,
        "has_compaction_summary": payload.has_compaction_summary,
    }
