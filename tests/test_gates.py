"""Pernix — Deterministic gates (adaptation plan 3a).

A gate's exit code is host-observable evidence: the reflect clamp makes
`pass` unreachable while one fails, the unchanged-watch_paths guard reuses
stale failures only after the first retry, and when reflect is skipped a
failing gate requests the retry directly.
"""

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import gates as gates_mod
from core.gates import GateResult, format_evidence, format_retry_guidance, run_gates_for_turn
from db import models as db


@pytest.fixture(autouse=True)
def _gates_on(monkeypatch):
    monkeypatch.setattr("config.settings.gates_enabled", True)
    from core.tools.paths import workspace

    workspace().mkdir(parents=True, exist_ok=True)


def _session_obj(turn=1, reflect_count=0):
    return SimpleNamespace(current_turn_user_msg_id=turn, reflect_count=reflect_count, reflect_lessons="")


# ---------------------------------------------------------------------------
# DB accessors
# ---------------------------------------------------------------------------


def test_gate_crud_upsert():
    sid = db.create_session(title="g")
    db.add_gate(sid, "tests", "true", watch_paths=["src"], cwd=None)
    db.add_gate(sid, "tests", "false")  # upsert replaces command
    rows = db.get_gates(sid)
    assert len(rows) == 1
    assert rows[0]["command"] == "false"
    assert rows[0]["watch_paths"] == []
    assert db.remove_gate(sid, "tests")
    assert db.get_gates(sid) == []
    assert not db.remove_gate(sid, "tests")


def test_add_gate_tool_exposes_scope():
    """The add_gate tool must be able to reach scope='goal' — otherwise the
    documented goal-scoped gate is unreachable from the toolset."""
    from core.extensions.evaluation import add_gate as add_gate_tool

    sid = db.create_session(title="scoped")
    ctx = {"session_id": sid}

    out = add_gate_tool("plain", "true", _context=ctx)
    assert "scope=session" in out
    out = add_gate_tool("goalie", "true", scope="goal", _context=ctx)
    assert "scope=goal" in out

    scopes = {r["name"]: r["scope"] for r in db.get_gates(sid)}
    assert scopes == {"plain": "session", "goalie": "goal"}

    bad = add_gate_tool("nope", "true", scope="bogus", _context=ctx)
    assert bad.startswith("Error:") and "scope" in bad
    assert "nope" not in {r["name"] for r in db.get_gates(sid)}


def test_add_gate_tool_scope_registered_in_schema():
    from core.extensions.evaluation import register as eval_register

    captured = {}

    class _Reg:
        def register(self, name="", parameters=None, **_kw):
            captured[name] = parameters or {}

    eval_register(_Reg())
    props = captured["add_gate"]["properties"]
    assert props["scope"]["enum"] == ["session", "goal"]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def test_run_gates_pass_and_fail():
    sid = db.create_session(title="g")
    db.add_gate(sid, "ok", "true")
    db.add_gate(sid, "bad", "echo broken-thing; exit 3")
    results = run_gates_for_turn(sid, _session_obj(), attempt=1)
    by_name = {r.name: r for r in results}
    assert by_name["ok"].passed and by_name["ok"].exit_code == 0
    assert not by_name["bad"].passed and by_name["bad"].exit_code == 3
    assert "broken-thing" in by_name["bad"].output_tail


def test_watch_paths_reuse_guard(tmp_path):
    from core.tools.paths import workspace

    watched = workspace() / "watched.txt"
    watched.write_text("v1")
    sid = db.create_session(title="g")
    db.add_gate(sid, "check", "exit 1", watch_paths=["watched.txt"])
    obj = _session_obj()

    r1 = run_gates_for_turn(sid, obj, attempt=1)[0]
    assert not r1.passed and not r1.reused

    # First retry (attempt 2) always re-runs regardless of fingerprint.
    r2 = run_gates_for_turn(sid, obj, attempt=2)[0]
    assert not r2.reused

    # Later retries with unchanged watch paths reuse the failure.
    r3 = run_gates_for_turn(sid, obj, attempt=3)[0]
    assert r3.reused and not r3.passed

    # Changing a watched file forces a real re-run.
    time.sleep(0.01)
    watched.write_text("v2-changed")
    r4 = run_gates_for_turn(sid, obj, attempt=3)[0]
    assert not r4.reused


def test_new_turn_resets_gate_history():
    sid = db.create_session(title="g")
    from core.tools.paths import workspace

    (workspace() / "w.txt").write_text("x")
    db.add_gate(sid, "check", "exit 1", watch_paths=["w.txt"])
    obj = _session_obj(turn=1)
    run_gates_for_turn(sid, obj, attempt=1)
    # New turn id -> history cleared -> attempt 3 still runs fresh.
    obj.current_turn_user_msg_id = 2
    r = run_gates_for_turn(sid, obj, attempt=3)[0]
    assert not r.reused


