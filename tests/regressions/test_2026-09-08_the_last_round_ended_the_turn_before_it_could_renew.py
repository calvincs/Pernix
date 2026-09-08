"""Harness review 3.2.1 / H01, 2026-09-08: `round_cap_auto_continue` was dead
code for every provider that behaves.

The loop disabled tools on the last configured round, and the resource status
told the model in as many words to stop calling tools and summarize. The model
did. A tool-less response is the loop's completion signal, so the turn returned
`termination_reason="complete"` — and the renewal branch, which sat AFTER tool
execution and after the bottom-of-loop increment, could only be reached by a
round that EXECUTED tools while tools were switched off. Measured against the
unfixed source with a compliant fake provider (max_tool_rounds=6,
round_cap_auto_continue=1): 6 LLM calls, tools offered [T,T,T,T,T,False], zero
renewals, `complete`. A provider that ignored `tools=None` got 13 calls and the
renewal — the branch was live only for misbehaviour.

Two things were wrong at once, and both are covered here:

  * the renewal decision now happens BEFORE the tools-disabled round is
    entered, so the extension lands while tools are still on the table;
  * a tool-less reply on a round the harness disarmed is no longer read as
    completion. It keeps `round_ceiling`, which is what reflect's ceiling-loop
    guard and the worker INCOMPLETE header key on — a round-exhausted worker
    used to reach its parent looking like a worker that had finished.

The window index and the turn's total rounds are now separate numbers, so a
renewal no longer resets the in-turn goal-budget checkpoint cadence with it.
"""

from __future__ import annotations

import inspect
import json
import re
from types import SimpleNamespace

import pytest

from core.agent import _build_resource_status, _round_renewal_refusal, run_agent
from core.llm.types import StreamEvent, StreamEventType, TokenUsage, ToolCall
from core.scout.report import ScoutReport
from sessions.state import AgentSession
from tests.conftest import FakeLLMClient


class ToolHungryClient(FakeLLMClient):
    """Requests a tool whenever tools are offered; answers in prose when not.

    Exactly the provider H01's acceptance spec asks for: it never volunteers
    to stop, so every turn that ends did so because the harness ended it.
    """

    def __init__(self, extends: list, session, on_call=None):
        super().__init__()
        self.tools_offered: list[bool] = []
        self.extends_seen: list[int] = []
        self.n = 0
        self._extends = extends
        self._session = session
        self._on_call = on_call

    async def chat_stream(self, messages, tools=None, model="", **kw):
        self.n += 1
        self.tools_offered.append(tools is not None)
        # How many budget extensions had already been granted when this call
        # was dispatched — the number that proves ordering.
        self.extends_seen.append(len(self._extends))
        self.calls.append({"messages": messages, "tools": tools, "model": model})
        if self._on_call:
            self._on_call(self.n, tools, self._session)
        if tools:
            yield StreamEvent(
                type=StreamEventType.TOOL_CALL,
                tool_calls=[ToolCall(id=f"tc{self.n}", name="step_tool", arguments=json.dumps({"n": self.n}))],
            )
        else:
            yield StreamEvent(
                type=StreamEventType.TOKEN,
                content=f"Partial summary after {self.n} rounds. Not finished.",
            )
        yield StreamEvent(
            type=StreamEventType.USAGE,
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )
        yield StreamEvent(type=StreamEventType.DONE)


class SilentClient(ToolHungryClient):
    """Never calls a tool and never says anything useful."""

    async def chat_stream(self, messages, tools=None, model="", **kw):
        self.n += 1
        self.tools_offered.append(tools is not None)
        self.extends_seen.append(len(self._extends))
        if self._on_call:
            self._on_call(self.n, tools, self._session)
        yield StreamEvent(type=StreamEventType.TOKEN, content="")
        yield StreamEvent(type=StreamEventType.DONE)


