"""Pernix — Gate-outcome logger: gate verdicts reach the standing ledgers.

Pre-registered falsifier (the TELOS consumer's): if a turn where a gate ran
does NOT produce a type:'gates' trace event carrying name/passed/attempt AND a
gate_ok / gate_failure_mode Candor observation, the logger is wrong.

Gates re-run on every reflect retry within a turn, so the unit of record is
one gate per ATTEMPT — separate trace events, so the fail -> retry -> pass arc
stays matchable in sequence by the hypothesis evaluator.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from config import settings
from core.gates import GateResult
from core.telos.store import TelosStore
from db import models as db
from sessions import hooks
from sessions.state import TurnState

TRACE_FIELDS = {"name", "passed", "attempt", "reflect_mode", "session_type", "session"}


class _FakeBridge:
    def __init__(self):
        self.recorded: list[list[dict]] = []

    async def record(self, observations):
        self.recorded.append(observations)
        return {"observed": len(observations), "buffered": 0}


@pytest.fixture
def bridge(monkeypatch):
    fake = _FakeBridge()
    monkeypatch.setattr("core.extensions.candor.bridge.get_candor_bridge", lambda: fake)
    return fake


@pytest.fixture(autouse=True)
def _ledgers_on(monkeypatch):
    monkeypatch.setattr(settings, "gates_enabled", True)
    monkeypatch.setattr(settings, "telos_enabled", True)
    monkeypatch.setattr(settings, "candor_enabled", True)
    from core.tools.paths import workspace

    workspace().mkdir(parents=True, exist_ok=True)


def _session_obj(turn=1, reflect_count=0):
    return SimpleNamespace(
        current_turn_user_msg_id=turn,
        turn=TurnState(reflect_count=reflect_count),
        model_override="test-model",
    )


def _events():
    return TelosStore.open().trace_events(days=1, types={"gates"})


def _obs(bridge, pred):
    return [o for batch in bridge.recorded for o in batch if o["pred"] == pred]


# ---------------------------------------------------------------------------
# The falsifier, end to end: a turn where gates actually ran
# ---------------------------------------------------------------------------


async def test_turn_with_gates_writes_trace_event_and_observation_per_gate(bridge):
    """The acceptance contract, exercised through the real runner: two gates
    run, two trace events land, two gate_ok observations land."""
    sid = db.create_session(title="logger")
    db.add_gate(sid, "ok", "true")
    db.add_gate(sid, "bad", "echo broken-thing; exit 3")

    results = await hooks._run_turn_gates(sid, {"session_type": "cron"}, _session_obj())
    assert len(results) == 2

    events = {e["name"]: e for e in _events()}
    assert set(events) == {"ok", "bad"}
    for name, ev in events.items():
        assert TRACE_FIELDS <= set(ev), f"{name} event missing fields: {TRACE_FIELDS - set(ev)}"
        assert ev["type"] == "gates"
        assert ev["attempt"] == 1
        assert ev["session"] == sid
        assert ev["session_type"] == "cron"
        assert ev["reflect_mode"] == "sync"
    assert events["ok"]["passed"] is True
    assert events["bad"]["passed"] is False
    # A failure names itself; a pass carries no prose at all.
    assert "broken-thing" in events["bad"]["excerpt"]
    assert "excerpt" not in events["ok"]

    ok_obs = {o["args"][0]: o for o in _obs(bridge, "gate_ok")}
    assert ok_obs["ok"]["outcome"] is True and ok_obs["bad"]["outcome"] is False
    assert ok_obs["bad"]["stmt_type"] == "frequency"
    assert ok_obs["bad"]["ctx"]["reflect_mode"] == "sync"
    modes = _obs(bridge, "gate_failure_mode")
    assert [o["args"] for o in modes] == [["bad"]]  # only the failure
    assert modes[0]["stmt_type"] == "categorical" and modes[0]["value"]


async def test_fail_then_pass_across_attempts_yields_two_events(bridge):
    """Two attempts of the same turn are two genuine observations — the arc is
    the evidence, and an attempts array would hide it."""
    session = {"session_type": "cron"}
    failed = GateResult(name="tests", command="pytest -q", passed=False, exit_code=1, output_tail="2 failed")
    passed = GateResult(name="tests", command="pytest -q", passed=True, exit_code=0)

    await hooks._log_gate_outcomes("sid", session, _session_obj(), [failed], attempt=1)
    await hooks._log_gate_outcomes("sid", session, _session_obj(reflect_count=1), [passed], attempt=2)

    events = _events()
    assert [(e["attempt"], e["passed"]) for e in events] == [(1, False), (2, True)]
    assert "2 failed" in events[0]["excerpt"]

    ok_obs = _obs(bridge, "gate_ok")
    assert [o["outcome"] for o in ok_obs] == [False, True]
    assert [o["ctx"]["retry"] for o in ok_obs] == ["no", "yes"]
    assert len(_obs(bridge, "gate_failure_mode")) == 1


async def test_reflect_mode_reflects_the_deferral_predicate(bridge, monkeypatch):
    """Since bfbaadd an interactive turn's grade is observe-only, so the gate
    is its only mechanical retry path. A verdict that doesn't say which regime
    produced it is ambiguous for the September calibration review."""
    monkeypatch.setattr(settings, "reflect_deferred_normal", True)
    result = GateResult(name="tests", command="true", passed=True, exit_code=0)

    await hooks._log_gate_outcomes("sid", {"session_type": "normal"}, _session_obj(), [result], attempt=1)
    await hooks._log_gate_outcomes("sid", {"session_type": "cron"}, _session_obj(), [result], attempt=1)

    assert [e["reflect_mode"] for e in _events()] == ["deferred", "sync"]
    assert [o["ctx"]["reflect_mode"] for o in _obs(bridge, "gate_ok")] == ["deferred", "sync"]

    # The setting is half the predicate: with deferral off, "normal" is sync.
    monkeypatch.setattr(settings, "reflect_deferred_normal", False)
    await hooks._log_gate_outcomes("sid", {"session_type": "normal"}, _session_obj(), [result], attempt=1)
    assert _events()[-1]["reflect_mode"] == "sync"


# ---------------------------------------------------------------------------
# Gating and fail-soft
# ---------------------------------------------------------------------------


async def test_disabled_surfaces_emit_nothing_and_do_not_raise(bridge, monkeypatch):
    monkeypatch.setattr(settings, "telos_enabled", False)
    monkeypatch.setattr(settings, "candor_enabled", False)
    result = GateResult(name="tests", command="true", passed=True, exit_code=0)

    await hooks._log_gate_outcomes("sid", {"session_type": "cron"}, _session_obj(), [result], attempt=1)

    assert _events() == []
    assert bridge.recorded == []


async def test_canary_sessions_are_isolated(bridge):
    """Same convention as _maybe_candor / on_post_task: synthetic turns run
    deliberately-hard gates and must not move either ledger."""
    result = GateResult(name="tests", command="false", passed=False, output_tail="nope")
    await hooks._log_gate_outcomes("c1", {"session_type": "canary"}, _session_obj(), [result], attempt=1)
    assert _events() == []
    assert bridge.recorded == []


async def test_trace_failure_does_not_break_the_turn_or_the_observation(bridge, monkeypatch):
    """The two surfaces fail independently: a broken trace must not cost the
    Candor observation, and neither may reach the caller."""

    def _boom(*_a, **_kw):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr("core.telos.anomaly.record_gate_outcomes", _boom)
    result = GateResult(name="tests", command="true", passed=True, exit_code=0)

    await hooks._log_gate_outcomes("sid", {"session_type": "cron"}, _session_obj(), [result], attempt=1)
    assert _obs(bridge, "gate_ok")


async def test_candor_failure_is_swallowed(monkeypatch):
    class _Exploding:
        async def record(self, observations):
            raise RuntimeError("boom")

    monkeypatch.setattr("core.extensions.candor.bridge.get_candor_bridge", lambda: _Exploding())
    result = GateResult(name="tests", command="false", passed=False, output_tail="nope")

    await hooks._log_gate_outcomes("sid", {"session_type": "cron"}, _session_obj(), [result], attempt=1)
    assert len(_events()) == 1  # the trace still got its event


async def test_gate_run_still_returns_results_when_logging_explodes(bridge, monkeypatch):
    """The logger hangs off the end of _run_turn_gates; nothing it does may
    change what the clamp and the retry fallback see."""

    async def _boom(*_a, **_kw):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(hooks, "_log_gate_outcomes", _boom)
    sid = db.create_session(title="logger")
    db.add_gate(sid, "bad", "exit 1")

    results = await hooks._run_turn_gates(sid, {"session_type": "cron"}, _session_obj())
    assert [r.passed for r in results] == [False]


# ---------------------------------------------------------------------------
# Excerpt shaping
# ---------------------------------------------------------------------------


async def test_excerpt_is_bounded_and_falls_back_to_runner_error(bridge):
    long_tail = "x" * 5000 + "FINAL-LINE"
    noisy = GateResult(name="noisy", command="pytest", passed=False, exit_code=1, output_tail=long_tail)
    # A timeout or a policy refusal produces no output at all — the runner's
    # own error is then the only thing that explains the failure.
    timed_out = GateResult(name="slow", command="sleep 999", passed=False, error="timed out after 120s")

    await hooks._log_gate_outcomes("sid", {"session_type": "cron"}, _session_obj(), [noisy, timed_out], attempt=1)

    events = {e["name"]: e for e in _events()}
    assert len(events["noisy"]["excerpt"]) <= hooks.GATE_EXCERPT_CHARS
    assert events["noisy"]["excerpt"].endswith("FINAL-LINE")
    assert events["slow"]["excerpt"] == "timed out after 120s"
    modes = {o["args"][0]: o["value"] for o in _obs(bridge, "gate_failure_mode")}
    assert modes["slow"] == "timeout"  # classified, never raw prose
