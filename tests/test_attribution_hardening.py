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
from core.snooze import SnoozeRunner, candor_receipt
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


# ---------------------------------------------------------------------------
# 4. The candor producer exempts dialog tools and stamps a receipt
# ---------------------------------------------------------------------------


class _FakeBridge:
    def __init__(self, degraded):
        self._degraded = degraded

    async def run_maintenance(self, _cancelled):
        return {}

    async def degraded_tools(self):
        return list(self._degraded)


def _producer_harness(monkeypatch, degraded, *, live_entries=()):
    """Drive _candor_maintenance with a fake ledger and registry."""
    queued: list[list[dict]] = []

    monkeypatch.setattr("config.settings.adaptive_enabled", True)
    monkeypatch.setattr(
        "core.extensions.candor.bridge.get_candor_bridge",
        lambda: _FakeBridge(degraded),
    )

    reg = ToolRegistry()
    for name in ("ask_user", "notify_user"):
        reg.register(
            name=name,
            func=lambda: "ok",
            description=name,
            parameters={"type": "object", "properties": {}},
            category="dialog",
        )
    reg.register(name="bash", func=lambda: "ok", description="bash", parameters={"type": "object", "properties": {}})
    monkeypatch.setattr("core.tools.registry.get_registry", lambda: reg)

    monkeypatch.setattr("db.models.adaptive_get_entry", lambda eid: None)
    monkeypatch.setattr("db.models.adaptive_list_entries", lambda **kw: list(live_entries))

    def _queue(edits, source, rationale=""):
        queued.append(edits)
        return {"queued": len(edits), "gated": 0}

    monkeypatch.setattr("core.adaptive.contract.queue_producer_edits", _queue)

    runner = SnoozeRunner()
    runner._cycle_generation = runner._cancel_generation  # not cancelled
    return runner, queued


async def test_candor_producer_skips_ask_user_even_with_degraded_counts(monkeypatch):
    """The live 8-uses/7-failures ledger must not re-mint the hint."""
    runner, queued = _producer_harness(
        monkeypatch,
        [{"tool": "ask_user", "p": 0.125, "n": 8}, {"tool": "notify_user", "p": 0.2, "n": 10}],
    )
    await runner._candor_maintenance()

    minted = [e for batch in queued for e in batch if e["action"] == "create"]
    assert minted == []


async def test_candor_producer_still_mints_for_a_genuinely_degraded_tool(monkeypatch):
    runner, queued = _producer_harness(monkeypatch, [{"tool": "bash", "p": 0.3, "n": 40}])
    await runner._candor_maintenance()

    minted = [e for batch in queued for e in batch if e["action"] == "create"]
    assert len(minted) == 1
    assert minted[0]["entry_id"] == "tool-bash-degraded"
    # The receipt W4 resolves is the FIRST evidence item, in `candor:<key>` form.
    assert minted[0]["evidence"][0] == "candor:tool_ok(bash)"
    assert minted[0]["evidence"][0] == candor_receipt("bash")


async def test_a_live_ask_user_hint_is_retired_with_a_receipt(monkeypatch):
    """Exempting the tool also releases the slot its live hint holds."""
    live = [{"id": "tool-ask_user-degraded", "source": "candor", "version": 3, "kind": "routing_hint"}]
    runner, queued = _producer_harness(monkeypatch, [{"tool": "ask_user", "p": 0.125, "n": 8}], live_entries=live)
    await runner._candor_maintenance()

    retired = [e for batch in queued for e in batch if e["action"] == "delete"]
    assert len(retired) == 1
    assert retired[0]["entry_id"] == "tool-ask_user-degraded"
    assert retired[0]["evidence"][0] == candor_receipt("ask_user")
    assert "dialog tool" in retired[0]["evidence"][1]
