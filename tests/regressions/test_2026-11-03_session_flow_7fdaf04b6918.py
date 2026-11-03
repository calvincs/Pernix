"""Flow defects found on live session 7fdaf04b6918 (transcript → LinkedIn post → "good job").

The turns themselves ran cleanly. What went wrong was around them:

- Reflect filed turn 1 (all Qwen) under the paid model, because the deferred
  grade read the session's model five minutes later, after the user switched.
- Refine read tool calls in the nested OpenAI shape, but Pernix stores them
  flat, so it never saw load_skill, its tool summary was always empty, and it
  credited turn 2 to the wrong skill. Its one proposal recommended
  list_transcripts(), which the session had only called in a failed call.
- Distill ran four times and saved nothing: twice on a delta of only a grade
  row, once on "good job, well odne".
- The scout injected the skill cut at 5,000 characters with a "load the rest"
  note, so the agent's first round loaded it again.
- No-LLM turns were labelled "scout unavailable" with viability "pending".
"""

from __future__ import annotations

import asyncio
import json

import pytest

from core.acks import is_acknowledgement, trivial_delta_reason
from db import models as db


def _call(cid: str, tool: str, **args) -> dict:
    # The stored shape: flat, arguments as a JSON string.
    return {"id": cid, "name": tool, "arguments": json.dumps(args)}


def _assistant(sid: str, calls: list[dict], *, model: str, parent: int, content: str = "") -> int:
    meta = json.dumps({"model": model, "parent_user_msg_id": parent})
    return db.add_message(sid, "assistant", content, tool_calls=json.dumps(calls) if calls else None, metadata=meta)


# --- reflect attribution ----------------------------------------------------


def test_a_deferred_grade_names_the_model_that_ran_the_turn(monkeypatch):
    from types import SimpleNamespace

    from core.reflect import ReflectResult, _write_post_mortem

    sid = db.create_session()
    u1 = db.add_message(sid, "user", "get transcript of this video")
    _assistant(sid, [], model="Qwen/Qwen3.8-27B", parent=u1, content="Got the transcript.")
    # The user switched models after the turn, before the deferred grade.
    live = SimpleNamespace(model_override="openai/gpt-5.6-luna-pro")
    monkeypatch.setattr("sessions.manager.get_manager", lambda: SimpleNamespace(get=lambda _: live))

    _write_post_mortem(sid, 1, ReflectResult(verdict="pass"), None, {}, turn_user_msg_id=u1)

    pm = db.list_post_mortems(session_id=sid)[0]
    assert json.loads(pm["payload_json"])["agent_model"] == "Qwen/Qwen3.8-27B"


def test_a_turn_with_no_reply_falls_back_to_the_live_model(monkeypatch):
    from types import SimpleNamespace

    from core.reflect import ReflectResult, _write_post_mortem

    sid = db.create_session()
    u1 = db.add_message(sid, "user", "do the thing")
    live = SimpleNamespace(model_override="some/model")
    monkeypatch.setattr("sessions.manager.get_manager", lambda: SimpleNamespace(get=lambda _: live))

    _write_post_mortem(sid, 1, ReflectResult(verdict="pass"), None, {}, turn_user_msg_id=u1)

    pm = db.list_post_mortems(session_id=sid)[0]
    assert json.loads(pm["payload_json"])["agent_model"] == "some/model"


# --- acknowledgements --------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["good job", "thanks!", "Thank you so much", "nice work, thank you", "perfect", "great, thanks", "love it"],
)
def test_thanks_and_praise_are_acknowledgements(text):
    assert is_acknowledgement(text)


@pytest.mark.parametrize(
    "text",
    [
        "good job, well odne",  # a typo is not filler: distill as usual
        "thanks, remember I prefer tabs",
        "well, that broke",
        "good, delete it",
        "ok",
        "yes go ahead",
        "sure",
        "thanks, now deploy it",
        "great, but use port 8091",
        "nice, what about the other one?",
        "thanks https://example.com",
        "perfect, see notes.md",
        "thanks for the 3 files",
        "",
    ],
)
def test_anything_with_content_is_not_an_acknowledgement(text):
    assert not is_acknowledgement(text)


def test_an_attachment_is_never_an_acknowledgement():
    assert not is_acknowledgement("thanks", has_attachments=True)


def test_a_grade_only_delta_is_trivial():
    assert trivial_delta_reason([{"id": 9, "role": "reflect", "content": '{"verdict": "pass"}'}])


def test_a_thanks_turn_with_a_short_reply_is_trivial():
    fresh = [
        {"id": 1, "role": "user", "content": "thanks!"},
        {"id": 2, "role": "assistant", "content": "Thanks — glad it landed well."},
    ]
    assert trivial_delta_reason(fresh) == "acknowledgement turn"


def test_a_thanks_turn_that_did_tool_work_is_not_trivial():
    fresh = [
        {"id": 1, "role": "user", "content": "thanks!"},
        {"id": 2, "role": "assistant", "content": ""},
        {"id": 3, "role": "tool", "content": "SAVED file=x epoch=1"},
    ]
    assert trivial_delta_reason(fresh) == ""


