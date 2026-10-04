"""Harness review 2026-09-08 (F10): once the stream ladder failed over, the
turn kept compiling context for the PRIMARY model while every remaining
request was dispatched to the fallback.

`_resolve_effective_model` read only `session.model_override or
settings.llm_model`, so the sticky-fallback rounds took their context budget,
output cap, capabilities and the compiler's `model_name` from a model they
were no longer calling; the substitution happened at dispatch. With the
documented topology — a large cloud primary in front of a local Ollama
fallback — that hands a small model a prompt sized for a large one. Overflow
recovery repeated the mistake: it re-compiled for the primary and handed the
compactor a primary-sized `history_budget`, so the keep-window it clamped to
could exceed the fallback's entire window and the turn spent its three
attempts without ever shrinking to something the fallback would accept.

The fix resolves the model for the request that is about to be made — tool
loop, final answer and overflow recovery alike. `session.model_override` is
untouched: a failover is a per-turn detour, not the switch a user asked for.
"""

from __future__ import annotations

import pytest

from core.llm.errors import FailoverError, FailoverReason
from core.llm.types import StreamEvent, StreamEventType, ToolCall
from core.scout.report import ScoutReport
from sessions.state import AgentSession

PRIMARY = "vendor/primary-model"
FALLBACK = "local-fallback"
# The point of the finding: the fallback is the SMALLER window.
BUDGETS = {PRIMARY: 180_000, FALLBACK: 32_000}


@pytest.fixture
def turn(monkeypatch):
    """Run one agent turn against a scripted provider, recording every
    context compile, budget derivation and compaction it performed.

    `script` is one entry per chat_stream call: "rate_limit" (drives the
    ladder to the fallback), "tool" (a tool call, so the turn continues),
    "overflow" (the provider rejects the prompt as too long) or "text".
    """
    import core.agent as agent_mod
    from core.tools.registry import ToolRegistry
    from db import models as db
    from tests.conftest import FakeLLMClient

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr("config.settings.llm_model", PRIMARY)
    monkeypatch.setattr("config.settings.fallback_model", FALLBACK)
    monkeypatch.setattr("config.settings.max_tool_rounds", 10)
    monkeypatch.setattr("config.settings.context_auto", True)

    record: dict = {"streams": [], "budgets": [], "compiles": [], "compactions": []}

    def fake_budget(model: str) -> int:
        record["budgets"].append(model)
        return BUDGETS[model]

    monkeypatch.setattr("core.agent.derive_model_budget", fake_budget)

    async def no_refresh(model: str) -> bool:
        return False

    monkeypatch.setattr("core.agent.ensure_model_known", no_refresh)

    real_compile = agent_mod.compile_context

    def spy_compile(**kw):
        payload = real_compile(**kw)
        record["compiles"].append(
            {
                "model_name": kw["model_name"],
                "context_budget": kw["context_budget"],
                "history_budget": payload.history_budget,
            }
        )
        return payload

    monkeypatch.setattr("core.agent.compile_context", spy_compile)

    async def fake_compact(session_id, messages, history_budget=None, turn_user_msg_id=None, **kw):
        record["compactions"].append(history_budget)
        return True

    monkeypatch.setattr("core.agent.compact_with_llm", fake_compact)

    reg = ToolRegistry()
    reg.register(
        name="noop_tool",
        func=lambda: "ok",
        description="no-op",
        parameters={"type": "object", "properties": {}},
        parallel_safe=True,
        timeout=5,
    )
    monkeypatch.setattr("core.agent.get_registry", lambda: reg)

    async def _run(script: list[str], *, context_budget_override: int | None = None):
        steps = iter(script)

        class ScriptedClient(FakeLLMClient):
            async def chat_stream(self, messages, tools=None, model="", **kwargs):
                record["streams"].append(model)
                self.call_count += 1
                step = next(steps)
                if step == "rate_limit":
                    yield StreamEvent(type=StreamEventType.ERROR, error="429 rate limit exceeded")
                elif step == "tool":
                    yield StreamEvent(
                        type=StreamEventType.TOOL_CALL,
                        tool_calls=[ToolCall(id=f"tc{self.call_count}", name="noop_tool", arguments="{}")],
                    )
                    yield StreamEvent(type=StreamEventType.DONE)
                elif step == "overflow":
                    raise FailoverError(FailoverReason.CONTEXT_OVERFLOW, "context_length_exceeded: prompt too long")
                else:
                    yield StreamEvent(type=StreamEventType.TOKEN, content="all done")
                    yield StreamEvent(type=StreamEventType.DONE)

            def resolve_provider(self, model=""):
                return "openrouter" if "/" in model else "ollama"

        monkeypatch.setattr("core.agent.get_llm_client", ScriptedClient)

        sid = db.create_session(title="F10 regression")
        session = AgentSession(session_id=sid, session_type="normal")
        session.last_scout_report = ScoutReport(recommended_tools=["noop_tool"])
        session.context_budget_override = context_budget_override

        await agent_mod.run_agent(sid, "go", session)
        return session, record

    return _run


