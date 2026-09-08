"""A cron job's model pin and tool charter landed on the user's live turn.

`_dispatch_prompt` wrote `session.model_override` and `session.tool_allowlist`
onto the AgentSession *before* calling `prompt()`, which is the call that
decides whether a message starts a turn or queues behind one. Against a busy
session that produced three separate failures from one mistake:

  * the user's own turn — already running, nothing to do with the job — was
    reconfigured under it: a different model for its next LLM call, and an
    exclusive tool charter the executor backstop then enforced, so a `bash`
    the user had asked for came back refused;
  * the cleanup assigned `None` rather than restoring what was there, so a
    model the user had pinned through the API was gone for the life of the
    session (that route writes memory only — the DB restore is worker-only)
    and a worker kind's confinement was stripped until the next rehydration;
  * and the job's own turn, when it finally popped off the queue, ran with
    `model_override=None` and `tool_allowlist=None` — the exact allow-list
    bypass the code's own comment claimed to prevent. An earlier fix moved
    the timing of the clear; it never fixed ownership.

Execution options now belong to the admitted message. They are applied at the
boundary of the turn that owns them and lifted back to the value they
displaced when that turn ends. `_dispatch_prompt` no longer touches the
session at all, so there is nothing left for it to null.
"""

from __future__ import annotations

import asyncio

import pytest

from core.extensions import scheduling as sched
from sessions import state_v2 as sv2
from sessions.manager import SessionManager


@pytest.fixture
def mgr(monkeypatch):
    fresh = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", fresh)
    return fresh


def _admission_barrier(mgr, monkeypatch):
    """An event set the moment prompt() has finished deciding."""
    admitted = asyncio.Event()
    real = mgr.prompt

    async def watched(*args, **kwargs):
        result = await real(*args, **kwargs)
        admitted.set()
        return result

    monkeypatch.setattr(mgr, "prompt", watched)
    return admitted


async def test_a_job_firing_into_a_busy_session_leaves_the_live_turn_alone(mgr, monkeypatch):
    sid = mgr.create_session(title="a session the user is using")
    session = mgr.get(sid)
    session.model_override = "user/pinned-model"  # what the API route sets, in memory only

    running = asyncio.Event()
    release = asyncio.Event()
    seen: dict[str, tuple] = {}

    async def runner(session_id, message, session, **kw):
        if message == "what is the weather":
            running.set()
            await release.wait()
            # Read AFTER the job has been admitted: this is the live turn's
            # own view of its model and its tool surface.
            seen["user_turn"] = (session.model_override, session.tool_allowlist)
        else:
            seen["job_turn"] = (session.model_override, session.tool_allowlist)

    mgr.set_agent_runner(runner)

    await mgr.prompt(sid, "what is the weather")
    await running.wait()
    session.last_user_msg_at -= 60  # push the user's message out of the rapid-fire window

    admitted = _admission_barrier(mgr, monkeypatch)
    job = asyncio.create_task(sched._dispatch_prompt(sid, "cron work", model="cron/model", allowed_tools=["file_read"]))
    await admitted.wait()

    assert session.pending_messages, "the job must queue behind the running turn"
    assert session.model_override == "user/pinned-model", "the job must not repin the user's live turn"
    assert session.tool_allowlist is None, "nor hand it a charter it never asked for"

    release.set()
    result = await asyncio.wait_for(job, timeout=10)

    # The job's own turn is the one that carries the job's options.
    assert seen["user_turn"] == ("user/pinned-model", None)
    assert seen["job_turn"] == ("cron/model", frozenset({"file_read"}))
    assert result.completed


async def test_cleanup_restores_the_users_pin_instead_of_nulling_it(mgr):
    sid = mgr.create_session(title="a session the user pinned")
    session = mgr.get(sid)
    session.model_override = "user/pinned-model"

    async def runner(session_id, message, session, **kw):
        assert session.model_override == "cron/model"

    mgr.set_agent_runner(runner)

    result = await sched._dispatch_prompt(sid, "cron work", model="cron/model")
    assert result.completed
    assert session.model_override == "user/pinned-model", "the pin is unrecoverable if this is None"