def test_a_real_request_is_not_trivial():
    fresh = [{"id": 1, "role": "user", "content": "use our linkedin skill for this"}]
    assert trivial_delta_reason(fresh) == ""


# --- distill -----------------------------------------------------------------


def test_distill_leaves_a_grade_only_delta_for_the_next_turn(monkeypatch):
    from core.memory import distill

    sid = db.create_session()
    db.add_message(sid, "reflect", '{"verdict": "pass", "reasoning": "' + "x" * 300 + '"}')

    def _no_llm():
        raise AssertionError("distill called the LLM on a grade row")

    monkeypatch.setattr("core.llm.client.get_llm_client", _no_llm)
    monkeypatch.setattr("core.memory.store.get_memory_store", lambda: object())

    asyncio.run(distill.distill_session(session_id=sid, title="t", messages=db.get_messages(sid)))

    assert db.get_distill_watermark(sid) == 0


def test_distill_skips_a_thanks_turn_without_an_llm_call(monkeypatch):
    from core.memory import distill

    sid = db.create_session()
    db.add_message(sid, "user", "good job")
    last = db.add_message(sid, "assistant", "Thanks — glad it landed well.")

    def _no_llm():
        raise AssertionError("distill called the LLM on a thanks turn")

    monkeypatch.setattr("core.llm.client.get_llm_client", _no_llm)
    monkeypatch.setattr("core.memory.store.get_memory_store", lambda: object())

    asyncio.run(distill.distill_session(session_id=sid, title="t", messages=db.get_messages(sid)))

    assert db.get_distill_watermark(sid) == last


# --- refine ------------------------------------------------------------------


def _transcript_session() -> tuple[str, list[dict]]:
    """The shape of turn 1: a failed call, then the calls that worked."""
    sid = db.create_session()
    u = db.add_message(sid, "user", "get transcript of this video")
    q = "Qwen/Qwen3.8-27B"
    _assistant(sid, [_call("c0", "load_skill", name="youtube-whisper")], model=q, parent=u)
    db.add_message(sid, "tool", "## How to run", tool_call_id="c0")
    bad = "tl = yta.YouTubeTranscriptApi.list_transcripts('qvOxAbC3d0U')"
    _assistant(sid, [_call("c1", "repl", code=bad)], model=q, parent=u)
    db.add_message(
        sid,
        "tool",
        "  File \"<cell>\", line 3\nAttributeError: type object 'YouTubeTranscriptApi' has no attribute 'list_transcripts'",
        tool_call_id="c1",
    )
    good = "api = yta.YouTubeTranscriptApi()\ntl = api.list('qvOxAbC3d0U')\ntr = tl.find_transcript(['en']).fetch()"
    _assistant(sid, [_call("c2", "repl", code=good)], model=q, parent=u)
    db.add_message(sid, "tool", "en True 90", tool_call_id="c2")
    _assistant(sid, [], model=q, parent=u, content="Got the transcript from the captions.")
    return sid, db.get_messages(sid)


def test_refine_reads_flat_tool_calls():
    from core.refine import _build_tool_summary, _identify_active_skill

    _, messages = _transcript_session()

    assert _identify_active_skill(messages) == "youtube-whisper"
    summary = _build_tool_summary(messages)
    assert summary["load_skill"]["calls"] == 1
    assert summary["repl"] == {"calls": 2, "failures": 1}


@pytest.mark.parametrize(
    "result",
    [
        "FAILED: AttributeError type object 'YouTubeTranscriptApi' has no attribute 'get_transcript'",
        "  File \"<cell>\", line 4, in <module>\nAttributeError: 'str' object has no attribute 'x'",
    ],
)
def test_repl_exceptions_count_as_failures(result):
    from core.refine import _is_failure_content

    assert _is_failure_content(result)


def test_refine_only_counts_calls_that_worked():
    from core.refine import _successful_code

    _, messages = _transcript_session()

    worked = _successful_code(messages)
    assert len(worked) == 1 and "api.list(" in worked[0]


def test_a_proposal_calling_a_failed_api_is_flagged():
    from core.refine import _successful_code, _ungrounded_calls

    _, messages = _transcript_session()
    worked = _successful_code(messages)
    wrong = "```python\ntl = YouTubeTranscriptApi.list_transcripts(video_id)\n```"
    right = "```python\napi = YouTubeTranscriptApi()\nfetched = api.list(video_id)\n```"

    assert _ungrounded_calls(wrong, worked) == ["list_transcripts"]
    assert _ungrounded_calls(right, worked) == []
    assert _ungrounded_calls("Prose only, no code.", worked) == []
    # Keywords before "(" and helpers the block defines are not calls.
    shaped = "```python\ndef ts(sec):\n    return (sec)\nif (api.list(v)):\n    print(ts(1))\n```"
    assert _ungrounded_calls(shaped, worked) == []


