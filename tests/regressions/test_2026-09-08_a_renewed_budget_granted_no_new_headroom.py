"""Harness review 3.2.1 / H02, 2026-09-08: renewing a spent phase budget gave
the renewed phase nothing to spend.

`extend_session_budget` grows the cap relative to the BASE timeout —
`effective = base + additional` — and installs it only when it is larger than
what is there. That is deliberately idempotent, which is right for an
orchestrator asking for a total cap proportional to its worker count and wrong
for a fresh window measured from now. Measured against a real scheduler with a
fake clock, three consecutive goal continuations each asking for one base
timeout were granted `[1799s, 0s, 0s]`: cycles 2 and 3 raised
LLMSessionTimeoutError on their first acquire. One prior `spawn_worker` had
already installed a 3x-base cap, so even the FIRST continuation added nothing.

Three things are fixed here:

  * the goal continuation and the round-budget renewal use a clock-relative
    phase window bounded by a cumulative ceiling — a fresh window every time,
    but never an unlimited self-service bypass;
  * the extension used to happen only after `budget_exhausted`, so a
    continuation after `round_ceiling` or `complete` started on the same spent
    clock with no window at all;
  * a turn that died on its first acquire was filed as `scout_error`, which is
    not a reason `_maybe_enqueue_goal_continuation` accepts, so the goal
    stalled at `status=active` with its allowance already debited. The wall it
    hit was the clock, not the scout.
"""

from __future__ import annotations

from collections import deque
from types import SimpleNamespace

import pytest

from core.llm import semaphore as _sem_mod
from core.llm.client import MAX_SESSION_WALL_CLOCK_S, phase_budget_ceiling
from core.llm.semaphore import LLMSessionTimeoutError, SessionAwareLLMScheduler

BASE = 1800.0


