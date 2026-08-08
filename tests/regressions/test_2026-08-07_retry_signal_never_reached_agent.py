"""Regression: a retry's corrective signal never reached the agent.

Shipped defect (architecture review 2026-08-07, §2.3 and §3.3):

  * `_run_scout_and_process` appended `session.reflect_lessons` to the
    SCOUT's message only; the agent was invoked with the original message.
    The lessons reached the agent only if the scout LLM echoed them into
    approach_guidance — so scout bypass (which fires on retries of short
    messages, since a retry re-sends the same user text), a scout timeout,
    the deterministic fallback and cache hits all dropped them, and the
    retry re-ran a byte-identical turn.
  * Eval's `feedback_parts` existed only inside the `eval.retry` SSE event.
    An eval retry therefore carried NO new information at all and failed the
    same features again, up to eval_max_retries times.

Fix: both producers write to one channel, `_build_retry_directive`, which the
manager stamps onto `ScoutReport.retry_directive` — the report the agent
reads — regardless of which scout path produced the report.
"""

import pytest

from core.scout.report import ScoutReport
from sessions import state_v2 as sv2
from sessions.manager import SessionManager


@pytest.fixture
def mgr_and_session():
    mgr = SessionManager()
    sid = mgr.create_session(title="Retry signal")
    session = mgr.get(sid)
    sv2.transition(session, sv2.SessionStateV2.SCOUTING, "prompt-arrived")
    return mgr, session


def _bypass_scout(monkeypatch):
    """Stand in for every scout path that ignores the message: bypass,
    timeout, fallback, cache hit. All return a report built without ever
    reading the retry text."""

    async def _fake_run_scout(session_id, message, brief=None, emit=None, is_retry=False):
        return ScoutReport(
            approach_guidance="canned fallback plan",
            from_fallback=True,
            fallback_reason="bypass",
        )

    monkeypatch.setattr("core.scout.runner.run_scout", _fake_run_scout)


async def test_reflect_lessons_reach_the_agent_when_scout_is_bypassed(monkeypatch, mgr_and_session):
    mgr, session = mgr_and_session
    _bypass_scout(monkeypatch)

    seen: list[str] = []

    async def _runner(session_id, message, session, pre_saved=False, is_retry=False):
        seen.append(session.last_scout_report.to_system_prompt_section())

    mgr.set_agent_runner(_runner)
    session.reflect_lessons = "[REFLECT — Retry #1 of 2] Previous attempt wrote no file."

    await mgr._run_scout_and_process(session, "go", is_retry=True)

    assert seen, "agent runner never ran"
    assert "wrote no file" in seen[0], "reflect lessons never reached the agent's prompt"
    assert "[RETRY — PREVIOUS ATTEMPT FAILED VERIFICATION]" in seen[0]


async def test_eval_feedback_reaches_the_agent(monkeypatch, mgr_and_session):
    mgr, session = mgr_and_session
    _bypass_scout(monkeypatch)

    seen: list[str] = []

    async def _runner(session_id, message, session, pre_saved=False, is_retry=False):
        seen.append(session.last_scout_report.retry_directive)

    mgr.set_agent_runner(_runner)
    session.eval_feedback = "todo_list: the delete button does nothing"

    await mgr._run_scout_and_process(session, "build the app", is_retry=True)

    assert seen and "delete button does nothing" in seen[0]


async def test_a_normal_turn_carries_no_directive(monkeypatch, mgr_and_session):
    """Reports are cached and reused — a stale directive must not outlive the
    retry that produced it."""
    mgr, session = mgr_and_session

    stale = ScoutReport(approach_guidance="plan", retry_directive="lessons from an older turn")

    async def _fake_run_scout(session_id, message, brief=None, emit=None, is_retry=False):
        return stale

    monkeypatch.setattr("core.scout.runner.run_scout", _fake_run_scout)

    seen: list[str] = []

    async def _runner(session_id, message, session, pre_saved=False, is_retry=False):
        seen.append(session.last_scout_report.retry_directive)

    mgr.set_agent_runner(_runner)

    await mgr._run_scout_and_process(session, "a fresh question")

    assert seen == [""], "a stale retry directive leaked into a normal turn"


async def test_eval_feedback_is_stored_on_the_session(monkeypatch, tmp_path):
    """_maybe_evaluate used to emit the judge's feedback and drop it."""
    import json

    from db import models as db
    from sessions.hooks import _maybe_evaluate
    from sessions.state import AgentSession

    monkeypatch.setattr("config.settings.eval_auto", True)
    monkeypatch.setattr("config.settings.eval_max_retries", 2)

    sid = db.create_session(title="Eval feedback")
    session = db.get_session(sid)
    session_obj = AgentSession(session_id=sid)

    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "registry.json").write_text(
        json.dumps([{"id": "f1", "title": "delete button", "passes": False, "session_id": sid}])
    )

    async def _fake_eval(feat, session_id):
        return {"passed": False, "scores": {}, "feedback": "the delete button does nothing"}

    monkeypatch.setattr("core.extensions.evaluation.evaluate_single_async", _fake_eval)

    await _maybe_evaluate(sid, session, session_obj=session_obj)

    assert session_obj.eval_retry_requested
    assert "delete button does nothing" in session_obj.eval_feedback