class RamblingClient(ToolHungryClient):
    """Writes prose until it is cut off by max_tokens, and never calls a tool.

    Its rounds are consumed by length continuations, so the window reaches the
    terminal round having executed nothing.
    """

    async def chat_stream(self, messages, tools=None, model="", **kw):
        self.n += 1
        self.tools_offered.append(tools is not None)
        self.extends_seen.append(len(self._extends))
        if self._on_call:
            self._on_call(self.n, tools, self._session)
        yield StreamEvent(type=StreamEventType.TOKEN, content=f"still writing ({self.n})")
        yield StreamEvent(
            type=StreamEventType.DONE,
            finish_reason="length" if tools is not None else "stop",
        )


def _registry(monkeypatch):
    from core.tools.registry import ToolRegistry

    reg = ToolRegistry()
    reg.register(
        name="step_tool",
        func=lambda n=0: f"step {n} done",
        description="do one step",
        parameters={"type": "object", "properties": {"n": {"type": "integer"}}},
        parallel_safe=True,
        timeout=5,
    )
    monkeypatch.setattr("core.agent.get_registry", lambda: reg)
    return reg


async def _drive(
    monkeypatch,
    *,
    rounds: int,
    renewals: int,
    client_cls=ToolHungryClient,
    on_call=None,
    session_type: str = "normal",
):
    """Run one whole turn and hand back everything worth asserting on."""
    from db import models as db

    monkeypatch.setattr("config.settings.max_tool_rounds", rounds)
    monkeypatch.setattr("config.settings.round_cap_auto_continue", renewals)
    extends: list[tuple[str, float, float]] = []
    monkeypatch.setattr(
        "core.llm.client.renew_phase_budget",
        lambda sid, window, ceiling=0.0: extends.append((sid, window, ceiling)) or window,
    )

    sid = db.create_session(title="H01")
    session = AgentSession(session_id=sid, session_type=session_type)
    session.last_scout_report = ScoutReport(recommended_tools=["step_tool"])
    fake = client_cls(extends, session, on_call=on_call)
    monkeypatch.setattr("core.agent.get_llm_client", lambda: fake)
    monkeypatch.setattr("core.llm.client._client", fake)
    _registry(monkeypatch)

    await run_agent(sid, "keep working, this needs many rounds", session)

    msgs = db.get_messages(sid)
    return SimpleNamespace(
        sid=sid,
        session=session,
        fake=fake,
        extends=extends,
        messages=msgs,
        system_texts=[m["content"] for m in msgs if m["role"] == "system"],
        tool_rows=[m for m in msgs if m["role"] == "tool"],
    )


def _renewal_notices(run) -> list[str]:
    return [t for t in run.system_texts if "round budget renewed" in t]


# ---------------------------------------------------------------------------
# The headline: the extension lands before the tools go away
# ---------------------------------------------------------------------------


async def test_the_renewal_lands_before_the_tools_disappear(monkeypatch):
    """One authorized renewal, and it is spent while tools are still offered.

    Unfixed, this run was 4 calls ending in `complete` with zero renewals.
    """
    run = await _drive(monkeypatch, rounds=4, renewals=1)

    # Window 1 spends rounds 0-2 on tools; round 3 would have been the
    # tools-disabled round, so the renewal is taken there instead and window 2
    # gets a full four rounds of its own.
    assert run.fake.tools_offered == [True, True, True, True, True, True, False]
    assert run.fake.n == 7
    assert len(run.tool_rows) == 6

    # Ordering, not just occurrence: by the time tools were withheld the
    # extension had already been granted.
    assert len(run.extends) == 1
    assert run.fake.extends_seen[-1] == 1
    assert run.fake.tools_offered.index(False) == len(run.fake.tools_offered) - 1

    notices = _renewal_notices(run)
    assert len(notices) == 1
    assert "(1/1)" in notices[0]
    assert "LAST renewal" in notices[0]

    # The forced prose answer is persisted, and the wall it hit is typed.
    assert run.session.termination_reason == "round_ceiling"
    assert run.messages[-1]["role"] == "assistant"
    assert "Not finished" in run.messages[-1]["content"]


async def test_zero_renewals_still_ends_as_a_ceiling_not_a_completion(monkeypatch):
    """The un-renewable case is where the mislabelling was most expensive."""
    run = await _drive(monkeypatch, rounds=4, renewals=0)

    assert run.fake.tools_offered == [True, True, True, False]
    assert run.fake.n == 4
    assert len(run.tool_rows) == 3
    assert run.extends == []
    assert _renewal_notices(run) == []
    # Was "complete": the harness disabled the tools, told the model to
    # summarize, then read the summary as proof the task was done.
    assert run.session.termination_reason == "round_ceiling"


