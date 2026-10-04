"""A paused turn that died on compaction had nowhere to go.

`2dee81b` routed `PAUSE_REQUESTED` through the same termination branch as
`PROCESSING`, which maps `compaction_failed` to the reason
`compaction-failed` — an edge only `COMPACTING` declared. The same commit
made `transition()` return `False` on an undeclared pair instead of forcing
it, so the `except Exception` wrappers around every call never fired and the
rejection was a single WARNING. The turn stayed in `PAUSE_REQUESTED`: no post
hooks, queued prompts never started, pause and cancel hidden in the UI, until
the reaper's sixty-second unstick.

The second instance of the same class: `_run_agent_safe`'s
finalization-failure handler picks `reaper-unstick` for whatever state
finalization died in, and `CANCELLING` had no such edge.

Both edges are declared now. The rest of this file closes the class: every
`(state, reason)` pair the two runtime producers can compose is enumerated
and checked against the graph, and a rejection is made loud — ERROR log plus
a per-edge counter that `/api/health/detailed` publishes.

Writing that enumeration found a third instance immediately: an exhausted or
stalled compactor never enters `COMPACTING` at all, so the same
`compaction-failed` break can fire from plain `PROCESSING`. That edge is
declared too.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock

import pytest

from config import settings
from db import models as db
from sessions import state_v2 as sv2
from sessions.manager import (
    TERMINATION_ROUTED_STATES,
    TERMINATION_TO_V2,
    SessionManager,
    TurnExecution,
    _map_termination_to_v2_reason,
    finalize_failure_reason,
)

S = sv2.SessionStateV2


@pytest.fixture
def manager(monkeypatch):
    mgr = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", mgr)
    monkeypatch.setattr(settings, "goals_enabled", False)
    monkeypatch.setattr(mgr, "_run_post_hooks", AsyncMock())
    sv2.reset_rejected_transitions()
    yield mgr
    sv2.reset_rejected_transitions()


def make_run(mgr):
    sid = mgr.create_session(title="missing edges")
    return mgr.get(sid), TurnExecution(sid)


# ---------------------------------------------------------------------------
# The two edges that were missing
# ---------------------------------------------------------------------------


def test_a_paused_turn_can_end_on_a_failed_compaction():
    assert sv2.TRANSITIONS[(S.PAUSE_REQUESTED, "compaction-failed")] is S.FINALIZING


def test_finalization_can_unstick_a_cancelling_session():
    assert sv2.TRANSITIONS[(S.CANCELLING, "reaper-unstick")] is S.IDLE_READY


# ---------------------------------------------------------------------------
# The class the two edges belong to: every pair a producer can compose
# ---------------------------------------------------------------------------


def _agent_termination_reasons() -> set[str]:
    """Every literal core/agent.py assigns to session.termination_reason.

    The mapping defaults unknown strings to loop-complete, so this is not
    about the mapping being complete — it is about the *resulting* edge being
    declared from both states that route through it.
    """
    import re
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "core" / "agent.py").read_text()
    return set(re.findall(r'termination_reason = "([a-z_]+)"', source))


def test_every_termination_reason_maps_to_a_declared_edge_from_every_routed_state():
    """_map_termination_to_v2_reason × the states manager.py routes through it.

    PAUSE_REQUESTED joined PROCESSING in this branch in 2dee81b and nothing
    re-checked the product. compaction_failed was the pair that fell out.
    """
    reasons = set(TERMINATION_TO_V2) | _agent_termination_reasons() | {None}
    assert "compaction_failed" in reasons  # the one that broke
    assert set(TERMINATION_ROUTED_STATES) == {S.PROCESSING, S.PAUSE_REQUESTED}
    missing = []
    for state in TERMINATION_ROUTED_STATES:
        for tr in reasons:
            v2_reason, _term = _map_termination_to_v2_reason(tr)
            if (state, v2_reason) not in sv2.TRANSITIONS:
                missing.append((state.value, tr, v2_reason))
    assert not missing, f"undeclared edges a finishing turn can emit: {missing}"


def test_finalization_failure_has_a_declared_exit_from_every_state_it_can_die_in():
    """finalize_failure_reason × every state but IDLE_READY.

    The handler runs under `if current is not IDLE_READY`, so every other
    state is reachable there — including CANCELLING, which had no edge.
    """
    missing = []
    for state in S:
        if state is S.IDLE_READY:
            continue
        reason = finalize_failure_reason(state)
        if sv2.TRANSITIONS.get((state, reason)) is not S.IDLE_READY:
            missing.append((state.value, reason))
    assert not missing, f"finalization would strand these states: {missing}"


def test_the_reaper_reasons_stay_in_the_timeline_map():
    """Both new edges reuse a (from, to) pair the map already draws, so the
    timeline stays in parity. Asserted here too so a later widening of the
    graph in this stream cannot silently outrun static/'s MAP_EDGES."""
    import re
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "static/js/components/modals/timeline.js").read_text()
    drawn = set(re.findall(r"\{ from: '([^']+)', to: '([^']+)'", source))
    assert ("pause_requested", "finalizing") in drawn
    assert ("cancelling", "idle_ready") in drawn
    assert drawn == {(start.value, end.value) for (start, _r), end in sv2.TRANSITIONS.items()}


