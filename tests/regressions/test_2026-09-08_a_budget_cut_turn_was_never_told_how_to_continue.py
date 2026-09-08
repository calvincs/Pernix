"""Agent Mesh build, 2026-09-08: three sessions spent a combined 22 hours on
an explicit "work till its completed and tested" build, took two
budget_exhausted cuts, and never wrote a single session_goals row.

goal_create's continuation_budget is exactly the mechanism for that — the
harness resumes a live goal after a budget cut or a round ceiling instead of
waiting for the user — but nothing connects the user's instruction to it. The
soft-land message now says so, at the one moment it is actionable, and only
when no goal is already doing the job.
"""

from __future__ import annotations

import pytest

from core import agent


@pytest.fixture
def soft_land(monkeypatch):
    """Drive _end_turn_on_stream_error's budget branch, capturing messages."""
    written: list[str] = []

    monkeypatch.setattr("db.models.add_message", lambda sid, role, content, **kw: written.append(content))

    async def _run(active_goal):
        monkeypatch.setattr("db.models.get_active_goal", lambda sid: active_goal)
        session = agent.AgentSession(session_id="s1")

        async def _save(role, content, **kw):
            return 1

        await agent._end_turn_on_stream_error(
            session=session,
            session_id="s1",
            error="Session s1 has exceeded the 1800s LLM time limit",
            partial_content="",
            save_turn_msg=_save,
        )
        return session, written

    return _run


async def test_a_budget_cut_points_at_the_continuation_budget(soft_land):
    session, written = await soft_land(None)
    assert session.termination_reason == "budget_exhausted"
    assert "goal_create" in written[-1]
    assert "continuation_budget" in written[-1]


async def test_the_pointer_is_suppressed_when_a_goal_already_exists(soft_land):
    """Advice the session is already following is noise."""
    _session, written = await soft_land({"id": 1, "status": "active"})
    assert "goal_create" not in written[-1]
    assert "budget exhausted" in written[-1]


async def test_the_turn_still_soft_lands_rather_than_erroring(soft_land):
    """The 2026-08 soft-land contract: a budget cut is not a session error."""
    session, _written = await soft_land(None)
    assert session.error is None
