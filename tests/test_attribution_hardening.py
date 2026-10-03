"""Regression tests for the 2026-09-04 trust-loop hardening, W1.

Four defects in the attribution path, all of which let the learning loop
record activity without ever recording a verdict:

1. `attribute()` credited adaptive entries for wins only, so every policy's
   failure counter was a structural zero and the failure-dominated retirement
   in core/adaptive/retire.py could never fire for policy/prompt_note.
2. Hint uses were bumped at scout submit time even for canary sessions and
   fallback plans, whose post-mortems `attribute()` drops — uses accrued
   against outcomes that could never arrive.
3. `ask_user` in an unattended session returned an "Error:" string for a
   by-design non-answer, so the executor set was_error, tool_summary booked a
   failure and candor emitted tool_ok(ask_user)=false.
4. Off that ledger the candor producer minted a live routing hint telling
   every scout to "prefer an alternative" to asking the user (8 uses, 7
   failures on the box).
"""

import json
from types import SimpleNamespace

from core import synthesis
from core.agent import record_tool_outcome
from core.extensions.candor.emit import build_turn_observations
from core.scout.runner import _count_hint_usage
from core.tools.executor import (
    UNAVAILABLE_PREFIX,
    ToolExecutionResult,
    _execute_single,
    is_unavailable,
)
from core.tools.registry import ToolRegistry


def _pm(verdict, failure_cause, *, used_hints=None, cited_policies=None, tool_summary=None):
    payload = {"scout_summary": {"from_fallback": False}}
    if used_hints is not None:
        payload["scout_summary"]["used_hints"] = used_hints
    if cited_policies is not None:
        payload["cited_policies"] = cited_policies
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


def _entries(row):
    return [a for a in synthesis.attribute(row) if a.signal_type == "adaptive_entry"]


# ---------------------------------------------------------------------------
# 1. Cited policies and used hints can accrue failures
# ---------------------------------------------------------------------------


def test_policy_failure_fires_on_retry_blamed_on_the_agent():
    attrs = _entries(_pm("retry", "agent", cited_policies=["p1"]))
    assert len(attrs) == 1
    assert attrs[0].subject == "p1"
    assert attrs[0].delta_failures == 1 and attrs[0].delta_successes == 0
    # The use still books, so the retirement denominator is honest.
    assert attrs[0].delta_reinforcements == 1
    assert "cause=agent" in attrs[0].rationale


def test_policy_failure_fires_on_escalate_blamed_on_the_scout():
    attrs = _entries(_pm("escalate", "scout", cited_policies=["p1"]))
    assert len(attrs) == 1
    assert attrs[0].delta_failures == 1
    assert "verdict=escalate" in attrs[0].rationale and "cause=scout" in attrs[0].rationale


def test_policy_failure_does_not_fire_on_retry_blamed_on_the_environment():
    """env/task/skill are not the policy's doing: use booked, no verdict."""
    for cause in ("env", "task", "skill"):
        attrs = _entries(_pm("retry", cause, cited_policies=["p1"]))
        assert len(attrs) == 1, cause
        assert attrs[0].delta_failures == 0 and attrs[0].delta_successes == 0, cause
        assert attrs[0].delta_reinforcements == 1, cause
        assert "not charged" in attrs[0].rationale, cause


def test_policy_success_branch_still_credits_a_pass():
    attrs = _entries(_pm("pass", "none", cited_policies=["p1"]))
    assert len(attrs) == 1
    assert attrs[0].delta_successes == 1 and attrs[0].delta_failures == 0


def test_hint_failure_fires_on_escalate_blamed_on_the_agent():
    attrs = _entries(_pm("escalate", "agent", used_hints=["h1"]))
    assert len(attrs) == 1
    assert attrs[0].subject == "h1"
    assert attrs[0].delta_failures == 1
    # Hint usage was already counted at scout submit time — no double count.
    assert attrs[0].delta_reinforcements == 0
    assert "cause=agent" in attrs[0].rationale


def test_hint_failure_keeps_the_original_retry_scout_rule_as_a_subset():
    attrs = _entries(_pm("retry", "scout", used_hints=["h1"]))
    assert len(attrs) == 1 and attrs[0].delta_failures == 1


def test_hint_is_not_charged_for_an_environment_failure():
    assert _entries(_pm("retry", "env", used_hints=["h1"])) == []


# ---------------------------------------------------------------------------
# 2. No use bump where the outcome can never arrive
# ---------------------------------------------------------------------------


def _hint_bump_probe(monkeypatch):
    bumped = []
    monkeypatch.setattr("config.settings.adaptive_enabled", True)
    monkeypatch.setattr("db.models.adaptive_list_entries", lambda **kw: [{"id": "h1"}])
    monkeypatch.setattr("db.models.upsert_signal", lambda *a, **kw: bumped.append(a))
    return bumped


def test_no_hint_use_bump_in_canary_sessions(monkeypatch):
    bumped = _hint_bump_probe(monkeypatch)
    report = SimpleNamespace(used_hints=["[h1]"], from_fallback=False)

    _count_hint_usage(report, "canary")
    assert bumped == []
    # Sanitisation still ran, so the post-mortem carries real ids.
    assert report.used_hints == ["h1"]


def test_no_hint_use_bump_for_a_fallback_plan(monkeypatch):
    bumped = _hint_bump_probe(monkeypatch)
    _count_hint_usage(SimpleNamespace(used_hints=["h1"], from_fallback=True), "normal")
    assert bumped == []


def test_hint_use_bump_still_fires_for_an_ordinary_session(monkeypatch):
    bumped = _hint_bump_probe(monkeypatch)
    _count_hint_usage(SimpleNamespace(used_hints=["h1"], from_fallback=False), "normal")
    assert bumped == [("adaptive_entry", "h1")]


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


def test_candor_records_no_tool_ok_failure_for_unavailable():
    obs, emitted = build_turn_observations(
        tool_summary={"ask_user": {"calls": 2, "failures": 0, "unavailable": 2}},
        already_emitted={},
        termination_reason=None,
        reflect_verdict=None,
        failure_cause=None,
        model="m",
        session_kind="cron",
        is_retry=False,
        ts_ms=0,
    )
    # Neither a false nor a true: the tool never ran, so nothing is observed.
    assert [o for o in obs if o["pred"] == "tool_ok"] == []
    assert emitted["ask_user"]["calls"] == 0


def test_candor_still_sees_real_failures_alongside_unavailable_calls():
    obs, _ = build_turn_observations(
        tool_summary={"ask_user": {"calls": 3, "failures": 1, "unavailable": 2}},
        already_emitted={},
        termination_reason=None,
        reflect_verdict=None,
        failure_cause=None,
        model="m",
        session_kind="cron",
        is_retry=False,
        ts_ms=0,
    )
    outcomes = [o["outcome"] for o in obs if o["pred"] == "tool_ok" and o["args"] == ["ask_user"]]
    assert outcomes == [False]


def test_unavailable_calls_are_not_a_tool_signal_failure():
    row = _pm("pass", "none", tool_summary={"ask_user": {"calls": 2, "failures": 0, "unavailable": 2}})
    assert [a for a in synthesis.attribute(row) if a.signal_type == "tool"] == []


def test_a_real_failure_beside_an_unavailable_call_still_attributes():
    row = _pm("pass", "none", tool_summary={"ask_user": {"calls": 3, "failures": 2, "unavailable": 1}})
    attrs = [a for a in synthesis.attribute(row) if a.signal_type == "tool"]
    assert len(attrs) == 1 and attrs[0].delta_failures == 1
    assert "2/2 calls failed" in attrs[0].rationale