class FakeClock:
    """Monotonic by construction — it only ever moves forward."""

    def __init__(self, t0: float = 10_000.0):
        self.t = t0

    def monotonic(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        assert seconds >= 0
        self.t += seconds


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(_sem_mod, "time", c)
    return c


async def _started(sem, session_id="s"):
    """Start the session's wall clock the way a first LLM call would."""
    await sem.acquire(session_id=session_id)
    sem.release()


def _route_client_to(monkeypatch, sem):
    """Point the client facade's fan-out at one real scheduler."""
    monkeypatch.setattr(
        "core.llm.client._get_router",
        lambda: SimpleNamespace(_ollama_semaphore=sem, _openrouter_semaphore=None),
    )


# ---------------------------------------------------------------------------
# The scheduler primitive
# ---------------------------------------------------------------------------


async def test_three_renewal_cycles_each_grant_a_real_window(clock):
    """The measured failure was [1799s, 0s, 0s]. Every cycle now gets a window,
    and every cycle can actually acquire."""
    sem = SessionAwareLLMScheduler(max_concurrent=1, session_timeout=BASE)
    await _started(sem)
    ceiling = phase_budget_ceiling(BASE, phases_authorized=5)

    granted, acquired = [], []
    for _cycle in range(3):
        # The phase burns its whole window and ends budget_exhausted.
        clock.advance(sem.session_seconds_remaining("s") + 1.0)
        assert sem.session_seconds_remaining("s") == 0.0
        granted.append(sem.renew_phase_budget("s", BASE, ceiling))
        try:
            await sem.acquire(session_id="s")
            sem.release()
            acquired.append(True)
        except LLMSessionTimeoutError:
            acquired.append(False)

    assert granted == [BASE, BASE, BASE]
    assert acquired == [True, True, True]
    # Cumulative allowance, not just per-cycle headroom: three windows of
    # real time were actually authorized, and the total stayed under the cap.
    assert sem._effective_timeout("s") == pytest.approx(4 * BASE + 3.0)
    assert sem._effective_timeout("s") < ceiling


async def test_the_old_primitive_is_what_granted_nothing(clock):
    """Kept as the contrast: base-relative extension is idempotent, which is
    the entire bug when the caller wanted a fresh window."""
    sem = SessionAwareLLMScheduler(max_concurrent=1, session_timeout=BASE)
    await _started(sem)

    granted = []
    for _cycle in range(3):
        clock.advance(sem.session_seconds_remaining("s") + 1.0)
        sem.extend_session_budget("s", BASE)
        granted.append(sem.session_seconds_remaining("s"))

    assert granted[0] > 0
    assert granted[1] == 0.0 and granted[2] == 0.0


async def test_a_prior_spawn_worker_cap_no_longer_swallows_the_renewal(clock):
    """spawn_worker installs (workers+1) base timeouts as a TOTAL cap. The old
    goal extension asked for base+1x on top of base and was silently a no-op."""
    sem = SessionAwareLLMScheduler(max_concurrent=1, session_timeout=BASE)
    await _started(sem)
    sem.extend_session_budget("s", 2 * BASE)  # first spawn_worker: cap = 3x base
    assert sem._effective_timeout("s") == 3 * BASE

    clock.advance(5200.0)  # the workers ran long; 200s of the cap is left
    assert sem.session_seconds_remaining("s") == pytest.approx(200.0)

    # The old call — measured: still 200s.
    assert sem.extend_session_budget("s", BASE) == 3 * BASE
    assert sem.session_seconds_remaining("s") == pytest.approx(200.0)

    ceiling = phase_budget_ceiling(BASE, phases_authorized=3, worker_count=1)
    assert sem.renew_phase_budget("s", BASE, ceiling) == pytest.approx(BASE)
    assert sem._effective_timeout("s") == pytest.approx(5200.0 + BASE)


async def test_the_cumulative_ceiling_is_hard(clock):
    """Repeated renewals are not an unlimited spending bypass. Once the
    authorized total is reached, a renewal grants nothing and queued admission
    is refused — which is the honest outcome, not another free window."""
    sem = SessionAwareLLMScheduler(max_concurrent=1, session_timeout=BASE)
    await _started(sem)
    ceiling = phase_budget_ceiling(BASE, phases_authorized=1)  # 2x base, total
    assert ceiling == 2 * BASE

    clock.advance(BASE + 1.0)
    first = sem.renew_phase_budget("s", BASE, ceiling)
    assert first == pytest.approx(ceiling - (BASE + 1.0))  # clipped by the ceiling
    assert sem._effective_timeout("s") == ceiling

    clock.advance(first + 1.0)
    second = sem.renew_phase_budget("s", BASE, ceiling)
    assert second == 0.0
    assert sem._effective_timeout("s") == ceiling  # never grew past it

    with pytest.raises(LLMSessionTimeoutError):
        await sem.acquire(session_id="s")

    # A third renewal is not a way back in.
    assert sem.renew_phase_budget("s", BASE, ceiling) == 0.0
    assert sem._effective_timeout("s") == ceiling


async def test_a_renewal_never_shrinks_an_existing_grant(clock):
    """Even when the ceiling is below a cap someone else already installed."""
    sem = SessionAwareLLMScheduler(max_concurrent=1, session_timeout=BASE)
    await _started(sem)
    sem.extend_session_budget("s", 9 * BASE)  # a big orchestration cap
    before = sem._effective_timeout("s")

    assert sem.renew_phase_budget("s", BASE, phase_budget_ceiling(BASE, 0)) > 0
    assert sem._effective_timeout("s") == before


def test_the_ceiling_counts_renewals_and_workers():
    """Headroom comes from the allowance the user actually authorized."""
    assert phase_budget_ceiling(BASE, 0) == BASE  # the running phase alone
    assert phase_budget_ceiling(BASE, 3) == 4 * BASE
    assert phase_budget_ceiling(BASE, 3, worker_count=2) == 6 * BASE
    # A 500-continuation goal is still not a week of wall-clock.
    assert phase_budget_ceiling(BASE, 500) == MAX_SESSION_WALL_CLOCK_S


async def test_unlimited_stays_unlimited(clock):
    """llm_session_timeout=0 means no cap; the router builds the scheduler with
    inf, and nothing here may quietly install a finite one."""
    sem = SessionAwareLLMScheduler(max_concurrent=1, session_timeout=float("inf"))
    await _started(sem)
    clock.advance(50_000.0)

    assert phase_budget_ceiling(0, 5) == float("inf")
    assert phase_budget_ceiling(float("inf"), 5) == float("inf")
    assert sem.renew_phase_budget("s", BASE, float("inf")) == float("inf")
    assert sem.session_seconds_remaining("s") == float("inf")
    # Background callers have no session budget to renew.
    assert sem.renew_phase_budget("", BASE, 3600.0) == float("inf")


# ---------------------------------------------------------------------------
# The goal continuation caller
# ---------------------------------------------------------------------------


def _fake_session(sid, termination="round_ceiling"):
    return SimpleNamespace(
        session_id=sid,
        session_type="normal",
        termination_reason=termination,
        pending_messages=deque(),
        worker_ids=[],
        emit_event=lambda e: None,
    )


@pytest.fixture
def mgr(monkeypatch):
    from sessions import state_v2 as sv2
    from sessions.manager import SessionManager

    monkeypatch.setattr(sv2, "_current_state", lambda s: sv2.SessionStateV2.FINALIZING)
    m = SimpleNamespace(broadcast=lambda *a, **k: None)
    m._limit_goal = lambda session, goal, reason: SessionManager._limit_goal(m, session, goal, reason)
    m._renew_continuation_budget = lambda session, budget: SessionManager._renew_continuation_budget(m, session, budget)
    return m


@pytest.mark.parametrize("termination", ["budget_exhausted", "round_ceiling", "complete"])
async def test_every_continuation_gets_a_window_whatever_ended_the_turn(termination, mgr, clock, monkeypatch):
    """The extension used to fire only after `budget_exhausted`. A synthetic
    continuation never gets the clock reset a real user message gets, so a
    round_ceiling continuation started on a clock that was already spent."""
    from db import models as db
    from sessions.manager import SessionManager

    monkeypatch.setattr("config.settings.llm_session_timeout", int(BASE))
    sem = SessionAwareLLMScheduler(max_concurrent=1, session_timeout=BASE)
    _route_client_to(monkeypatch, sem)

    sid = db.create_session(title="cont")
    db.create_goal(sid, "an objective that outlives one turn", continuation_budget=3)
    await _started(sem, sid)
    clock.advance(BASE + 1.0)
    assert sem.session_seconds_remaining(sid) == 0.0

    s = _fake_session(sid, termination=termination)
    await SessionManager._maybe_enqueue_goal_continuation(mgr, s)

    assert len(s.pending_messages) == 1
    # Real headroom, not just "the helper was called".
    assert sem.session_seconds_remaining(sid) == pytest.approx(BASE)
    await sem.acquire(session_id=sid)
    sem.release()


async def test_three_goal_continuations_in_a_row_all_get_to_run(mgr, clock, monkeypatch):
    """The end-to-end shape of the finding: an unattended goal renewing itself
    three times, each renewal actually buying time."""
    from db import models as db
    from sessions.manager import SessionManager

    monkeypatch.setattr("config.settings.llm_session_timeout", int(BASE))
    sem = SessionAwareLLMScheduler(max_concurrent=1, session_timeout=BASE)
    _route_client_to(monkeypatch, sem)

    sid = db.create_session(title="unattended")
    db.create_goal(sid, "long unattended objective", continuation_budget=3)
    await _started(sem, sid)

    headroom, ran = [], []
    for _cycle in range(3):
        clock.advance(sem.session_seconds_remaining(sid) + 1.0)
        s = _fake_session(sid, termination="budget_exhausted")
        await SessionManager._maybe_enqueue_goal_continuation(mgr, s)
        assert len(s.pending_messages) == 1
        headroom.append(sem.session_seconds_remaining(sid))
        try:
            await sem.acquire(session_id=sid)
            sem.release()
            ran.append(True)
        except LLMSessionTimeoutError:
            ran.append(False)

    # Was [1799.0, 0.0, 0.0]: cycles 2 and 3 could not make a single call.
    assert headroom[:2] == [BASE, BASE]
    assert ran == [True, True, True]
    assert db.get_active_goal(sid)["continuations_used"] == 3
    # The third window is trimmed rather than refused: the goal authorized
    # three continuations, and that total is exactly where the cap stops.
    assert 0 < headroom[2] < BASE
    assert sem._effective_timeout(sid) == phase_budget_ceiling(BASE, 3)


async def test_a_goals_continuations_cannot_outspend_their_ceiling(mgr, clock, monkeypatch):
    """An active goal is not unlimited consent."""
    from db import models as db
    from sessions.manager import SessionManager

    monkeypatch.setattr("config.settings.llm_session_timeout", int(BASE))
    sem = SessionAwareLLMScheduler(max_concurrent=1, session_timeout=BASE)
    _route_client_to(monkeypatch, sem)

    sid = db.create_session(title="greedy")
    db.create_goal(sid, "an objective with one continuation", continuation_budget=1)
    await _started(sem, sid)

    ceiling = phase_budget_ceiling(BASE, 1)
    for _cycle in range(2):
        clock.advance(sem.session_seconds_remaining(sid) + 1.0)
        s = _fake_session(sid, termination="budget_exhausted")
        await SessionManager._maybe_enqueue_goal_continuation(mgr, s)

    assert sem._effective_timeout(sid) == ceiling
    assert db.get_active_goal(sid)["status"] == "budget_limited"


async def test_an_unlimited_session_is_left_alone(mgr, clock, monkeypatch):
    from db import models as db
    from sessions.manager import SessionManager

    monkeypatch.setattr("config.settings.llm_session_timeout", 0)
    sem = SessionAwareLLMScheduler(max_concurrent=1, session_timeout=float("inf"))
    _route_client_to(monkeypatch, sem)

    sid = db.create_session(title="unlimited")
    db.create_goal(sid, "an objective with no clock", continuation_budget=2)
    await _started(sem, sid)
    clock.advance(100_000.0)

    s = _fake_session(sid, termination="round_ceiling")
    await SessionManager._maybe_enqueue_goal_continuation(mgr, s)
    assert len(s.pending_messages) == 1
    assert sid not in sem._session_timeout_override
    assert sem.session_seconds_remaining(sid) == float("inf")


# ---------------------------------------------------------------------------
# A turn that dies on its first acquire
# ---------------------------------------------------------------------------


async def test_a_dead_first_acquire_is_a_budget_cut_not_a_scout_error(monkeypatch):
    """It was recorded as `scout_error`, which `_maybe_enqueue_goal_continuation`
    does not accept — so the goal sat at `active` with the allowance spent and
    nothing running. Time-budget exhaustion stays typed and distinct from a
    provider failure."""
    from db import models as db
    from sessions import state_v2 as sv2
    from sessions.manager import SessionManager

    monkeypatch.setattr("config.settings.goals_enabled", True)

    sid = db.create_session(title="dead-acquire")
    db.create_goal(sid, "an objective whose continuation died at the gate", continuation_budget=2)

    mgr = SessionManager()
    session = mgr.get_or_create(sid)
    session.worker_ids = []

    async def _die(*_a, **_kw):
        raise LLMSessionTimeoutError(f"Session {sid[:12]} has exceeded the 1800s LLM time limit")

    finalized: list = []

    async def _finalize(session, message, system_prompt, was_cancelled):
        finalized.append(session.termination_reason)

    monkeypatch.setattr(mgr, "_run_scout_and_process", _die)
    monkeypatch.setattr(mgr, "_finalize_turn", _finalize)

    await mgr._run_agent_safe(session, "continue the goal", "")

    assert session.termination_reason == "budget_exhausted"
    assert finalized == ["budget_exhausted"]
    assert sv2._current_state(session) is sv2.SessionStateV2.FINALIZING

    # The point of typing it correctly: the goal keeps going instead of
    # stalling with a burned allowance.
    await mgr._maybe_enqueue_goal_continuation(session)
    assert len(session.pending_messages) == 1
    assert db.get_active_goal(sid)["continuations_used"] == 1


async def test_a_real_provider_failure_is_still_an_error(monkeypatch):
    """The distinction has to cut both ways."""
    from db import models as db
    from sessions.manager import SessionManager

    sid = db.create_session(title="provider-down")
    mgr = SessionManager()
    session = mgr.get_or_create(sid)

    async def _die(*_a, **_kw):
        raise RuntimeError("provider returned 503")

    async def _finalize(*_a, **_kw):
        return None

    monkeypatch.setattr(mgr, "_run_scout_and_process", _die)
    monkeypatch.setattr(mgr, "_finalize_turn", _finalize)

    await mgr._run_agent_safe(session, "do the thing", "")
    assert session.termination_reason == "scout_error"
    assert session.error == "provider returned 503"