async def test_every_authorized_renewal_opens_a_full_window(monkeypatch):
    """Three renewals, three extensions, and the copy counts down honestly."""
    run = await _drive(monkeypatch, rounds=3, renewals=3)

    # 2 tool rounds per window, three renewals, then a final window that runs
    # its two tool rounds and its tools-disabled round.
    assert run.fake.tools_offered == [True] * 8 + [False]
    assert len(run.tool_rows) == 8
    assert len(run.extends) == 3
    assert run.fake.extends_seen[-1] == 3

    notices = _renewal_notices(run)
    assert [n.count("/3") for n in notices] == [1, 1, 1]
    assert "(1/3)" in notices[0] and "2 further renewal(s)" in notices[0]
    assert "(2/3)" in notices[1] and "1 further renewal(s)" in notices[1]
    assert "(3/3)" in notices[2] and "LAST renewal" in notices[2]
    assert run.session.termination_reason == "round_ceiling"


# ---------------------------------------------------------------------------
# What must NOT buy a renewal
# ---------------------------------------------------------------------------


async def test_a_window_that_ran_no_tools_buys_no_renewal(monkeypatch):
    """A renewal pays for observed progress, never for an empty response."""
    run = await _drive(
        monkeypatch,
        rounds=2,
        renewals=1,
        client_cls=SilentClient,
    )

    assert run.fake.n == 1
    assert run.extends == []
    assert _renewal_notices(run) == []
    # Round 0 answered with no tools and no content — a genuine tool-less
    # finish, on a round the harness had not disarmed.
    assert run.session.termination_reason == "complete"


async def test_a_window_of_length_continuations_buys_no_renewal(monkeypatch):
    """Reaching the terminal round without executing a single tool call is
    not progress, however many LLM calls it took to get there."""
    run = await _drive(monkeypatch, rounds=2, renewals=1, client_cls=RamblingClient)

    assert run.tool_rows == []
    assert run.extends == []
    assert _renewal_notices(run) == []
    assert run.fake.tools_offered == [True, False]
    assert run.session.termination_reason == "round_ceiling"


async def test_cancellation_at_the_window_boundary_takes_no_renewal(monkeypatch):
    """A cancel landing on the last tool round is a stop, not a pause."""

    def _cancel_on_last_tool_round(n, tools, session):
        if n == 2:
            session.cancel_requested = True

    run = await _drive(monkeypatch, rounds=3, renewals=1, on_call=_cancel_on_last_tool_round)

    assert run.fake.tools_offered == [True, True]
    assert run.extends == []
    assert _renewal_notices(run) == []
    assert run.session.termination_reason == "cancelled"


async def test_the_refusal_reasons_are_checked_before_a_window_is_opened(monkeypatch):
    """Each guard the renewal owes: an explicit stop, a human with something
    to say, a genuine no-progress stop, and the goal's own ceiling."""

    def _session(**kw):
        s = AgentSession(session_id="s1")
        for k, v in kw.items():
            setattr(s, k, v)
        return s

    ok = SimpleNamespace(repeat_count=0)
    assert await _round_renewal_refusal(_session(), "s1", ok) is None

    assert "cancel" in (await _round_renewal_refusal(_session(cancel_requested=True), "s1", ok))
    assert "errored" in (await _round_renewal_refusal(_session(error="boom"), "s1", ok))
    assert "no progress" in (await _round_renewal_refusal(_session(), "s1", SimpleNamespace(repeat_count=3)))

    queued = _session()
    queued.pending_messages.append(("what about the other thing?", None, False))
    assert "user message is queued" in (await _round_renewal_refusal(queued, "s1", ok))

    goal_bound = _session(active_goal_id=7)
    monkeypatch.setattr("config.settings.goals_enabled", True)
    monkeypatch.setattr("core.agent._goal_budget_exceeded", lambda gid: None)
    assert await _round_renewal_refusal(goal_bound, "s1", ok) is None
    monkeypatch.setattr("core.agent._goal_budget_exceeded", lambda gid: "token budget spent (10/10)")
    refusal = await _round_renewal_refusal(goal_bound, "s1", ok)
    assert refusal is not None and "goal budget spent" in refusal