# ---------------------------------------------------------------------------
# The runtime path: a paused turn that ends on compaction_failed settles
# ---------------------------------------------------------------------------


async def test_a_pause_requested_turn_ending_on_compaction_failure_reaches_idle(manager, monkeypatch):
    session, execution = make_run(manager)

    async def pipeline(session, message, **kwargs):
        sv2.transition(session, S.PROCESSING, "scout-done")
        session.pause_event.clear()
        sv2.transition(session, S.PAUSE_REQUESTED, "pause-requested")
        session.termination_reason = "compaction_failed"

    monkeypatch.setattr(manager, "_run_scout_and_process", pipeline)
    session.task = asyncio.create_task(manager._run_agent_safe(session, "", "", execution=execution))
    await asyncio.wait_for(session.task, 10)

    assert sv2._current_state(session) is S.IDLE_READY
    assert sv2.rejected_transition_stats()["total"] == 0
    log = db.get_state_log(session.session_id)
    hops = [(r["from_state"], r["reason"], r["to_state"]) for r in log]
    assert ("pause_requested", "compaction-failed", "finalizing") in hops
    assert not any(str(r["reason"]).startswith("invariant-violation") for r in log)


# ---------------------------------------------------------------------------
# A rejection is loud and counted
# ---------------------------------------------------------------------------


async def test_an_undeclared_pair_is_logged_at_error_and_counted(manager, caplog):
    session, _execution = make_run(manager)
    sv2.transition(session, S.SCOUTING, "prompt-arrived")

    with caplog.at_level(logging.ERROR, logger="pernix.session.state_v2"):
        accepted = sv2.transition(session, S.AWAITING_USER, "no-such-reason")

    assert accepted is False
    assert sv2._current_state(session) is S.SCOUTING  # the turn did not advance
    assert any("no-such-reason" in r.getMessage() and r.levelno == logging.ERROR for r in caplog.records)

    stats = sv2.rejected_transition_stats()
    assert stats["total"] == 1
    assert stats["edges"] == {"scouting--(no-such-reason)-->awaiting_user": 1}

    sv2.transition(session, S.AWAITING_USER, "no-such-reason")
    assert sv2.rejected_transition_stats()["edges"]["scouting--(no-such-reason)-->awaiting_user"] == 2


async def test_health_detailed_publishes_the_rejection_counter(manager, monkeypatch):
    import api.routers.health as health

    session, _execution = make_run(manager)
    sv2.transition(session, S.SCOUTING, "prompt-arrived")
    sv2.transition(session, S.AWAITING_USER, "no-such-reason")

    class _Req:
        client = type("C", (), {"host": "127.0.0.1"})()

    async def _check_health():
        return {}

    monkeypatch.setattr(health, "is_local_client", lambda host: True)
    monkeypatch.setattr(
        "core.llm.client.get_llm_client",
        lambda: type("C", (), {"check_health": staticmethod(_check_health)})(),
    )
    body = await health.health_detailed(_Req())
    assert body["sessions"]["rejected_transitions"]["total"] == 1
    assert "scouting--(no-such-reason)-->awaiting_user" in body["sessions"]["rejected_transitions"]["edges"]
