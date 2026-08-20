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


# ---------------------------------------------------------------------------
# C1: scout constraint awareness
# ---------------------------------------------------------------------------


def test_session_brief_renders_constraint_block():
    from core.scout.report import SessionBrief

    brief = SessionBrief(session_id="s", tool_allowlist=["recall", "telos_status"])
    text = brief.to_prompt_text()
    assert "CONSTRAINED SESSION" in text
    assert "recall, telos_status" in text
    assert "5-15 guidance does not apply" in text


def test_session_brief_unconstrained_has_no_block():
    from core.scout.report import SessionBrief

    assert "CONSTRAINED SESSION" not in SessionBrief(session_id="s").to_prompt_text()


def test_build_session_brief_reads_live_allowlist(monkeypatch):
    from core.scout.runner import build_session_brief
    from db import models as db

    sid = db.create_session()
    live = AgentSession(session_id=sid)
    live.tool_allowlist = frozenset({"recall", "file_read"})
    _fake_manager(monkeypatch, live)

    brief = build_session_brief(sid)
    assert brief.tool_allowlist == ["file_read", "recall"]


# ---------------------------------------------------------------------------
# C2: per-attempt tool summary in reflect evidence
# ---------------------------------------------------------------------------


def _evidence(attempt, tool_summary, attempts_list):
    from core.reflect import _build_compact_evidence

    messages = [
        {"id": 1, "role": "user", "content": "do the thing"},
        {"id": 2, "role": "assistant", "content": "done"},
    ]
    return _build_compact_evidence(
        "sid-c2",
        "do the thing",
        messages,
        attempt,
        tool_summary,
        None,
        tool_summary_attempts=attempts_list,
    )


def test_retry_evidence_carries_current_attempt_section():
    cumulative = {
        "bash": {"calls": 7, "failures": 7, "errors": [], "total_latency_ms": 100},
        "recall": {"calls": 3, "failures": 0, "errors": [], "total_latency_ms": 50},
    }
    attempts = [
        {"bash": {"calls": 7, "failures": 7}, "recall": {"calls": 1, "failures": 0}},
        {"recall": {"calls": 2, "failures": 0}},
    ]
    evidence = _evidence(2, cumulative, attempts)
    assert "cumulative across ALL attempts" in evidence
    assert "CURRENT ATTEMPT (#2) TOOL CALLS" in evidence
    # The current-attempt section must not inherit attempt 1's bash calls.
    section = evidence.split("CURRENT ATTEMPT (#2) TOOL CALLS")[1].split("USER REQUEST")[0]
    assert "bash" not in section
    assert "recall: 2 call(s)" in section


def test_first_attempt_evidence_has_no_per_attempt_section():
    cumulative = {"recall": {"calls": 1, "failures": 0, "errors": [], "total_latency_ms": 10}}
    evidence = _evidence(1, cumulative, [{"recall": {"calls": 1, "failures": 0}}])
    assert "CURRENT ATTEMPT" not in evidence
    assert "cumulative across ALL attempts" not in evidence


def test_retry_evidence_tolerates_missing_attempt_data():
    """Pre-C2 callers pass no attempts list — the section is simply absent."""
    cumulative = {"recall": {"calls": 5, "failures": 0, "errors": [], "total_latency_ms": 10}}
    evidence = _evidence(3, cumulative, None)
    assert "CURRENT ATTEMPT" not in evidence
    assert "cumulative across ALL attempts" in evidence


async def test_dispatch_allowlist_survives_fire_and_forget_prompt(monkeypatch):
    """Regression: manager.prompt() returns at task CREATION, not turn end.
    Clearing the allow-list right after prompt() returned unconstrained every
    real scheduled run (runs ecfd3f89c219/404eaba3c8d9 called file_edit
    straight through E1). The dispatch must wait for the turn task."""
    from core.extensions import scheduling

    session = AgentSession(session_id="cron-test")
    seen = {}

    async def _turn():
        # The schema builder runs well after prompt() has returned.
        await asyncio.sleep(0.05)
        seen["during_turn"] = session.tool_allowlist

    class _Manager:
        def get(self, sid):
            return session

        async def prompt(self, sid, prompt):
            session.task = asyncio.get_running_loop().create_task(_turn())
            # returns immediately — the turn is still running

    monkeypatch.setattr("sessions.manager.get_manager", lambda: _Manager())

    await scheduling._dispatch_prompt("cron-test", "go", allowed_tools=["recall"])
    assert seen["during_turn"] == frozenset({"recall"}), "allow-list was cleared before the turn ran"
    assert session.tool_allowlist is None


def test_update_scheduled_job_preserves_extra_meta(monkeypatch, tmp_path):
    """Regression: update_scheduled_job re-added the job without extra_meta,
    stripping allowed_tools/last_fired_at/session_mode from any job it touched."""
    import json as _json

    from core.extensions import scheduling

    cron_path = tmp_path / "cron_jobs.json"
    cron_path.write_text(
        _json.dumps(
            [
                {
                    "name": "j1",
                    "cron_expr": "0 3 * * 2",
                    "prompt": "old",
                    "model": "",
                    "session_id": None,
                    "session_mode": "fresh",
                    "paused": False,
                    "allowed_tools": ["recall", "bash"],
                    "last_fired_at": "2026-08-18T00:00:00+00:00",
                }
            ]
        )
    )
    monkeypatch.setattr(scheduling, "CRON_PATH", cron_path)

    captured = {}

    def _fake_add(name, cron, prompt, session_id=None, model="", extra_meta=None):
        captured["extra_meta"] = extra_meta or {}

    monkeypatch.setattr(scheduling, "_add_job_internal", _fake_add)
    monkeypatch.setattr(scheduling, "_get_scheduler", lambda: object())
    monkeypatch.setattr(scheduling, "_save_jobs", lambda: None)

    result = scheduling.update_scheduled_job("j1", cron_expr="0 4 * * 3")
    assert "updated" in result
    assert captured["extra_meta"].get("allowed_tools") == ["recall", "bash"]
    assert captured["extra_meta"].get("last_fired_at") == "2026-08-18T00:00:00+00:00"
