"""Cross-retry circuit breaker + reflect lesson effector (audit P1f).

Field case: session 2072ab68cfd4 burned ten consecutive reflect retries, each
failing identically (spawned workers against the scout's explicit instruction,
deliverable never written). The breaker stops retrying when the last two
attempts share a failure signature; the effector lets reflect mechanically
disable the misused tools for the retry attempt.
"""

import json

from db import models as db
from sessions.hooks import _same_failure_repeating

# ---------------------------------------------------------------------------
# _same_failure_repeating
# ---------------------------------------------------------------------------


def _pm(sid, attempt, verdict, cause, reasoning, diagnostic=""):
    payload = json.dumps({"verdict": verdict, "reasoning": reasoning, "diagnostic": diagnostic})
    return db.add_post_mortem(sid, attempt, verdict, cause, 0.95, "m", 1, None, None, payload)


def test_breaker_trips_on_identical_consecutive_failures():
    sid = db.create_session(title="breaker")
    text = "The agent ignored the scout's explicit instruction and spawned workers which stalled."
    _pm(sid, 1, "retry", "agent", text, "premature delegation to workers")
    _pm(sid, 2, "retry", "agent", text, "premature delegation to workers")
    sig = _same_failure_repeating(sid)
    assert sig is not None
    assert "cause=agent" in sig


def test_breaker_stays_quiet_on_different_failures():
    sid = db.create_session(title="breaker-diff")
    _pm(sid, 1, "retry", "agent", "spawned workers against instructions and stalled out")
    _pm(sid, 2, "retry", "agent", "wrote the file to the wrong directory and never verified it")
    assert _same_failure_repeating(sid) is None


def test_breaker_requires_two_retry_verdicts():
    sid = db.create_session(title="breaker-one")
    _pm(sid, 1, "retry", "agent", "spawned workers against instructions")
    assert _same_failure_repeating(sid) is None

    sid2 = db.create_session(title="breaker-pass")
    _pm(sid2, 1, "pass", "none", "all good")
    _pm(sid2, 2, "retry", "agent", "spawned workers against instructions")
    assert _same_failure_repeating(sid2) is None


def test_breaker_requires_same_cause():
    sid = db.create_session(title="breaker-cause")
    text = "the exact same reasoning text both times, word for word"
    _pm(sid, 1, "retry", "agent", text)
    _pm(sid, 2, "retry", "env", text)
    assert _same_failure_repeating(sid) is None


# ---------------------------------------------------------------------------
# retry_without_tools parsing
# ---------------------------------------------------------------------------


def test_retry_without_tools_parsed_on_retry():
    from core.reflect import _result_from_data

    r = _result_from_data(
        {
            "verdict": "retry",
            "reasoning": "delegated instead of working inline",
            "retry_without_tools": ["spawn_worker", "await_workers"],
        },
        "m",
        1,
    )
    assert r.retry_without_tools == ["spawn_worker", "await_workers"]


def test_retry_without_tools_ignored_on_pass_and_capped():
    from core.reflect import _result_from_data

    r = _result_from_data(
        {"verdict": "pass", "reasoning": "fine", "retry_without_tools": ["spawn_worker"]},
        "m",
        1,
    )
    assert r.retry_without_tools == []

    r2 = _result_from_data(
        {
            "verdict": "retry",
            "reasoning": "x",
            "retry_without_tools": [f"tool_{i}" for i in range(9)] + [42, ""],
        },
        "m",
        1,
    )
    assert len(r2.retry_without_tools) == 5
    assert all(isinstance(t, str) for t in r2.retry_without_tools)


# ---------------------------------------------------------------------------
# Schema filter — excluded tool is removed from the agent's tool surface
# ---------------------------------------------------------------------------


async def test_excluded_tool_removed_from_schema(monkeypatch):
    from core.agent import run_agent
    from core.llm.types import StreamEvent, StreamEventType
    from core.scout.report import ScoutReport
    from sessions.state import AgentSession
    from tests.conftest import FakeLLMClient

    captured_tools: list = []

    class ToolCapturingClient(FakeLLMClient):
        async def chat_stream(self, messages, tools=None, model="", **kwargs):
            captured_tools.append([t["function"]["name"] for t in (tools or [])])
            self.call_count += 1
            yield StreamEvent(type=StreamEventType.TOKEN, content="done without the tool")
            yield StreamEvent(type=StreamEventType.DONE)

    fake = ToolCapturingClient()
    monkeypatch.setattr("core.agent.get_llm_client", lambda: fake)

    from core.tools.registry import ToolRegistry

    reg = ToolRegistry()
    for name in ("noop_tool", "other_tool"):
        reg.register(
            name=name,
            func=lambda: "ok",
            description="no-op",
            parameters={"type": "object", "properties": {}},
            parallel_safe=True,
            timeout=5,
        )
    monkeypatch.setattr("core.agent.get_registry", lambda: reg)

    sid = db.create_session(title="Excluded Tool Test")
    session = AgentSession(session_id=sid)
    session.last_scout_report = ScoutReport(recommended_tools=["noop_tool", "other_tool"])
    session.retry_excluded_tools = {"noop_tool"}

    await run_agent(sid, "go", session)

    assert captured_tools, "agent must have made at least one LLM call"
    for tool_list in captured_tools:
        assert "noop_tool" not in tool_list, f"excluded tool leaked into schema: {tool_list}"
        assert "other_tool" in tool_list


def test_breaker_ignores_previous_turn_post_mortems():
    """Gate-fallback retries bump reflect_count without writing post-mortems,
    so the second row can be a PREVIOUS turn's — the turn anchor must keep
    the breaker from comparing across turns (polish review)."""
    sid = db.create_session(title="breaker-turn-scope")
    text = "spawned workers against explicit scout instruction and stalled"
    _pm(sid, 2, "retry", "agent", text)  # previous turn's last attempt
    _pm(sid, 3, "retry", "agent", text)  # current turn's first real reflect

    # Without an anchor both rows match (legacy behavior)…
    assert _same_failure_repeating(sid) is not None
    # …but an anchor after the first row's creation excludes it.
    from db import models as dbm

    rows = dbm.list_post_mortems(session_id=sid, limit=2)
    older_created = rows[1]["created_at"]
    anchor_after_older = older_created + "z"  # lexically after the older row
    assert _same_failure_repeating(sid, anchor_after_older) is None
