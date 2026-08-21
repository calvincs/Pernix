"""Regression — 2026-08-21, box.

A scheduled job's allow-list refused bash/glob/discover_tools 15 times in a
week. Each refusal was a tool result with was_error=True, so tool_summary
counted it as a failure: reflect read "bash: 7 failures", candor emitted
tool_ok(bash)=False, telos minted "discover_tools failed 1/1" — about tools
that never ran. Pinned here: policy refusals are tagged at the source,
counted as `refusals` (never `failures`), shown to reflect as such, and
invisible to candor's reliability statistics.

Second pin from the same session: a deferred reflect escalated a correct
reply by attributing a scout-plan requirement to the user. The rubric now
demands a quote of the user's words for any user-attributed requirement.
"""

from types import SimpleNamespace

from core.reflect import REFLECT_PROMPT, _build_evidence
from core.tools.executor import ToolExecutionResult, _execute_single, is_policy_refusal
from core.tools.registry import ToolRegistry
from db import models as db
from sessions.state import AgentSession


def _registry(names):
    reg = ToolRegistry()
    for n in names:
        reg.register(name=n, func=lambda: "ran", description=n, parameters={"type": "object", "properties": {}})
    return reg


async def test_allowlist_refusal_is_tagged_at_the_source(monkeypatch):
    reg = _registry(["bash", "recall"])
    session = AgentSession(session_id="cron-x")
    session.tool_allowlist = frozenset({"recall"})
    monkeypatch.setattr("sessions.manager.get_manager", lambda: type("M", (), {"get": lambda self, s: session})())

    result = await _execute_single("bash", {}, {"session_id": "cron-x"}, reg)
    assert result.was_error and result.metadata.get("refused") is True
    assert is_policy_refusal(result)


def test_gate_refusals_are_recognised_by_content_and_real_errors_are_not():
    gate = ToolExecutionResult(
        "bash", "Error: Tool 'bash' requires explicit user approval for this specific call.", True, 0
    )
    scope = ToolExecutionResult("bash", "Error: The approved scope ('ls') does not mention the value(s)", True, 0)
    real = ToolExecutionResult("bash", "Error: command exited 1: No such file or directory", True, 0)
    ok = ToolExecutionResult("bash", "done", False, 0)
    assert is_policy_refusal(gate) and is_policy_refusal(scope)
    assert not is_policy_refusal(real) and not is_policy_refusal(ok)


def test_record_tool_outcome_counts_refusals_apart_from_failures():
    from core.agent import record_tool_outcome

    turn = SimpleNamespace(tool_summary={}, tool_summary_attempts=[], reflect_count=0)
    refused = ToolExecutionResult(
        "bash", "Error: Tool 'bash' is not permitted in this scheduled run — x", True, 0, metadata={"refused": True}
    )
    failed = ToolExecutionResult("bash", "Error: exit 127", True, 40)
    worked = ToolExecutionResult("bash", "ok", False, 12)
    for r in (refused, refused, failed, worked):
        record_tool_outcome(turn, r)

    s = turn.tool_summary["bash"]
    assert (s["calls"], s["failures"], s["refusals"]) == (4, 1, 2)
    assert s["errors"] == ["Error: exit 127"]  # refusal text never enters the failure previews
    assert s["refusal_errors"] == ["Error: Tool 'bash' is not permitted in this scheduled run — x"]
    a = turn.tool_summary_attempts[0]["bash"]
    assert (a["calls"], a["failures"], a["refusals"]) == (4, 1, 2)

    # A summary restored from before the key existed keeps working.
    turn.tool_summary["glob"] = {"calls": 1, "failures": 0, "errors": [], "total_latency_ms": 3}
    record_tool_outcome(
        turn, ToolExecutionResult("glob", "Error: Tool 'glob' is not permitted in this scheduled run", True, 0)
    )
    assert turn.tool_summary["glob"]["refusals"] == 1 and turn.tool_summary["glob"]["failures"] == 0


def test_reflect_sees_refusals_as_refusals_and_the_rubric_says_so():
    sid = db.create_session(title="refusals")
    db.add_message(sid, "user", "run the nightly job")
    db.add_message(sid, "assistant", "done")
    summary = {
        "bash": {
            "calls": 2,
            "failures": 0,
            "refusals": 2,
            "errors": [],
            "refusal_errors": ["Error: Tool 'bash' is not permitted in this scheduled run — charter: recall"],
            "total_latency_ms": 0,
        }
    }
    _, evidence = _build_evidence(sid, attempt=1, tool_summary=summary)
    assert "bash: 2 call(s), 0 failure(s), 0ms total, 2 policy refusal(s) — not tool failures" in evidence
    assert "REFUSED: Error: Tool 'bash' is not permitted" in evidence
    assert "ERROR:" not in evidence
    assert "REFUSALS ARE NOT FAILURES" in REFLECT_PROMPT
    assert "A REQUIREMENT ATTRIBUTED TO THE USER MUST QUOTE THE USER" in REFLECT_PROMPT


def test_candor_never_sees_a_refusal_as_tool_ok_false():
    from core.extensions.candor.emit import build_turn_observations

    summary = {"bash": {"calls": 3, "failures": 1, "refusals": 2, "errors": ["exit 1"], "total_latency_ms": 5}}
    obs, _ = build_turn_observations(
        tool_summary=summary,
        already_emitted={},
        termination_reason="complete",
        reflect_verdict="pass",
        failure_cause="none",
        model="m",
        session_kind="cron",
        is_retry=False,
        ts_ms=1,
    )
    bash_ok = [o for o in obs if o["pred"] == "tool_ok" and o["args"] == ["bash"]]
    assert [o["outcome"] for o in bash_ok].count(False) == 1  # the real failure
    assert [o["outcome"] for o in bash_ok].count(True) == 2  # calls minus failures — refusals are not successes either