def test_disabled_flag_short_circuits(monkeypatch):
    monkeypatch.setattr("config.settings.gates_enabled", False)
    sid = db.create_session(title="g")
    db.add_gate(sid, "x", "true")
    assert run_gates_for_turn(sid, _session_obj(), attempt=1) == []


# ---------------------------------------------------------------------------
# Reflect clamp
# ---------------------------------------------------------------------------


class _StubClient:
    def __init__(self, verdict="pass"):
        self._verdict = verdict

    async def chat(self, messages=None, model="", max_tokens=0, **kwargs):
        return SimpleNamespace(
            content=json.dumps(
                {
                    "verdict": self._verdict,
                    "reasoning": "looks complete",
                    "failure_cause": "",
                    "confidence": 0.9,
                    "deliverables": [],
                }
            )
        )


async def test_reflect_clamp_forces_retry_on_failing_gate(monkeypatch):
    sid = db.create_session(title="clamp")
    db.add_message(sid, "user", "please build the thing and make the tests pass")
    db.add_message(sid, "assistant", "Done! Everything works.")
    monkeypatch.setattr("core.llm.client.get_llm_client", lambda: _StubClient("pass"))

    from core.reflect import reflect_on_session

    failing_gate = GateResult(name="tests", command="pytest -q", passed=False, exit_code=1, output_tail="2 failed")
    result = await reflect_on_session(sid, gate_results=[failing_gate])
    assert result.verdict == "retry"
    assert "[gate clamp]" in result.reasoning
    assert "tests" in (result.missing or "")

    # Post-mortem records the CLAMPED verdict plus the H2/gate fields.
    pms = db.list_post_mortems(session_id=sid)
    assert pms, "post-mortem row missing"
    row_text = json.dumps(pms[0], default=str)
    assert "retry" in row_text  # clamped verdict, not the LLM's pass
    assert "agent_model" in row_text or "task_category" in row_text or "gates" in row_text


async def test_reflect_passing_gates_do_not_clamp(monkeypatch):
    sid = db.create_session(title="noclamp")
    db.add_message(sid, "user", "do the thing")
    db.add_message(sid, "assistant", "done")
    monkeypatch.setattr("core.llm.client.get_llm_client", lambda: _StubClient("pass"))

    from core.reflect import reflect_on_session

    ok = GateResult(name="tests", command="true", passed=True, exit_code=0)
    result = await reflect_on_session(sid, gate_results=[ok])
    assert result.verdict == "pass"


# ---------------------------------------------------------------------------
# Skipped-reflect fallback + formatting
# ---------------------------------------------------------------------------


def test_gate_retry_fallback_sets_retry_and_lessons(monkeypatch):
    from sessions.hooks import _apply_gate_retry_fallback

    monkeypatch.setattr("config.settings.reflect_max_retries", 2)
    obj = SimpleNamespace(reflect_count=0, reflect_lessons="", reflect_retry_requested=False)
    bad = GateResult(name="build", command="make", passed=False, exit_code=2, output_tail="link error")
    _apply_gate_retry_fallback("sid", {"session_type": "normal"}, obj, [bad])
    assert obj.reflect_retry_requested
    assert obj.reflect_count == 1
    assert "build" in obj.reflect_lessons and "link error" in obj.reflect_lessons

    # At the cap: no further retry requested.
    obj2 = SimpleNamespace(reflect_count=2, reflect_lessons="", reflect_retry_requested=False)
    _apply_gate_retry_fallback("sid", {"session_type": "normal"}, obj2, [bad])
    assert not obj2.reflect_retry_requested

    # All passing: untouched.
    obj3 = SimpleNamespace(reflect_count=0, reflect_lessons="", reflect_retry_requested=False)
    _apply_gate_retry_fallback("sid", {"session_type": "normal"}, obj3, [GateResult("t", "true", True, 0)])
    assert not obj3.reflect_retry_requested


def test_evidence_and_guidance_formatting():
    results = [
        GateResult(name="ok", command="true", passed=True, exit_code=0),
        GateResult(name="bad", command="pytest", passed=False, exit_code=1, output_tail="3 failed", reused=True),
    ]
    ev = format_evidence(results)
    assert "GATE EVIDENCE" in ev and "bad: FAIL" in ev and "reused prior failure" in ev
    guidance = format_retry_guidance(results)
    assert "bad" in guidance and "3 failed" in guidance and "ok" not in guidance.split("\n")[0]
    assert format_retry_guidance([results[0]]) == ""