def test_a_log_that_mentions_an_error_is_not_a_failure():
    from core.refine import _is_failure_content

    assert not _is_failure_content("[exit: 0]\nValueError: seen in yesterday's log\nall good")


def test_refine_keeps_the_prose_and_drops_ungrounded_code(monkeypatch):
    from types import SimpleNamespace

    from core import refine

    sid, _ = _transcript_session()
    proposal = {
        "skill_name": "youtube-whisper",
        "section": "How to run",
        "problem": "The caption snippet uses a removed API.",
        "proposed_change": "Use the v1.x instance API.\n```python\nYouTubeTranscriptApi.list_transcripts(v)\n```",
        "confidence": 0.9,
    }

    class _Client:
        async def chat(self, **_):
            return SimpleNamespace(content=json.dumps({"nothing_actionable": False, "proposals": [proposal]}))

    monkeypatch.setattr("core.llm.client.get_llm_client", lambda: _Client())
    monkeypatch.setattr(refine.settings, "background_model", "test/model")
    fake_reg = SimpleNamespace(
        exists=lambda n: True, load_instructions=lambda n: "## How to run\nstuff", all_skills=lambda: []
    )
    monkeypatch.setattr("core.skills.registry.get_skill_registry", lambda: fake_reg)

    stats = asyncio.run(refine.run_for_session(sid))

    assert stats.get("proposals_ungrounded") == 1, stats
    saved = db.list_skill_proposals(skill_name="youtube-whisper", limit=5)
    assert len(saved) == 1
    assert "list_transcripts" not in saved[0]["proposed_change"]
    assert "v1.x instance API" in saved[0]["proposed_change"]
    assert float(saved[0]["confidence"]) <= 0.5


def test_refine_skips_growth_that_is_only_a_thanks(monkeypatch):
    from core import refine

    sid, messages = _transcript_session()
    db.set_snooze_state(f"refined:{sid}", str(messages[-1]["id"]))
    db.add_message(sid, "user", "thanks!")
    db.add_message(sid, "assistant", "Glad it helped.")

    def _no_llm():
        raise AssertionError("refine called the LLM for a thanks")

    monkeypatch.setattr("core.llm.client.get_llm_client", _no_llm)

    assert asyncio.run(refine.run_for_session(sid))["skipped_reason"] == "trivial_delta"


# --- scout -------------------------------------------------------------------


def test_run_once_mode_points_at_the_skill_instead_of_injecting_it():
    from core.scout.report import ScoutReport

    report = ScoutReport(injected_skill_name="youtube-whisper", injected_skill="")
    text = report.to_system_prompt_section()

    assert 'load_skill("youtube-whisper")' in text
    assert "[ACTIVE SKILL" not in text


def test_the_briefing_names_tools_outside_the_fixed_list():
    from core.scout.report import ScoutReport

    text = ScoutReport(extra_tools=["ingest"]).to_system_prompt_section()

    assert "[TOOLS OUTSIDE YOUR LIST]" in text and "ingest" in text and "use_tool" in text


def test_scout_extra_tools_exclude_native_tools(monkeypatch):
    from types import SimpleNamespace

    from core.scout import runner
    from core.scout.report import ScoutReport

    monkeypatch.setattr(runner.settings, "scout_first_turn_only", True)
    fake = SimpleNamespace(exists=lambda n: True, is_disabled=lambda n: False)
    monkeypatch.setattr("core.tools.registry.get_registry", lambda: fake)
    report = runner._validate_report(ScoutReport(recommended_tools=["bash", "ingest"]))

    assert report.extra_tools == ["ingest"]


def test_run_once_scout_prompt_asks_for_tools_outside_the_list(monkeypatch):
    from core.scout import runner

    monkeypatch.setattr(runner.settings, "scout_first_turn_only", True)
    assert "NOT in the agent's fixed tool list" in runner._scout_base_prompt(1)
    monkeypatch.setattr(runner.settings, "scout_first_turn_only", False)
    assert "(5-15 tools)" in runner._scout_base_prompt(1)


def test_a_no_llm_report_is_labelled_by_design(monkeypatch):
    from core.scout import runner
    from core.scout.report import SessionBrief

    monkeypatch.setattr(runner, "memory_recall_denied", lambda _b: True)
    report = runner._build_fallback_report("next", SessionBrief(session_id="s"), reason="no_llm", session_id="s")

    assert report.viability == "inherited"
    assert "unavailable" not in report.tool_rationale


def test_a_full_skill_raises_a_saturation_notice(monkeypatch):
    from core.skills import proposals

    sent = []
    monkeypatch.setattr("core.notices.notify", lambda cat, **kw: sent.append((cat, kw)))
    proposals._notify_saturated("youtube-whisper", "skip:would push the skill past the 5000-char prompt limit")

    assert sent and sent[0][0] == "skills.saturated" and sent[0][1]["subject"] == "youtube-whisper"
