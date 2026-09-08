"""Agent Mesh build, 2026-09-07: the stuck detector asked for a change of
approach and then threw the change away.

Two mechanisms held a turn at the threshold once a tool failure was open.
`has_unresolved_failure` blocks the decrement, so even a zero-score round
could not walk `repeat_count` back down; and Signals 7 and 11 added 0.4 to
*every* later round from per-tool counters that only a success of that same
tool resets, whether or not the round touched it. So a distinct `ask_user`,
a switch to another tool, and a worker's `file_write` deliverable all reached
`_handle_stuck_signals` over the line, were discarded before `gate.admit`,
never ran, never cleared the failure — and three complied-with rounds later
the turn was stopped for not complying. d620c23's "WRITE YOUR DELIVERABLE
NOW" nudge was unfollowable in exactly the state that produces it.

These tests drive the real `StuckDetector.evaluate` into the real handler
across rounds — the d620c23 test passed `repeats=3` by hand and could not
see either mechanism.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from core.agent import StuckDetector, _handle_stuck_signals, _hash_args

NUDGE_LIMIT = 3  # STUCK_ASK_USER_LIMIT in run_agent

WORKER_TOOLS = ["bash", "file_write", "grep"]  # a worker allowlist: no ask_user
USER_TOOLS = ["bash", "file_edit", "file_write", "grep", "ask_user"]


@pytest.fixture
def captured(monkeypatch):
    """Collect system messages instead of writing them to the DB."""
    written: list[str] = []
    monkeypatch.setattr("db.models.add_message", lambda sid, role, content, **kw: written.append(content))
    return written


def _registry():
    reg = MagicMock()
    reg.exists = lambda name: name in set(USER_TOOLS)
    return reg


def _call(name, **args):
    return {"id": f"c{abs(hash(json.dumps(args, sort_keys=True))) % 9999}", "name": name, "arguments": json.dumps(args)}


class _Loop:
    """The stuck-detection slice of run_agent's tool loop, round by round."""

    def __init__(self, active_tools):
        self.stuck = StuckDetector()
        self.tool_failures: dict[str, list[str]] = {}
        self.active_tools = active_tools
        self.registry = _registry()
        self.nudges = 0

    async def round(self, calls, content=""):
        score, repeats = self.stuck.evaluate(content, calls, self.tool_failures, self.registry)
        action, self.nudges = await _handle_stuck_signals(
            session_id="s1",
            score=score,
            repeats=repeats,
            tool_calls=calls,
            active_tools=self.active_tools,
            nudges_used=self.nudges,
            nudge_limit=NUDGE_LIMIT,
            turn_user_msg_id=7,
            stuck=self.stuck,
        )
        return action

    def executed_failure(self, call):
        """What _record_round_results does when the call ran and errored."""
        args = json.loads(call["arguments"])
        self.tool_failures.setdefault(call["name"], []).append(_hash_args(call["arguments"]))
        self.stuck.mark_failure(tool_name=call["name"], args=args)

    def rejected(self, call):
        """What _ToolCallGate._reject does: a failure with no tool name."""
        self.tool_failures.setdefault(call["name"], []).append(_hash_args(call["arguments"]))
        self.stuck.mark_failure()


async def _grind_to_threshold(loop, call, *, rejected=False):
    """Fail the same call until the detector is actually at its threshold.

    The last of these rounds is the one that trips it and draws the first
    nudge — the nudge that asks for the recovery the tests below attempt.
    """
    for _ in range(4):
        await loop.round([call])
        loop.rejected(call) if rejected else loop.executed_failure(call)
    assert loop.stuck.repeat_count >= 3, "setup did not reach the stuck threshold"
    assert loop.stuck.has_unresolved_failure
    assert loop.nudges == 1, "setup should have drawn exactly the first nudge"
    return loop


# ---------------------------------------------------------------------------
# (i)-(iii): the three recovery shapes must run
# ---------------------------------------------------------------------------


async def test_a_distinct_ask_user_runs_instead_of_being_discarded(captured):
    """The reviewer's shape: four rejected file_edits, then the agent asks."""
    loop = _Loop(USER_TOOLS)
    await _grind_to_threshold(loop, _call("file_edit", path="x.py"), rejected=True)

    action = await loop.round([_call("ask_user", question="Which file should I edit?")])

    assert action == "proceed"
    assert loop.stuck.recovery_passes == 1


async def test_a_genuinely_different_tool_runs(captured):
    """A switch away from the failing tool is the change of approach asked for."""
    loop = _Loop(USER_TOOLS)
    await _grind_to_threshold(loop, _call("bash", command="curl https://x"))

    action = await loop.round([_call("grep", pattern="TODO", path="notes.md")])

    assert action == "proceed"


async def test_a_workers_deliverable_runs_with_no_ask_user_available(captured):
    """The post-d620c23 worker: told to WRITE YOUR DELIVERABLE NOW, it did."""
    loop = _Loop(WORKER_TOOLS)
    await _grind_to_threshold(loop, _call("bash", command="curl https://x"))

    action = await loop.round([_call("file_write", path="report.md", content="findings, with gaps")])

    assert action == "proceed"
    assert "ask_user" not in loop.active_tools


async def test_the_nudge_the_worker_was_given_is_the_move_it_is_now_allowed(captured):
    """The instruction and the permitted move have to agree."""
    loop = _Loop(WORKER_TOOLS)
    call = _call("bash", command="curl https://x")
    await _grind_to_threshold(loop, call)
    assert await loop.round([call]) == "nudge-and-retry"
    assert "WRITE YOUR DELIVERABLE" in captured[-1]

    assert await loop.round([_call("file_write", path="report.md", content="x")]) == "proceed"


