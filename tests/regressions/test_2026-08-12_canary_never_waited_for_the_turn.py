"""Regression: every canary run scored an agent turn that had not happened yet.

Shipped defect, found on box 2026-08-12: `canary_runs` held 99 rows and **all
99 had passed=0**. Not flakiness — the suite had never once passed since it was
enabled. The rows say why: `duration_s` between 0.15 and 0.29 and `tokens: 0`.
The gates were evaluated a fifth of a second after the prompt, against a
workspace the agent had not touched, because the agent had not started.

`SessionManager.prompt()` does not run the turn. Its last act is

    session.task = asyncio.create_task(self._run_agent_safe(...))

which only *schedules* it — and the state transition out of IDLE_READY happens
inside that coroutine, which has not been entered when `prompt()` returns. So
`run_canary`'s wait loop, which polled for "state is IDLE_READY or
AWAITING_USER", matched on its very first check and returned "the turn ended".
A textbook start race: it waited for *not running* without first waiting for
*started*.

The consequences compounded:

  * every canary scored FAIL against an untouched workspace;
  * `run_canary`'s `finally` then `shutil.rmtree`'d the temp workspace while
    the real turn was still starting up, so the orphaned agent ran on inside a
    deleted directory — observed looping to round 37, one tool call and zero
    content per round, for 30+ minutes after its sweep had already reported
    "complete";
  * and because the canary suite is the measurement layer the adaptive layer's
    auto-rollback is supposed to trust, its entire baseline was false negatives.

The existing runner tests passed throughout because their fake `prompt()` did
the work inline and parked the session before returning — modelling `prompt`
as synchronous-and-complete, which is exactly the assumption that was wrong.
The fakes here model the real contract instead: `prompt()` returns with the
session still parked and the work happening later on a task.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from db import models as db
from sessions import state_v2 as sv2
from sessions.state import TurnState


def _async_manager(monkeypatch, solve, work_time: float = 0.25):
    """Fake manager with production `prompt()` semantics.

    prompt() schedules the work and returns immediately; the session is still
    IDLE_READY at that moment and only flips to PROCESSING once the scheduled
    coroutine actually gets to run.
    """
    sessions: dict[str, SimpleNamespace] = {}

    def create_session(title="", session_type="normal", **kw):
        sid = db.create_session(title=title, session_type=session_type)
        sessions[sid] = SimpleNamespace(
            session_id=sid,
            session_type=session_type,
            workspace_override=None,
            model_override=None,
            turn=TurnState(reflect_count=0),
            cancel_requested=False,
            task=None,
            _running=False,
        )
        return sid

    async def prompt(sid, message):
        s = sessions[sid]

        async def _turn():
            s._running = True
            # Sliced rather than one long sleep so the loop can observe
            # cancel_requested, which is how the real agent loop aborts.
            waited = 0.0
            while waited < work_time:
                if s.cancel_requested:
                    s._running = False
                    return
                await asyncio.sleep(0.02)
                waited += 0.02
            solve(Path(s.workspace_override))
            s._running = False

        # Exactly what SessionManager.prompt does: schedule, then return.
        s.task = asyncio.create_task(_turn())

    mgr = SimpleNamespace(create_session=create_session, get=lambda sid: sessions.get(sid), prompt=prompt)
    monkeypatch.setattr("sessions.manager.get_manager", lambda: mgr)
    monkeypatch.setattr(
        sv2,
        "_current_state",
        lambda s: sv2.SessionStateV2.PROCESSING if getattr(s, "_running", False) else sv2.SessionStateV2.IDLE_READY,
    )
    return mgr


def _canary(**over):
    from core.canary.parser import CanaryDef

    kw = dict(
        name="race",
        prompt="write hello.txt",
        gates=[{"name": "exists", "command": "grep -qx hi hello.txt", "watch_paths": []}],
        timeout=30,
        files={"seed.txt": "s"},
    )
    kw.update(over)
    return CanaryDef(**kw)


@pytest.mark.asyncio
async def test_runner_waits_for_the_turn_it_scheduled(monkeypatch):
    """The shipped bug: gates scored before the agent had written anything.

    The agent needs ~0.25s to produce hello.txt. Under the old wait loop the
    run returned in ~0.002s with the gate failing on a file that did not exist
    yet. It must instead wait for the turn it scheduled and score the result.
    """
    _async_manager(monkeypatch, lambda ws: (ws / "hello.txt").write_text("hi\n"), work_time=0.25)

    from core.canary.runner import run_canary

    result = await run_canary(_canary(), trigger="manual")

    assert result.passed is True, (
        f"canary scored FAIL in {result.duration_s:.3f}s — the runner returned "
        f"before the agent turn ran. gates={result.gate_results}"
    )
    assert result.duration_s >= 0.25, (
        f"run took {result.duration_s:.3f}s but the turn needs 0.25s — the " "runner did not wait for it"
    )


@pytest.mark.asyncio
async def test_workspace_survives_until_the_turn_is_done(monkeypatch):
    """The second half of the bug: the temp workspace was deleted underneath
    a still-running agent, which is why orphans looped in a dead directory."""
    seen: dict[str, bool] = {}

    def _solve(ws: Path):
        # The agent's own view of the world at the moment it does its work.
        seen["workspace_existed"] = ws.exists()
        seen["seed_present"] = (ws / "seed.txt").exists()
        (ws / "hello.txt").write_text("hi\n")

    _async_manager(monkeypatch, _solve, work_time=0.2)

    from core.canary.runner import run_canary

    await run_canary(_canary(), trigger="manual")

    assert seen.get("workspace_existed") is True, "workspace was rmtree'd while the agent was still running"
    assert seen.get("seed_present") is True, "seeded files were gone before the agent could read them"


@pytest.mark.asyncio
async def test_timeout_still_fires_for_a_genuinely_stuck_turn(monkeypatch):
    """Waiting properly must not mean waiting forever."""
    _async_manager(monkeypatch, lambda ws: None, work_time=30.0)

    from core.canary.runner import run_canary

    started = time.monotonic()
    result = await run_canary(_canary(timeout=1), trigger="manual")
    elapsed = time.monotonic() - started

    assert result.passed is False
    assert "timeout" in (result.error or "").lower(), result.error
    assert elapsed < 25, f"timeout path took {elapsed:.1f}s — it should give up near the 1s budget"


@pytest.mark.asyncio
async def test_run_is_recorded_with_real_duration(monkeypatch):
    """The DB row is the artifact the adaptive layer trusts; it must reflect
    a real turn, not a race."""
    _async_manager(monkeypatch, lambda ws: (ws / "hello.txt").write_text("hi\n"), work_time=0.2)

    from core.canary.runner import run_canary

    result = await run_canary(_canary(name="recorded"), trigger="scheduled")

    rows = db.list_canary_runs(task="recorded")
    assert len(rows) == 1
    assert rows[0]["passed"] == 1
    assert rows[0]["duration_s"] >= 0.2, rows[0]["duration_s"]
    assert result.run_id


@pytest.mark.asyncio
async def test_workspace_is_kept_when_a_turn_refuses_to_end(monkeypatch):
    """A refused cancel must not end with rmtree under a live agent.

    This is the state the orphans were observed in: the turn ignored cancel,
    the run cleaned up anyway, and the agent kept going inside a directory
    that no longer existed.
    """
    kept: dict[str, Path] = {}

    def create_session(title="", session_type="normal", **kw):
        sid = db.create_session(title=title, session_type=session_type)
        sessions[sid] = SimpleNamespace(
            session_id=sid,
            session_type=session_type,
            workspace_override=None,
            model_override=None,
            turn=TurnState(reflect_count=0),
            cancel_requested=False,
            task=None,
            _running=False,
        )
        return sid

    sessions: dict[str, SimpleNamespace] = {}

    async def prompt(sid, message):
        s = sessions[sid]
        kept["ws"] = Path(s.workspace_override)

        async def _turn():  # ignores cancel_requested entirely
            s._running = True
            await asyncio.sleep(5)
            s._running = False

        s.task = asyncio.create_task(_turn())

    mgr = SimpleNamespace(create_session=create_session, get=lambda sid: sessions.get(sid), prompt=prompt)
    monkeypatch.setattr("sessions.manager.get_manager", lambda: mgr)
    monkeypatch.setattr(sv2, "_current_state", lambda s: sv2.SessionStateV2.PROCESSING)
    monkeypatch.setattr("core.canary.runner._CANCEL_GRACE_S", 0.2)

    from core.canary.runner import run_canary

    result = await run_canary(_canary(timeout=0.2), trigger="manual")

    assert result.passed is False
    assert kept["ws"].exists(), "workspace was deleted while the agent was still running"
    # Clean up what the runner deliberately left behind.
    import shutil as _sh

    _sh.rmtree(kept["ws"], ignore_errors=True)


@pytest.mark.asyncio
async def test_failure_before_the_turn_still_cleans_up(monkeypatch):
    """The cleanup guard must not leak when no turn was ever started —
    `turn_ended` is read from `finally`, so it has to be bound on every path."""
    made: dict[str, Path] = {}
    real_mkdtemp = __import__("tempfile").mkdtemp

    def _spy(*a, **kw):
        p = real_mkdtemp(*a, **kw)
        made["tmp"] = Path(p)
        return p

    monkeypatch.setattr("core.canary.runner.tempfile.mkdtemp", _spy)

    def _boom(*a, **kw):
        raise RuntimeError("session creation exploded")

    mgr = SimpleNamespace(create_session=_boom, get=lambda sid: None, prompt=None)
    monkeypatch.setattr("sessions.manager.get_manager", lambda: mgr)

    from core.canary.runner import run_canary

    result = await run_canary(_canary(), trigger="manual")

    assert result.passed is False
    assert "exploded" in (result.error or ""), result.error
    assert not made["tmp"].exists(), "temp workspace leaked when the run failed before starting a turn"
