"""Reflect wrote the retry instruction and nobody read it.

Field case, session 3dc5a307d751 (2026-09-03). Reflect graded a turn `retry`
and filled `strategy` with a concrete, different approach for the next
attempt. Neither consumer carried it forward: the turn ledger rendered only
`what_failed` (150 chars), and the next turn's scout planned from the user
message alone. The agent re-ran a variant of the approach that had just been
rejected.

Now the ledger appends the strategy on retry/escalate, and run_scout prepends
a "[PRIOR TURN GRADED …]" block to the scout's task context — but only when
the grade belongs to the immediately previous turn, and never on a pass.
"""

from __future__ import annotations

import json

from core.context.compiler import _build_turn_ledger
from core.scout.runner import _prior_turn_verdict_block
from db import models as db

WHAT_FAILED = "the side condition was never checked, so the answer is unverified"
STRATEGY = "expand the two candidate branches symbolically and check the sign of each before answering"


def _graded_turn(verdict: str = "retry", strategy: str = STRATEGY, what_failed: str = WHAT_FAILED) -> tuple[str, int]:
    """A session whose previous turn was graded, with a fresh turn open."""
    sid = db.create_session(title="graded")
    db.add_message(sid, "user", "solve the integral")
    db.add_message(sid, "assistant", "the answer is 3")
    db.add_post_mortem(
        sid,
        1,
        verdict,
        "agent" if verdict != "pass" else "none",
        0.8,
        "m",
        10,
        None,
        None,
        json.dumps({"what_failed": what_failed, "strategy": strategy}),
    )
    turn = db.add_message(sid, "user", "try again")
    return sid, turn


# --- the ledger --------------------------------------------------------------


def test_the_ledger_carries_the_strategy_not_just_the_failure(monkeypatch):
    monkeypatch.setattr("config.settings.turn_ledger_enabled", True)
    sid, turn = _graded_turn()

    block = _build_turn_ledger(sid, turn)

    assert "retry (cause=agent)" in block
    assert "side condition was never checked" in block
    assert "expand the two candidate branches" in block
    assert "grader's opinion" in block


def test_the_ledger_leaves_a_strategy_free_grade_alone(monkeypatch):
    monkeypatch.setattr("config.settings.turn_ledger_enabled", True)
    sid, turn = _graded_turn(strategy="")

    block = _build_turn_ledger(sid, turn)

    assert "side condition was never checked" in block
    assert "suggested next" not in block


# --- the next scout ----------------------------------------------------------


def test_the_scout_sees_a_fresh_retry_grade():
    sid, _ = _graded_turn()

    block = _prior_turn_verdict_block(sid)

    assert block.startswith("[PRIOR TURN GRADED RETRY — grader's opinion, not ground truth]")
    assert "what_failed: the side condition" in block
    assert "strategy: expand the two candidate branches" in block
    assert len(block) <= 600


def test_the_scout_sees_an_escalate_grade_too():
    sid, _ = _graded_turn(verdict="escalate")

    assert _prior_turn_verdict_block(sid).startswith("[PRIOR TURN GRADED ESCALATE")


def test_a_pass_never_produces_a_block():
    sid, _ = _graded_turn(verdict="pass", strategy="", what_failed="")

    assert _prior_turn_verdict_block(sid) == ""


def test_a_stale_grade_is_not_replayed():
    """One more completed turn since the grade and it describes work the
    session has moved past — the whole reason this is scoped to the
    immediately previous turn."""
    sid, _ = _graded_turn()
    db.add_message(sid, "assistant", "fixed it")
    db.add_message(sid, "user", "now something else entirely")

    assert _prior_turn_verdict_block(sid) == ""


def test_a_session_with_no_grades_is_silent():
    sid = db.create_session(title="ungraded")
    db.add_message(sid, "user", "hello")

    assert _prior_turn_verdict_block(sid) == ""


async def test_run_scout_hands_the_block_to_the_llm(monkeypatch):
    """The block must reach the scout's task context — and not the memory /
    tool-discovery searches, which key on the user's own words."""
    from core.scout import runner

    sid, _ = _graded_turn()
    seen: dict = {}

    async def _fake_llm(message, brief, **kwargs):
        seen["message"] = message
        seen["prior"] = kwargs.get("prior_verdict_block", "")
        return runner._build_fallback_report(message, brief)

    monkeypatch.setattr(runner, "_run_scout_llm", _fake_llm)
    monkeypatch.setattr("config.settings.scout_retry_on_empty_approach", False)

    await runner.run_scout(sid, "please carry on with the integral and finish the verification step")

    assert seen["prior"].startswith("[PRIOR TURN GRADED RETRY")
    assert "PRIOR TURN GRADED" not in seen["message"]
