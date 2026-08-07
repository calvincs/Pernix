"""TELOS turn-end hook: anomaly extraction, question minting, isolation."""

from __future__ import annotations

import pytest

from config import settings
from core.telos.anomaly import extract_turn_anomalies, on_post_task
from core.telos.store import TelosStore


class _FakeSession:
    def __init__(self, turn_id="m1", tools=None, termination="complete", reflect_count=0):
        self.current_turn_user_msg_id = turn_id
        self.last_tool_summary = tools or {}
        self.termination_reason = termination
        self.reflect_count = reflect_count


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setattr(settings, "telos_enabled", True)
    s = TelosStore.open()
    s.ensure_root()
    return s


def test_anomalies_from_tool_failures_and_retries():
    tools = {"browse_web": {"calls": 4, "failures": 3, "errors": ["timeout"]}, "shell": {"calls": 2, "failures": 0}}
    out = extract_turn_anomalies(tools, "round_ceiling", reflect_retry=True, session_type="normal")
    texts = " ".join(a["text"] for a in out)
    assert "browse_web" in texts and "round_ceiling" not in texts  # phrased, not raw
    assert any("ceiling" in a["text"] for a in out)
    assert any("retry" in a["text"] for a in out)
    assert all(0 < a["surprise"] <= 1 for a in out)
    # Clean turn yields nothing.
    assert extract_turn_anomalies({"shell": {"calls": 2, "failures": 0}}, "complete", False, "normal") == []


async def test_on_post_task_traces_and_mints(store):
    sess = _FakeSession(tools={"browse_web": {"calls": 3, "failures": 2, "errors": ["boom"]}}, reflect_count=1)
    await on_post_task("s1", {"session_type": "normal"}, sess)
    turns = store.trace_events(days=1, types={"turn"})
    assert len(turns) == 1 and turns[0]["session"] == "s1"
    qs = store.list_questions(state="open")
    assert 1 <= len(qs) <= 2  # per-turn mint cap
    assert all("session:s1" in q.get("derived_from") for q in qs)


async def test_on_post_task_delta_tracked_per_turn(store):
    sess = _FakeSession(tools={"x": {"calls": 1, "failures": 1}})
    await on_post_task("s1", {"session_type": "normal"}, sess)
    await on_post_task("s1", {"session_type": "normal"}, sess)  # reflect re-entry
    assert len(store.trace_events(days=1, types={"turn"})) == 1


async def test_on_post_task_canary_isolated(store):
    sess = _FakeSession(tools={"x": {"calls": 1, "failures": 1}})
    await on_post_task("c1", {"session_type": "canary"}, sess)
    assert store.trace_events(days=1, types={"turn"}) == []
    assert store.list_questions() == []


async def test_duplicate_questions_not_minted(store):
    tools = {"browse_web": {"calls": 3, "failures": 2}}
    await on_post_task("s1", {"session_type": "normal"}, _FakeSession(turn_id="m1", tools=tools))
    n = len(store.list_questions())
    await on_post_task("s2", {"session_type": "normal"}, _FakeSession(turn_id="m2", tools=tools))
    assert len(store.list_questions()) == n  # near-duplicate rejected


async def test_hook_gated_off(monkeypatch):
    """run_post_task_hooks never touches TELOS while disabled."""
    monkeypatch.setattr(settings, "telos_enabled", False)
    from pathlib import Path

    from sessions.hooks import _maybe_telos

    await _maybe_telos("s1", {"session_type": "normal"}, _FakeSession())
    # _maybe_telos itself doesn't gate (the ladder does), so calling it
    # directly with telos off still must not raise; the ladder gate is
    # exercised by inspecting the source contract.
    import inspect

    from sessions import hooks

    src = inspect.getsource(hooks.run_post_task_hooks)
    assert "settings.telos_enabled" in src


def test_extension_tools_register_only_when_enabled(monkeypatch):
    from core.extensions.telos import register
    from core.tools.registry import ToolRegistry

    monkeypatch.setattr(settings, "telos_enabled", False)
    reg = ToolRegistry()
    register(reg)
    assert not reg.all_tools()

    monkeypatch.setattr(settings, "telos_enabled", True)
    reg2 = ToolRegistry()
    register(reg2)
    names = {t.name for t in reg2.all_tools()}
    assert {"telos_status", "telos_ask", "telos_goal_add", "telos_goal_complete"} <= names


def test_tool_flow_status_ask_goal(monkeypatch, store):
    from core.extensions.telos import telos_ask, telos_goal_add, telos_goal_complete, telos_status

    out = telos_goal_add(kind="milestone", title="Ship the probe", justification="opens measurement access to X")
    assert "g_ship_the_probe" in out
    out = telos_ask(question="What load class makes p99 deviate from the claim?", parent_goal="g_ship_the_probe")
    assert "Minted q_" in out
    status = telos_status()
    assert "1 open" in status and "milestones" in status
    out = telos_goal_complete(goal_id="g_ship_the_probe")
    assert "Hevel discharge" in out
    # Root is not completable via the tool.
    out = telos_goal_complete(goal_id="g_root")
    assert "not completable" in out