# ---------------------------------------------------------------------------
# (iv): real repetition is still bounded
# ---------------------------------------------------------------------------


async def test_the_same_failing_call_is_still_nudged_then_stopped(captured):
    """Nothing about the loop-breaker changes for an agent that repeats itself."""
    loop = _Loop(WORKER_TOOLS)
    call = _call("bash", command="curl https://x")
    await _grind_to_threshold(loop, call)

    actions = []
    for _ in range(5):
        action = await loop.round([call])
        actions.append(action)
        if action == "stop":
            break

    assert "proceed" not in actions
    assert actions[-1] == "stop"
    assert loop.nudges == NUDGE_LIMIT
    assert loop.stuck.recovery_passes == 0


async def test_recovery_is_bounded_to_two_passes_per_turn(captured):
    """Five fresh deliverable attempts, each failing: only the first two are
    exempt, then the ordinary nudge-then-stop ladder resumes."""
    loop = _Loop(WORKER_TOOLS)
    await _grind_to_threshold(loop, _call("bash", command="curl https://x"))

    actions = []
    for i in range(5):
        write = _call("file_write", path=f"report{i}.md", content="x")
        actions.append(await loop.round([write]))
        loop.executed_failure(write)

    assert actions[:2] == ["proceed", "proceed"]
    assert "proceed" not in actions[2:]
    assert actions[-1] == "stop"
    assert loop.stuck.recovery_passes == 2


async def test_a_recovery_pass_does_not_spend_the_nudge_budget(captured):
    """Recovery and the nudge ladder are separate budgets."""
    loop = _Loop(USER_TOOLS)
    await _grind_to_threshold(loop, _call("bash", command="curl https://x"))
    spent_before = loop.nudges

    await loop.round([_call("ask_user", question="what now?")])

    assert loop.nudges == spent_before


# ---------------------------------------------------------------------------
# Signals 7 and 11 score the current move, but keep their memory
# ---------------------------------------------------------------------------


async def test_a_failing_tools_counter_does_not_score_a_round_that_avoids_it(captured):
    """Signal 11 charged every later round for a bash streak it wasn't part of."""
    loop = _Loop(WORKER_TOOLS)
    bash = _call("bash", command="curl https://x")
    for _ in range(3):
        await loop.round([bash])
        loop.executed_failure(bash)
    assert loop.stuck.tool_failure_counts["bash"] >= 3

    score, _ = loop.stuck.evaluate(
        "", [_call("file_write", path="report.md", content="x")], loop.tool_failures, loop.registry
    )

    assert score <= 0.3
    assert "tool_failure_loop" not in loop.stuck.behavioral_flags


async def test_the_counter_is_still_sticky_when_the_tool_comes_back(captured):
    """Scoping the score must not forgive the streak: bash is still in a loop."""
    loop = _Loop(WORKER_TOOLS)
    bash = _call("bash", command="curl https://x")
    for _ in range(3):
        await loop.round([bash])
        loop.executed_failure(bash)
    loop.stuck.evaluate("", [_call("file_write", path="report.md", content="x")], loop.tool_failures, loop.registry)

    score, _ = loop.stuck.evaluate("", [_call("bash", command="curl https://y")], loop.tool_failures, loop.registry)

    assert score >= 0.4
    assert "tool_failure_loop" in loop.stuck.behavioral_flags


async def test_a_file_counter_does_not_score_a_write_to_another_path(captured):
    """Signal 7's twin: three failed edits of foo.py, then a write elsewhere."""
    sd = StuckDetector()
    reg = _registry()
    for _ in range(3):
        sd.mark_failure(tool_name="file_edit", args={"path": "foo.py"})

    sd.evaluate("", [_call("file_write", path="bar.md", content="x")], {}, reg)
    assert "file_edit_loop" not in sd.behavioral_flags

    score, _ = sd.evaluate("", [_call("file_edit", path="foo.py", old="a", new="b")], {}, reg)
    assert score >= 0.4
    assert "file_edit_loop" in sd.behavioral_flags


# ---------------------------------------------------------------------------
# The classifier's edges
# ---------------------------------------------------------------------------


async def test_a_guess_at_the_same_failing_tool_is_not_a_recovery(captured):
    """Novel arguments to the tool that is already failing is the loop itself."""
    loop = _Loop(WORKER_TOOLS)
    await _grind_to_threshold(loop, _call("bash", command="curl https://x"))

    action = await loop.round([_call("bash", command="curl https://y")])

    assert action == "nudge-and-retry"
    assert loop.stuck.recovery_passes == 0


async def test_a_move_the_session_cannot_run_is_not_a_recovery(captured):
    """A worker calling ask_user it does not have would just be rejected."""
    loop = _Loop(WORKER_TOOLS)
    await _grind_to_threshold(loop, _call("bash", command="curl https://x"))

    action = await loop.round([_call("ask_user", question="help?")])

    assert action == "nudge-and-retry"


async def test_the_handler_still_works_without_a_detector(captured):
    """The classifier is opt-in: callers that pass no detector are unchanged."""
    action, used = await _handle_stuck_signals(
        session_id="s1",
        score=0.0,
        repeats=3,
        tool_calls=[{"name": "ask_user", "arguments": "{}"}],
        active_tools=USER_TOOLS,
        nudges_used=0,
        nudge_limit=NUDGE_LIMIT,
    )
    assert action == "nudge-and-retry"
    assert used == 1