# ---------------------------------------------------------------------------
# The 397c4ce late-correction pass survives the new ordering
# ---------------------------------------------------------------------------


async def test_a_late_correction_on_the_final_round_still_gets_its_pass(monkeypatch):
    """The user amends the message while the forced synthesis is streaming.

    The reread pass must still fire, must not spend a round, and the turn must
    still land on the wall it actually hit.
    """

    def _amend_during_the_forced_answer(n, tools, session):
        if tools is None and session.turn.user_row_version == 0:
            session.turn.user_row_version = 1

    run = await _drive(monkeypatch, rounds=2, renewals=0, on_call=_amend_during_the_forced_answer)

    assert run.fake.tools_offered == [True, False, False]  # the reread costs no round
    assert any("the user edited the message you were answering" in t for t in run.system_texts)
    assert run.session.termination_reason == "round_ceiling"


# ---------------------------------------------------------------------------
# Where the typed wall is read
# ---------------------------------------------------------------------------


async def test_a_round_exhausted_worker_does_not_look_finished(monkeypatch):
    """A worker that ran out of rounds reached its parent with no INCOMPLETE
    header at all, because `complete` is not one of the reasons that earns
    one. The header and reflect's ceiling-loop guard both key on the string."""
    run = await _drive(monkeypatch, rounds=3, renewals=0, session_type="worker")
    reason = run.session.termination_reason
    assert reason == "round_ceiling"

    from core import reflect as _reflect
    from sessions import manager as _manager

    assert '_loop_walls = ("round_ceiling", "stuck_loop")' in inspect.getsource(_reflect)
    # Both guards are function-local, so this reads the source. Match the
    # membership test rather than one exact tuple: the set grew when worker
    # trust moved into orchestration, and the invariant is that a round-capped
    # worker is reported INCOMPLETE, not the order of the strings beside it.
    _mgr_src = inspect.getsource(_manager)
    assert "INCOMPLETE" in _mgr_src
    assert re.search(r'in \([^)]*"round_ceiling"[^)]*\)', _mgr_src), "manager no longer treats round_ceiling as a cap"


# ---------------------------------------------------------------------------
# The prompt copy stops lying about what is left
# ---------------------------------------------------------------------------


def test_the_status_does_not_call_the_last_round_last_while_renewals_remain(monkeypatch):
    from db import models as db

    monkeypatch.setattr("config.settings.max_tool_rounds", 10)
    sid = db.create_session(title="status")

    spent = _build_resource_status(sid, None, tool_round=9, renewals_remaining=2)
    assert "LAST ROUND" not in spent
    assert "2 automatic renewal(s)" in spent
    assert "do not wrap up" in spent

    final = _build_resource_status(sid, None, tool_round=9, renewals_remaining=0)
    assert "LAST ROUND (tools disabled)" in final
    assert "the only binding limit" in final


@pytest.mark.parametrize("left", [0, 2])
async def test_the_grant_copy_describes_the_real_remaining_allowance(left, monkeypatch):
    """It always said "No further continuations follow this one" — an agent
    with two renewals left was told to wrap up on the first."""
    from core import agent as _agent

    written: list[str] = []
    monkeypatch.setattr("db.models.add_message", lambda sid, role, content, **kw: written.append(content))
    monkeypatch.setattr("core.llm.client.extend_session_budget", lambda sid, secs: 0.0)
    monkeypatch.setattr("config.settings.max_tool_rounds", 40)

    await _agent._grant_round_renewal(
        AgentSession(session_id="s1"),
        "s1",
        granted=1,
        authorized=1 + left,
        rounds_spent=39,
    )
    text = written[-1]
    assert "40 fresh tool rounds" in text
    if left:
        assert f"{left} further renewal(s) can follow it." in text
        assert "LAST renewal" not in text
    else:
        assert "LAST renewal" in text
