"""Scheduled-job tool allow-list (E1, field case 0ba19fdbc823).

A cron job's prompt used to be the only thing standing between the agent and
the tools its charter forbade — and the schema builder force-adds every
builtin, so `bash` was offered on every round of a "discovery-only" run. The
`allowed_tools` field on a job entry now binds mechanically at the same two
points as reflect's retry-strip: the schema builder intersects the active set
with it, and the executor refuses anything outside it.
"""

import asyncio

from core.agent import _resolve_tool_surface
from core.tools.executor import _execute_single
from core.tools.registry import ToolRegistry
from sessions.state import AgentSession


def _make_registry(tools: dict) -> ToolRegistry:
    reg = ToolRegistry()
    for name, fn in tools.items():
        reg.register(
            name=name,
            func=fn,
            description=f"{name} tool",
            parameters={"type": "object", "properties": {}},
        )
    return reg


def _fake_manager(monkeypatch, session):
    monkeypatch.setattr(
        "sessions.manager.get_manager",
        lambda: type("M", (), {"get": lambda self, s: session})(),
    )


# ---------------------------------------------------------------------------
# Executor backstop
# ---------------------------------------------------------------------------


async def test_executor_refuses_tool_outside_allowlist(monkeypatch):
    reg = _make_registry({"bash": lambda: "ran", "recall": lambda: "found"})
    session = AgentSession(session_id="cron-test")
    session.tool_allowlist = frozenset({"recall", "file_read"})
    _fake_manager(monkeypatch, session)

    result = await _execute_single("bash", {}, {"session_id": "cron-test"}, reg)
    assert result.was_error
    assert "not permitted in this scheduled run" in result.content
    # The permitted set is named so the model can reroute instead of retrying.
    assert "recall" in result.content


async def test_executor_allowlist_error_is_distinct_from_retry_strip(monkeypatch):
    """Reflect verdicts must be able to tell a charter refusal from a
    retry-strip refusal — the messages carry different phrases."""
    reg = _make_registry({"bash": lambda: "ran"})
    session = AgentSession(session_id="cron-test")
    session.tool_allowlist = frozenset({"recall"})
    _fake_manager(monkeypatch, session)

    result = await _execute_single("bash", {}, {"session_id": "cron-test"}, reg)
    assert "retry attempt" not in result.content


async def test_executor_allows_tool_inside_allowlist(monkeypatch):
    reg = _make_registry({"recall": lambda: "found"})
    session = AgentSession(session_id="cron-test")
    session.tool_allowlist = frozenset({"recall"})
    _fake_manager(monkeypatch, session)

    result = await _execute_single("recall", {}, {"session_id": "cron-test"}, reg)
    assert not result.was_error
    assert result.content == "found"


async def test_executor_unconstrained_without_allowlist(monkeypatch):
    reg = _make_registry({"bash": lambda: "ran"})
    session = AgentSession(session_id="cron-test")
    _fake_manager(monkeypatch, session)

    result = await _execute_single("bash", {}, {"session_id": "cron-test"}, reg)
    assert not result.was_error


# ---------------------------------------------------------------------------
# Schema builder intersection
# ---------------------------------------------------------------------------


def test_schema_intersects_allowlist_over_builtin_force_add():
    """Builtins outside the allow-list must vanish from the schema — the
    force-add is exactly what kept offering bash to the curiosity drive."""
    reg = _make_registry({"bash": lambda: "x", "recall": lambda: "x", "telos_status": lambda: "x"})
    for t in reg.enabled_tools():
        t.source = "builtin"

    session = AgentSession(session_id="cron-test")
    session.last_scout_report = None
    session.tool_allowlist = frozenset({"recall", "telos_status"})

    _, names = _resolve_tool_surface(session, "cron-test", reg)
    assert "bash" not in names
    assert set(names) == {"recall", "telos_status"}


def test_schema_unconstrained_without_allowlist():
    reg = _make_registry({"bash": lambda: "x", "recall": lambda: "x"})
    session = AgentSession(session_id="cron-test")
    session.last_scout_report = None

    _, names = _resolve_tool_surface(session, "cron-test", reg)
    assert set(names) == {"bash", "recall"}


# ---------------------------------------------------------------------------
# Dispatch set/clear
# ---------------------------------------------------------------------------


async def test_dispatch_sets_and_clears_allowlist(monkeypatch):
    from core.extensions import scheduling

    session = AgentSession(session_id="cron-test")
    seen = {}

    class _Manager:
        def get(self, sid):
            return session

        async def prompt(self, sid, prompt):
            seen["during"] = session.tool_allowlist

    monkeypatch.setattr("sessions.manager.get_manager", lambda: _Manager())

    await scheduling._dispatch_prompt("cron-test", "go", allowed_tools=["recall", "file_read"])
    assert seen["during"] == frozenset({"recall", "file_read"})
    # A reused session must not stay constrained after the job's turn.
    assert session.tool_allowlist is None


async def test_dispatch_clears_allowlist_on_prompt_failure(monkeypatch):
    from core.extensions import scheduling

    session = AgentSession(session_id="cron-test")

    class _Manager:
        def get(self, sid):
            return session

        async def prompt(self, sid, prompt):
            raise RuntimeError("boom")

    monkeypatch.setattr("sessions.manager.get_manager", lambda: _Manager())

    try:
        await scheduling._dispatch_prompt("cron-test", "go", allowed_tools=["recall"])
    except RuntimeError:
        pass
    assert session.tool_allowlist is None


async def test_dispatch_leaves_allowlist_untouched_when_job_has_none(monkeypatch):
    from core.extensions import scheduling

    session = AgentSession(session_id="cron-test")

    class _Manager:
        def get(self, sid):
            return session

        async def prompt(self, sid, prompt):
            pass

    monkeypatch.setattr("sessions.manager.get_manager", lambda: _Manager())

    await scheduling._dispatch_prompt("cron-test", "go")
    assert session.tool_allowlist is None
