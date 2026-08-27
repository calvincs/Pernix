"""TELOS turn-end hook: anomaly extraction, question minting, isolation."""

from __future__ import annotations

import pytest

from config import settings
from core.telos.anomaly import extract_turn_anomalies, on_post_task
from core.telos.store import TelosStore
from sessions.state import TurnState


class _FakeSession:
    def __init__(self, turn_id="m1", tools=None, termination="complete", reflect_count=0):
        self.current_turn_user_msg_id = turn_id
        self.termination_reason = termination
        self.turn = TurnState(tool_summary=tools or {}, reflect_count=reflect_count)


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


def test_anomaly_questions_cite_standing_ledgers_not_turn_snapshots():
    """The question must name continuously recorded observables (tool_ok,
    tool_failure_mode), never 'this turn' — a turn snapshot is gone by
    evaluation time, making every spawned hypothesis un-evaluable (14/18
    abandoned questions in the 2026-08-16 audit were that class)."""
    out = extract_turn_anomalies(
        {"http_get": {"calls": 3, "failures": 2, "errors": ["403"]}}, "complete", False, "normal"
    )
    assert len(out) == 1
    text = out[0]["text"]
    assert "tool_ok('http_get')" in text
    assert "tool_failure_mode('http_get')" in text
    assert "this turn" not in text


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


async def test_remint_cooldown_suppresses_same_source(store):
    """Different failure counts defeat text dedup, but the derived_from marker
    (tool:X) is stable — within the cooldown the same source mints once, even
    after the first question was abandoned."""
    await on_post_task(
        "s1", {"session_type": "normal"}, _FakeSession(turn_id="m1", tools={"x": {"calls": 1, "failures": 1}})
    )
    qs = store.list_questions()
    n = len(qs)
    assert n >= 1
    for q in qs:  # abandonment must not reopen the mint window
        store.update(q, state="abandoned")
    await on_post_task(
        "s2", {"session_type": "normal"}, _FakeSession(turn_id="m2", tools={"x": {"calls": 9, "failures": 7}})
    )
    assert len(store.list_questions()) == n


def test_candor_tracked_tools_never_mint_questions():
    """v3.1 yield fix: a tool Candor has ANY calibrated record for is already
    tracked by the system that closes reliability loops — TELOS questions
    are for anomalies the rest of the system cannot explain. This is the fix
    for the 16-of-18-abandoned 'why did tool X fail' class on the live box."""
    from core.telos.anomaly import extract_turn_anomalies

    tracked = extract_turn_anomalies(
        {"x": {"calls": 3, "failures": 2}}, None, False, "normal", priors={"x": 0.9}
    )
    assert not any("tool 'x'" in a["text"] for a in tracked)
    novel = extract_turn_anomalies({"x": {"calls": 3, "failures": 2}}, None, False, "normal", priors={})
    assert any("tool 'x'" in a["text"] for a in novel)


async def test_remint_cooldown_zero_still_blocks_while_open(store, monkeypatch):
    """Cooldown 0 disables the TIME suppression only (v3.1): one OPEN line of
    inquiry per source, full stop — an expired cooldown must not re-mint a
    question that is still sitting in the queue. Abandoning it (with the
    cooldown off) reopens the window."""
    monkeypatch.setattr(settings, "telos_anomaly_remint_cooldown_days", 0)
    from core.telos.anomaly import _recently_minted

    await on_post_task(
        "s1", {"session_type": "normal"}, _FakeSession(turn_id="m1", tools={"x": {"calls": 1, "failures": 1}})
    )
    assert _recently_minted(store.list_questions(), ["tool:x"]) is True  # open blocks at any age
    for q in store.list_questions():
        store.update(q, state="abandoned")
    assert _recently_minted(store.list_questions(), ["tool:x"]) is False


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
    # The goal tools left with the v3.1 goal-DAG carve.
    assert names == {"telos_status", "telos_ask"}


def test_tool_flow_status_and_ask(monkeypatch, store):
    from core.extensions.telos import telos_ask, telos_status

    out = telos_ask(question="What load class makes p99 deviate from the claim?")
    assert "Minted q_" in out
    status = telos_status()
    assert "1 open" in status and "Root question" in status
    # A near-duplicate is refused.
    out = telos_ask(question="What load class makes p99 deviate from the claim??")
    assert "near-duplicate" in out
