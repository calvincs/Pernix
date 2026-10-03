"""Regression tests for the 2026-09-04 trust-loop hardening, W1.

Four defects in the attribution path, all of which let the learning loop
record activity without ever recording a verdict:

1. `attribute()` credited adaptive entries for wins only.
2. Hint uses were bumped at scout submit time even for canary sessions and
   fallback plans.
   (Points 1-2 went with the adaptive layer in 3.2, and their tests with it.)
3. `ask_user` in an unattended session returned an "Error:" string for a
   by-design non-answer, so the executor set was_error, tool_summary booked a
   failure and Candor (since retired) emitted tool_ok(ask_user)=false.
4. Off that ledger the Candor producer minted a live routing hint telling
   every scout to "prefer an alternative" to asking the user (8 uses, 7
   failures on the box). Candor and its producer were retired in 2026-10;
   the tests for points 3-4 that remain pin the executor and synthesis side.
"""

import json
from types import SimpleNamespace

from core import synthesis
from core.agent import record_tool_outcome
from core.tools.executor import (
    UNAVAILABLE_PREFIX,
    ToolExecutionResult,
    _execute_single,
    is_unavailable,
)
from core.tools.registry import ToolRegistry


def _pm(verdict, failure_cause, *, tool_summary=None):
    payload = {"scout_summary": {"from_fallback": False}}
    if tool_summary is not None:
        payload["tool_summary"] = tool_summary
    return {
        "id": "pm-w1",
        "verdict": verdict,
        "failure_cause": failure_cause,
        "confidence": 0.9,
        "execution_mode": "inline",
        "scout_viability": "verified",
        "payload_json": json.dumps(payload),
    }


# ---------------------------------------------------------------------------
# 3. By-design unavailability is not a failure
# ---------------------------------------------------------------------------


def test_ask_user_unattended_returns_unavailable_not_an_error(monkeypatch):
    from core.tools.builtin import dialog_tools

    monkeypatch.setattr("core.tools.executor._is_unattended_session", lambda sid: True)
    out = dialog_tools.ask_user(question="ship it?", _context={"session_id": "cron-1"})
    assert out.startswith(UNAVAILABLE_PREFIX)
    assert not out.startswith("Error:")
    # The agent still gets told what to do instead.
    assert "proceed without user input" in out


async def test_executor_does_not_count_unavailable_as_an_error():
    reg = ToolRegistry()
    reg.register(
        name="ask_user",
        func=lambda: f"{UNAVAILABLE_PREFIX} no user is present; proceed without user input.",
        description="ask",
        parameters={"type": "object", "properties": {}},
    )
    res = await _execute_single("ask_user", {}, {"session_id": "cron-1"}, reg)

    assert res.was_error is False
    assert is_unavailable(res)
    assert res.metadata.get("unavailable") is True
    # A call that never ran is no evidence about the tool either way.
    assert reg.metrics["ask_user"].failure_count == 0
    assert reg.metrics["ask_user"].success_count == 0


def test_record_tool_outcome_counts_unavailable_apart_from_failures():
    turn = SimpleNamespace(tool_summary={}, tool_summary_attempts=[], reflect_count=0)
    unavailable = ToolExecutionResult(
        "ask_user", f"{UNAVAILABLE_PREFIX} no user present", False, 0, metadata={"unavailable": True}
    )
    broken = ToolExecutionResult("ask_user", "Error: boom", True, 0)
    for r in (unavailable, unavailable, broken):
        record_tool_outcome(turn, r)

    stats = turn.tool_summary["ask_user"]
    assert (stats["calls"], stats["failures"], stats["unavailable"]) == (3, 1, 2)
    assert stats["errors"] == ["Error: boom"]  # the non-answers never enter the previews
    assert turn.tool_summary_attempts[0]["ask_user"]["unavailable"] == 2


def test_unavailable_calls_are_not_a_tool_signal_failure():
    row = _pm("pass", "none", tool_summary={"ask_user": {"calls": 2, "failures": 0, "unavailable": 2}})
    assert [a for a in synthesis.attribute(row) if a.signal_type == "tool"] == []


def test_a_real_failure_beside_an_unavailable_call_still_attributes():
    row = _pm("pass", "none", tool_summary={"ask_user": {"calls": 3, "failures": 2, "unavailable": 1}})
    attrs = [a for a in synthesis.attribute(row) if a.signal_type == "tool"]
    assert len(attrs) == 1 and attrs[0].delta_failures == 1
    assert "2/2 calls failed" in attrs[0].rationale
