"""Scout text and a changing tool list made every new turn cold.

The served Qwen template renders tools ahead of the system text, then the
system prompt, which carried each turn's scout report. Measured on vLLM:

- 67k prompt, new turn: 11.9 s with scout in the head, 2.9 s with it moved.
- 50k prompt, scout moved: 2.0 s with the same tools, 9 s with one tool added,
  even at the end of the list. The list changed on about 320 of 523 real turns.
- Scout itself was a median of 14.4 s, 38% of turn time.

Now the scout LLM runs once per session, later turns get a no-LLM memory
search, the tool list is fixed, other tools go through lookup and use_tool,
and per-turn text sits after the turn's user message.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core.agent import StuckDetector, _resolve_tool_surface, _ToolCallGate
from core.context.compiler import compile_context
from core.extensions import load_extensions
from core.scout import runner
from core.scout.report import ScoutReport, SessionBrief
from core.tools.builtin import load_builtin_tools, lookup_tools
from core.tools.native import NATIVE_TOOLS, catalog_names, frozen_surface
from core.tools.registry import ToolRegistry
from db import models as db

TOOLS = [{"type": "function", "function": {"name": n, "parameters": {}}} for n in ("bash", "lookup", "use_tool")]


@pytest.fixture(autouse=True)
def run_once(monkeypatch):
    monkeypatch.setattr("config.settings.scout_first_turn_only", True)


@pytest.fixture
def registry():
    reg = ToolRegistry()
    load_builtin_tools(reg)
    load_extensions(reg)
    return reg


def _session(report=None, allowlist=None, excluded=None):
    return SimpleNamespace(
        last_scout_report=report,
        tool_allowlist=allowlist,
        turn=SimpleNamespace(retry_excluded_tools=excluded or set()),
    )


# --- the tool list ----------------------------------------------------------


def test_every_native_tool_really_exists(registry):
    # These three register only when their feature is on.
    optional = {"goal_complete", "goal_status", "repl"}
    assert NATIVE_TOOLS - optional - {t.name for t in registry.all_tools()} == set()


def test_the_tool_list_ignores_what_scout_picked(registry):
    first = _session(ScoutReport(recommended_tools=["schedule_job", "rlm_process"]))
    later = _session(ScoutReport(recommended_tools=["add_gate"]))

    _, first_tools = _resolve_tool_surface(first, "s", registry)
    _, later_tools = _resolve_tool_surface(later, "s", registry, prior_tool_names={"mcp_add_server"})

    assert first_tools == later_tools == frozen_surface(registry)
    assert "schedule_job" not in first_tools and "bash" in first_tools


def test_a_session_with_no_report_gets_the_same_list(registry):
    text, tools = _resolve_tool_surface(_session(), "s", registry)

    assert text == "" and tools == frozen_surface(registry)


def test_an_allow_list_is_its_own_surface(registry):
    _, tools = _resolve_tool_surface(_session(allowlist=["bash", "schedule_job"]), "s", registry)

    assert tools == ["bash", "schedule_job"]


def test_a_disabled_tool_stays_out(registry):
    registry._disabled.add("grep")

    assert "grep" not in frozen_surface(registry)


def test_retry_exclusions_still_win(registry):
    _, tools = _resolve_tool_surface(_session(excluded={"bash"}), "s", registry)

    assert "bash" not in tools


def test_every_other_tool_is_named_for_the_model(registry):
    names = catalog_names(registry)

    assert "schedule_job" in names and "bash" not in names and names == sorted(names)


# --- the prompt -------------------------------------------------------------


def _chat(turns: int = 2) -> tuple[str, int]:
    sid = db.create_session()
    last = 0
    for i in range(turns):
        last = db.add_message(sid, "user", f"question {i}")
        if i < turns - 1:
            db.add_message(sid, "assistant", f"answer {i}")
    return sid, last


def test_scout_text_leaves_the_head_and_follows_the_turns_user_message():
    sid, user_id = _chat()
    db.add_message(sid, "assistant", "working")

    payload = compile_context(
        sid, tool_schemas=TOOLS, scout_report_text="[APPROACH]\nzqxplan", turn_user_msg_id=user_id
    )

    contents = [str(m["content"]) for m in payload.messages]
    assert "zqxplan" not in contents[0]
    at = next(i for i, c in enumerate(contents) if c.startswith("[TURN BRIEFING"))
    assert contents[at - 1] == "question 1"
    assert contents[at + 1] == "working", "later rounds go behind it, so it keeps its spot"
    assert payload.messages[at]["role"] == "user"


def test_a_new_scout_text_keeps_everything_before_the_turn_identical():
    sid, user_id = _chat()

    first = compile_context(sid, tool_schemas=TOOLS, scout_report_text="plan A", turn_user_msg_id=user_id)
    second = compile_context(sid, tool_schemas=TOOLS, scout_report_text="plan B", turn_user_msg_id=user_id)

    at = next(i for i, m in enumerate(first.messages) if str(m["content"]).startswith("[TURN BRIEFING"))
    assert first.messages[:at] == second.messages[:at]
    assert first.messages[at] != second.messages[at]


def test_the_briefing_is_counted_against_the_budget():
    sid, user_id = _chat()

    small = compile_context(sid, tool_schemas=TOOLS, scout_report_text="x", turn_user_msg_id=user_id)
    large = compile_context(sid, tool_schemas=TOOLS, scout_report_text="word " * 2000, turn_user_msg_id=user_id)

    assert large.metadata.system_tokens > small.metadata.system_tokens + 500


def test_a_compile_with_no_turn_id_still_delivers_the_briefing():
    sid, _ = _chat()

    payload = compile_context(sid, tool_schemas=TOOLS, scout_report_text="plan")

    assert any(str(m["content"]).startswith("[TURN BRIEFING") for m in payload.messages)


def test_help_text_needs_the_helper_tools():
    sid, _ = _chat(1)

    with_help = compile_context(sid, tool_schemas=TOOLS)
    without = compile_context(sid, tool_schemas=TOOLS[:1])

    assert "FINDING HELP" in with_help.messages[0]["content"]
    assert "FINDING HELP" not in without.messages[0]["content"]


def test_turning_it_off_restores_the_head(monkeypatch):
    monkeypatch.setattr("config.settings.scout_first_turn_only", False)
    sid, user_id = _chat()

    payload = compile_context(sid, tool_schemas=TOOLS, scout_report_text="zqxplan", turn_user_msg_id=user_id)

    assert "zqxplan" in payload.messages[0]["content"]
    assert "FINDING HELP" not in payload.messages[0]["content"]


# --- when scout runs --------------------------------------------------------


def test_a_greeting_does_not_spend_the_one_scout():
    assert runner.should_bypass_scout("hi there", 1) is True
    assert runner.should_bypass_scout("build me a todo app please", 2) is False


def test_a_session_with_a_plan_never_scouts_again():
    assert runner.should_bypass_scout("now do something entirely different please", 5, True) is True


def test_only_a_real_plan_uses_up_the_scout():
    sid = db.create_session()
    assert db.session_has_llm_scout(sid) is False

    db.add_message(sid, "scout", json.dumps({"type": "scout.done", "from_fallback": True}))
    assert db.session_has_llm_scout(sid) is False, "a failed scout may run again"

    db.add_message(sid, "scout", json.dumps({"type": "scout.done", "from_fallback": False}))
    assert db.session_has_llm_scout(sid) is True


async def test_a_later_turn_makes_no_llm_call_and_shows_no_degraded_notice(monkeypatch):
    sid = db.create_session()
    db.add_message(sid, "scout", json.dumps({"type": "scout.done", "from_fallback": False}))

    def _no_llm(*a, **k):
        raise AssertionError("the scout LLM ran")

    monkeypatch.setattr(runner, "get_llm_client", _no_llm, raising=False)
    report = await runner.run_scout(
        sid, "a brand new question about something", SessionBrief(session_id=sid, turn_count=2)
    )

    assert report.from_fallback and report.fallback_reason == "no_llm"
    assert report.approach_guidance == ""
    assert "SCOUT STATUS" not in report.to_system_prompt_section()


def test_a_later_turn_keeps_the_graders_strategy(monkeypatch):
    monkeypatch.setattr(runner, "_prior_turn_verdict_block", lambda sid: "[PRIOR TURN GRADED RETRY] strategy: x")

    report = runner._build_fallback_report("q", SessionBrief(session_id="s"), reason="no_llm", session_id="s")

    assert "strategy: x" in report.to_system_prompt_section()


# --- use_tool ---------------------------------------------------------------


def _gate(registry) -> tuple[_ToolCallGate, list]:
    saved: list = []

    async def _save(role, content, tool_call_id=""):
        saved.append((role, content, tool_call_id))

    session = SimpleNamespace(emit_event=lambda e: None)
    gate = _ToolCallGate(
        registry=registry, session=session, save_turn_msg=_save, stuck=StuckDetector(), tool_failures={}
    )
    return gate, saved


def _use(args, call_id="c1") -> dict:
    return {"id": call_id, "name": "use_tool", "arguments": args if isinstance(args, str) else json.dumps(args)}


async def test_use_tool_becomes_the_real_call_but_the_history_keeps_use_tool(registry):
    gate, _ = _gate(registry)
    call = _use({"name": "grep", "arguments": {"pattern": "x"}})

    parsed, _ = await gate.admit([call], sorted(NATIVE_TOOLS))

    assert [p["tc"]["name"] for p in parsed] == ["grep"]
    assert gate.proposed_calls[0]["name"] == "use_tool"


async def test_use_tool_accepts_arguments_sent_as_a_string(registry):
    gate, _ = _gate(registry)

    parsed, _ = await gate.admit([_use({"name": "bash", "arguments": '{"command": "ls"}'})], ["bash"])

    assert parsed[0]["parsed_args"]["command"] == "ls"


async def test_use_tool_goes_through_the_normal_argument_checks(registry):
    gate, saved = _gate(registry)

    parsed, _ = await gate.admit([_use({"name": "bash", "arguments": {}})], ["bash"])
    await gate.flush_rejections()

    assert parsed == []
    assert "missing required parameters" in saved[0][1]
    assert gate.proposed_calls[0].get("_rejected") is True, "the saved call must show it never ran"


@pytest.mark.parametrize(
    "args",
    [{"arguments": {}}, {"name": "use_tool"}, {"name": "bash", "arguments": [1]}, "not json", {"name": ""}],
)
async def test_a_bad_use_tool_call_is_refused_with_a_way_forward(registry, args):
    gate, saved = _gate(registry)

    parsed, _ = await gate.admit([_use(args)], ["bash"])
    await gate.flush_rejections()

    assert parsed == []
    assert saved and saved[0][2] == "c1"
    assert "use_tool" in saved[0][1]


async def test_an_unknown_tool_through_use_tool_gets_the_usual_hint(registry):
    gate, saved = _gate(registry)

    await gate.admit([_use({"name": "no_such_tool"})], ["bash"])
    await gate.flush_rejections()

    assert "does not exist" in saved[0][1]


def test_use_tool_never_runs_anything_itself():
    assert lookup_tools.use_tool("bash", {"command": "rm -rf /"}).startswith("Error")


# --- lookup -----------------------------------------------------------------


def test_lookup_gives_the_schema_of_a_tool_outside_the_list(monkeypatch, registry):
    monkeypatch.setattr("core.tools.registry.get_registry", lambda: registry)
    registry.rebuild_index()

    out = lookup_tools.lookup("schedule a recurring cron job")

    assert 'use_tool name="schedule_job"' in out and '"properties"' in out


def test_lookup_needs_a_query():
    assert lookup_tools.lookup("  ").startswith("Error")


def test_lookup_says_when_nothing_matched(monkeypatch):
    for name in ("_memory_section", "_skill_section"):
        monkeypatch.setattr(lookup_tools, name, lambda *a: "")
    monkeypatch.setattr(lookup_tools, "_tool_section", lambda *a: "")

    assert "does not mean it does not exist" in lookup_tools.lookup("zzz")


def test_a_broken_section_does_not_lose_the_others(monkeypatch):
    def _boom(*a):
        raise RuntimeError("index down")

    monkeypatch.setattr(lookup_tools, "_memory_section", _boom)
    monkeypatch.setattr(lookup_tools, "_skill_section", lambda *a: "- s: a skill")
    monkeypatch.setattr(lookup_tools, "_tool_section", lambda *a: "")

    assert "- s: a skill" in lookup_tools.lookup("anything")


def test_a_canary_cannot_look_up_memory(monkeypatch):
    sid = db.create_session(session_type="canary")

    def _no_recall(*a, **k):
        raise AssertionError("canary reached memory")

    monkeypatch.setattr("core.memory.internal_recall.internal_recall", _no_recall)

    assert lookup_tools._memory_section("answer", sid) == ""