async def test_two_jobs_into_one_session_do_not_cross_clear(mgr, monkeypatch):
    """The old cleanup closed over the OUTER dispatch's model and tools."""
    sid = mgr.create_session(title="shared by two jobs")
    session = mgr.get(sid)

    started = asyncio.Event()
    release = asyncio.Event()
    seen: dict[str, tuple] = {}

    async def runner(session_id, message, session, **kw):
        seen[message] = (session.model_override, session.tool_allowlist)
        if message == "job A":
            started.set()
            await release.wait()

    mgr.set_agent_runner(runner)

    a = asyncio.create_task(sched._dispatch_prompt(sid, "job A", model="model/A"))
    await started.wait()
    session.last_user_msg_at -= 60

    admitted = _admission_barrier(mgr, monkeypatch)
    b = asyncio.create_task(sched._dispatch_prompt(sid, "job B", allowed_tools=["recall"]))
    await admitted.wait()

    # B is queued. A is running and must still be on A's model.
    assert session.model_override == "model/A"
    assert session.tool_allowlist is None, "B's charter belongs to B's turn"

    release.set()
    assert (await asyncio.wait_for(a, timeout=10)).completed
    assert (await asyncio.wait_for(b, timeout=10)).completed

    assert seen["job A"] == ("model/A", None)
    assert seen["job B"] == (None, frozenset({"recall"}))
    assert session.model_override is None
    assert session.tool_allowlist is None


async def test_a_worker_kinds_confinement_survives_a_cron_pin(mgr):
    """A job pinned to a worker session used to strip the kind's allow-list.

    `_clear` nulled tool_allowlist unconditionally, and the kind's list is
    only re-applied when the session is rehydrated from the DB — so the
    worker ran unconfined for the rest of its life in memory.
    """
    sid = mgr.create_session(title="a worker", session_type="worker")
    session = mgr.get(sid)
    kind_charter = frozenset({"recall", "file_read"})
    session.tool_allowlist = kind_charter

    async def runner(session_id, message, session, **kw):
        assert session.tool_allowlist == frozenset({"bash"}), "the job's charter is exclusive for its turn"

    mgr.set_agent_runner(runner)

    result = await sched._dispatch_prompt(sid, "cron work", allowed_tools=["bash"])
    assert result.completed
    assert session.tool_allowlist == kind_charter, "the kind's confinement must come back"


async def test_a_pin_that_touches_only_the_model_leaves_the_charter_alone(mgr):
    sid = mgr.create_session(title="a worker", session_type="worker")
    session = mgr.get(sid)
    session.tool_allowlist = frozenset({"recall"})

    async def runner(session_id, message, session, **kw):
        assert session.tool_allowlist == frozenset({"recall"}), "a model-only job restricts nothing"
        assert session.model_override == "cron/model"

    mgr.set_agent_runner(runner)
    assert (await sched._dispatch_prompt(sid, "cron work", model="cron/model")).completed
    assert session.tool_allowlist == frozenset({"recall"})
    assert session.model_override is None


async def test_a_model_pin_brings_the_matching_context_budget(monkeypatch):
    """model_override and context_budget_override are documented as a pair.

    Only one of them was ever set, so a pinned run compiled its context
    against the window of whatever model the session used last.
    """
    monkeypatch.setattr("core.llm.budget.derive_model_budget", lambda model: 90000)
    options = sched._build_exec_options(model="big/model")
    assert options.model == "big/model"
    assert options.context_budget == 90000

    monkeypatch.setattr("core.llm.budget.derive_model_budget", lambda model: None)
    assert sched._build_exec_options(model="unknown/model").context_budget is None
    assert sched._build_exec_options() is None, "no options means no per-turn override at all"


async def test_a_repair_tool_is_still_paired_into_the_charter(mgr):
    """remember() names update_memory in its own error text; the pairing stays."""

    sid = mgr.create_session(title="a job that remembers")
    session = mgr.get(sid)
    seen = {}

    async def runner(session_id, message, session, **kw):
        seen["allow"] = session.tool_allowlist

    mgr.set_agent_runner(runner)
    assert (await sched._dispatch_prompt(sid, "note it", allowed_tools=["remember"])).completed
    assert seen["allow"] == frozenset({"remember", "update_memory", "recall"})


async def test_a_rejected_dispatch_never_touches_the_session(mgr):
    sid = mgr.create_session(title="cancelling")
    session = mgr.get(sid)
    session.model_override = "user/pinned-model"
    session._state_v2 = sv2.SessionStateV2.CANCELLING

    result = await sched._dispatch_prompt(sid, "cron work", model="cron/model", allowed_tools=["file_read"])

    assert result.status == sched.DISPATCH_REJECTED
    assert result.error == "cancelling"
    assert session.model_override == "user/pinned-model"
    assert session.tool_allowlist is None
