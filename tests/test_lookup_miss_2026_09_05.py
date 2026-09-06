"""Regression tests for the 2026-09-05 follow-up fixes.

Two candor "degraded" routing hints survived the 09-04 hardening and were
both wrong for reasons the loop could not see:

* read_skill_resource "15% reliable" — every failure was the agent asking for
  a resource path that does not exist. The tool answered correctly. A
  negative lookup now carries MISS_PREFIX: still an error for the agent,
  never a mark against the tool.
* the dream scout-replay judged 15 of 105 hypotheses against a post-mortem
  that PASSED, and promoted 8 of them into policies. A replay can only be
  judged against a failure.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core import synthesis
from core.agent import record_tool_outcome
from core.extensions.candor.emit import build_turn_observations
from core.tools.executor import MISS_MARKER, MISS_PREFIX, ToolExecutionResult, _execute_single, is_miss
from core.tools.registry import ToolRegistry
from db import models as db


def _res(content: str, was_error: bool = True, metadata: dict | None = None) -> ToolExecutionResult:
    return ToolExecutionResult("read_skill_resource", content, was_error, 3, metadata=metadata or {})


# ---------------------------------------------------------------------------
# The marker
# ---------------------------------------------------------------------------


def test_is_miss_reads_the_prefix_or_the_metadata():
    assert is_miss(_res(f"{MISS_PREFIX} Error: Resource 'x' not found in skill 'y'."))
    assert is_miss(_res("Error: whatever", metadata={MISS_MARKER: True}))
    assert not is_miss(_res("Error: connection reset"))
    assert not is_miss(_res("content", was_error=False))


async def test_executor_keeps_a_miss_an_error_for_the_agent_but_not_for_the_tool():
    reg = ToolRegistry()
    reg.register(
        name="lookup_thing",
        func=lambda: f"{MISS_PREFIX} Error: Thing 'q' not found.",
        description="t",
        parameters={"type": "object", "properties": {}},
    )
    res = await _execute_single("lookup_thing", {}, {"session_id": "s1"}, reg)

    assert res.was_error is True, "the agent must still see it as an error and correct course"
    assert is_miss(res)
    assert res.metadata.get(MISS_MARKER) is True
    assert reg.metrics["lookup_thing"].failure_count == 0, "per-tool health untouched"
    assert reg.metrics["lookup_thing"].success_count == 0


def test_record_tool_outcome_counts_misses_apart_from_failures():
    turn = SimpleNamespace(tool_summary={}, tool_summary_attempts=[], reflect_count=0)
    record_tool_outcome(turn, _res(f"{MISS_PREFIX} Error: Resource 'a' not found in skill 's'."))
    record_tool_outcome(turn, _res("Error: disk on fire"))
    record_tool_outcome(turn, _res("ok", was_error=False))

    e = turn.tool_summary["read_skill_resource"]
    assert e["calls"] == 3
    assert e["failures"] == 1, "only the real failure counts"
    assert e["misses"] == 1
    assert e["miss_errors"] and "not found" in e["miss_errors"][0]
    assert e["errors"] == ["Error: disk on fire"]


def test_candor_emit_nets_misses_out_of_the_denominator():
    obs, emitted = build_turn_observations(
        tool_summary={"read_skill_resource": {"calls": 6, "failures": 0, "misses": 4}},
        already_emitted={},
        termination_reason=None,
        reflect_verdict=None,
        failure_cause=None,
        model="m",
        session_kind="normal",
        is_retry=False,
        ts_ms=0,
    )
    per = [o for o in obs if o["pred"] == "tool_ok" and o["args"] == ["read_skill_resource"]]
    assert len(per) == 2, "six calls minus four misses is two observations"
    assert all(o["outcome"] is True for o in per), "and none of them is a failure"
    assert emitted["read_skill_resource"]["calls"] == 2


def _pm(tool_summary: dict) -> dict:
    return {
        "id": "pm-miss",
        "verdict": "pass",
        "failure_cause": "none",
        "confidence": 0.9,
        "execution_mode": "inline",
        "scout_viability": "verified",
        "payload_json": json.dumps({"scout_summary": {"from_fallback": False}, "tool_summary": tool_summary}),
    }


def test_synthesis_ignores_misses_when_blaming_a_tool():
    attrs = [
        a
        for a in synthesis.attribute(_pm({"read_skill_resource": {"calls": 5, "failures": 0, "misses": 4}}))
        if a.signal_type == "tool"
    ]
    assert len(attrs) == 1 and attrs[0].delta_successes == 1 and attrs[0].delta_failures == 0

    only_misses = _pm({"read_skill_resource": {"calls": 4, "failures": 0, "misses": 4}})
    assert [
        a for a in synthesis.attribute(only_misses) if a.signal_type == "tool"
    ] == [], "nothing but misses is no evidence"


def test_skill_tools_mark_negative_lookups(monkeypatch, tmp_path):
    from core.tools.builtin import skill_tools

    monkeypatch.setattr("config.settings.skills_dir", str(tmp_path))
    out = skill_tools.read_skill_resource("no-such-skill", "references/x.md")
    assert out.startswith(MISS_PREFIX) and "not found" in out
    out = skill_tools.load_skill("no-such-skill")
    assert out.startswith(MISS_PREFIX) and "not found" in out


# ---------------------------------------------------------------------------
# Recent-window corroboration data
# ---------------------------------------------------------------------------


def test_recent_tool_outcomes_reads_stamped_rows_and_skips_misses():
    sid = db.create_session(title="t")
    db.add_message(sid, "user", "go")
    for meta in (
        {"was_error": True, "tool": "forget"},
        {"was_error": False, "tool": "forget"},
        {"was_error": True, "tool": "forget", "miss": True},
        {"was_error": True, "tool": "forget", "unavailable": True},
        {"was_error": True, "tool": "bash"},
        {"was_error": True},  # legacy row without a tool name
    ):
        db.add_message(sid, "tool", "x", tool_call_id="c", metadata=json.dumps(meta))

    assert db.recent_tool_outcomes("forget") == {"calls": 2, "failures": 1}
    assert db.recent_tool_outcomes("bash") == {"calls": 1, "failures": 1}
    assert db.recent_tool_outcomes("never") == {"calls": 0, "failures": 0}


# ---------------------------------------------------------------------------
# Dream replay needs a failure to replay against
# ---------------------------------------------------------------------------


async def test_replay_expires_when_the_cited_post_mortem_passed(monkeypatch):
    from core.dream import validate

    finished: list = []
    monkeypatch.setattr(validate, "_evidence", lambda row: [{"type": "pm", "session_id": "s1", "id": "pm1"}])
    monkeypatch.setattr(
        validate.db, "get_post_mortem", lambda pid: {"id": pid, "verdict": "pass", "payload_json": "{}"}
    )
    monkeypatch.setattr(validate.db, "get_messages", lambda sid: [{"role": "user", "content": "do it"}])
    monkeypatch.setattr(
        validate, "_finish", lambda row, status, method, note: finished.append((status, method, note)) or status
    )
    monkeypatch.setattr(validate, "_debit_replay", lambda: pytest.fail("a replay must not be spent on a passing turn"))

    out = await validate._validate_lesson_ineffective({"id": "h1"})

    assert out == "expired"
    assert finished == [("expired", "scout_replay", "cited post-mortem passed — nothing to replay against")]
