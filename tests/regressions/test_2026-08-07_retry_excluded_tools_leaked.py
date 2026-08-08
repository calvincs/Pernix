"""Regression: reflect's tool exclusions leaked from one retry into the next.

Shipped defect (architecture review 2026-08-07, §3.5): the retry effector
assigned session.retry_excluded_tools only `if excluded:`. The manager clears
the set on a fresh prompt and at turn start, never between retry N and retry
N+1 — so a tool named by retry #1's verdict stayed disabled for retry #2 even
when that verdict named nothing, silently narrowing the agent's tool surface
for the rest of the turn.

Fix: the set is cleared where each new verdict is applied, so every retry runs
with exactly the exclusions its own verdict asked for.
"""

import json

from core.llm.types import ChatResponse, TokenUsage
from db import models as db
from sessions.hooks import _maybe_reflect
from sessions.state import AgentSession


def _verdict(**overrides) -> ChatResponse:
    payload = {
        "verdict": "retry",
        "reasoning": "Report not created",
        "diagnostic": "Wrong approach",
        "what_worked": "",
        "what_failed": "all tools",
        "strategy": "Use a different method",
    }
    payload.update(overrides)
    return ChatResponse(
        content=json.dumps(payload),
        tool_calls=None,
        usage=TokenUsage(10, 5, 15),
        model="test",
        provider="fake",
        finish_reason="stop",
    )


async def _run_reflect(mock_llm_client, monkeypatch, response, *, excluded):
    monkeypatch.setattr("config.settings.reflect_enabled", True)
    monkeypatch.setattr("config.settings.reflect_min_messages", 2)
    monkeypatch.setattr("config.settings.reflect_max_retries", 3)

    sid = db.create_session()
    db.add_message(sid, "user", "Create a report")
    db.add_message(sid, "assistant", "I tried but failed")
    db.add_message(sid, "tool", "error")

    session_obj = AgentSession(session_id=sid)
    # State left behind by the previous retry in this same turn.
    session_obj.turn.reflect_count = 1
    session_obj.turn.retry_excluded_tools = set(excluded)

    mock_llm_client.responses = [response]
    await _maybe_reflect(sid, db.get_session(sid), session_obj=session_obj)
    return session_obj


async def test_exclusion_does_not_survive_a_verdict_that_names_nothing(mock_llm_client, monkeypatch):
    session_obj = await _run_reflect(
        mock_llm_client,
        monkeypatch,
        _verdict(),
        excluded={"spawn_worker"},
    )
    assert session_obj.turn.reflect_retry_requested
    assert session_obj.turn.retry_excluded_tools == set(), "a prior retry's exclusion leaked into this one"


async def test_a_named_tool_is_still_excluded(mock_llm_client, monkeypatch):
    """The clear must not disarm the effector itself."""

    class _Registry:
        def exists(self, name):
            return name == "bash"

    monkeypatch.setattr("core.tools.registry.get_registry", lambda: _Registry())
    session_obj = await _run_reflect(
        mock_llm_client,
        monkeypatch,
        _verdict(retry_without_tools=["bash"]),
        excluded={"spawn_worker"},
    )
    assert session_obj.turn.retry_excluded_tools == {"bash"}