async def test_rounds_after_a_failover_compile_for_the_fallback(turn):
    """Round 0 fails over and comes back with a tool call; round 1 must be
    compiled for the fallback that will receive it, not for the primary."""
    session, record = await turn(["rate_limit", "tool", "text"])

    assert record["streams"] == [PRIMARY, FALLBACK, FALLBACK]
    # Round 0 was compiled before anything had gone wrong — the primary is
    # the right answer there. Every compile after the failover is not.
    assert record["compiles"][0]["model_name"] == PRIMARY
    later = record["compiles"][1:]
    assert later, "the turn must have run a second round"
    assert all(c["model_name"] == FALLBACK for c in later)
    assert all(c["context_budget"] == BUDGETS[FALLBACK] for c in later)
    assert FALLBACK in record["budgets"]
    # The failover is not a session-level model switch.
    assert session.model_override is None


async def test_an_overflow_on_the_fallback_compacts_to_the_fallback_budget(turn):
    """The fallback rejects the round-1 prompt as too long. The compactor has
    to be told the fallback's history budget — clamped to a primary-sized one
    it keeps a window the fallback cannot hold and the retry fails the same
    way."""
    _session, record = await turn(["rate_limit", "tool", "overflow", "text"])

    assert record["streams"] == [PRIMARY, FALLBACK, FALLBACK, FALLBACK]
    assert len(record["compactions"]) == 1
    fallback_compiles = [c for c in record["compiles"] if c["model_name"] == FALLBACK]
    assert record["compactions"][0] == fallback_compiles[0]["history_budget"]
    assert record["compactions"][0] < BUDGETS[FALLBACK]
    # And the post-overflow re-compile stayed on the fallback.
    assert record["compiles"][-1]["model_name"] == FALLBACK
    assert record["compiles"][-1]["context_budget"] == BUDGETS[FALLBACK]


async def test_an_overflow_in_the_failover_round_recompiles_before_compacting(turn):
    """When the ladder falls over inside the round, the payload in hand was
    compiled for the primary. Compacting against it would aim at the wrong
    size, so the loop re-compiles for the fallback first and only spends a
    compaction attempt if that still overflows."""
    _session, record = await turn(["rate_limit", "overflow", "text"])

    assert record["streams"] == [PRIMARY, FALLBACK, FALLBACK]
    assert record["compactions"] == []
    assert [c["model_name"] for c in record["compiles"]] == [PRIMARY, FALLBACK]
    assert record["compiles"][-1]["context_budget"] == BUDGETS[FALLBACK]


async def test_an_explicit_budget_override_still_wins_on_the_fallback(turn):
    """The per-session override is the operator's number; failing over does
    not get to replace it with the fallback's derived window."""
    _session, record = await turn(["rate_limit", "tool", "text"], context_budget_override=55_000)

    assert [c["model_name"] for c in record["compiles"]] == [PRIMARY, FALLBACK]
    assert all(c["context_budget"] == 55_000 for c in record["compiles"])
