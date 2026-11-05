"""2026-11-04: the grader failed a web-search turn for results it was never shown.

Session 4c454e7ccb0c, turn 2: "test web search now". search_web returned one
12,350-char result: 8,937 chars of memory notes, the "[!] Strong internal
match" line, then the six Tavily results from offset 9,059. Reflect cut every
tool result to its first 5,000 chars, so it saw memory notes only, and its
deferred verdict was retry: "search_web returned only an INTERNAL KNOWLEDGE
dump … fabricated a 'Tavily — 6 relevant results' claim … cited sources it
never retrieved (machinelearningplus, LangChain forum)". All three were in the
result. The transcript used about 5% of the grader's budget.

Fixed here:
- the cap ladder starts at 40,000, so an ordinary result is shown whole; a
  forced cut keeps the head AND the tail, and its marker names the section
  headers inside the cut;
- tool results carry their tool name, call id and length;
- phantom_evidence_reason gets a third check: a non-pass verdict that
  disputes two or more things the answer cites, found only in the cut text,
  is the grader's blindness and is downgraded;
- a late `reflect` row from the previous turn is no longer part of the graded
  transcript;
- search_web's memory block is capped at entry boundaries (memory stays
  first, the nudge line survives);
- the UI says a deferred grade is observe-only.

The fixture is the real turn, with the memory notes replaced by synthetic
filler of the same length.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import core.reflect as reflect
from core.reflect import ReflectResult, _build_evidence, phantom_evidence_reason
from db import models as db

ROOT = Path(__file__).resolve().parents[2]
FX = json.loads((ROOT / "tests" / "fixtures" / "reflect_4c454e7ccb0c_turn2.json").read_text())
TAVILY = "=== WEB SEARCH RESULTS (Tavily) ==="
SEARCH_RESULT = FX["search_result_memory_filler"] + FX["search_result_tail"]


def _session() -> str:
    sid = db.create_session(title="LangSmith web search test")
    db.add_message(sid, "user", FX["user"])
    db.add_message(sid, "scout", json.dumps({"type": "scout.done"}))
    db.add_message(sid, "assistant", "", tool_calls=json.dumps([FX["search_call"]]))
    db.add_message(
        sid,
        "tool",
        SEARCH_RESULT,
        tool_call_id=FX["search_call_id"],
        metadata=json.dumps({"tool": "search_web"}),
    )
    db.add_message(sid, "assistant", FX["pre_text"], tool_calls=json.dumps([FX["browse_call"]]))
    db.add_message(
        sid,
        "tool",
        FX["browse_result"],
        tool_call_id=FX["browse_call_id"],
        metadata=json.dumps({"tool": "browse_web"}),
    )
    # The previous turn's deferred grade landed mid-turn on the live box.
    db.add_message(sid, "reflect", json.dumps({"verdict": "pass", "reasoning": "PREVIOUS-TURN-GRADE"}))
    db.add_message(sid, "assistant", FX["final"])
    return sid


def _verdict() -> ReflectResult:
    r = FX["reflect"]
    return ReflectResult(
        verdict=r["verdict"],
        reasoning=r["reasoning"],
        diagnostic=r["diagnostic"],
        what_failed=r["what_failed"],
        strategy=r["strategy"],
        missing=r["missing"],
        failure_cause=r["failure_cause"],
        confidence=r["confidence"],
    )


def test_the_fixture_is_the_live_shape():
    assert len(SEARCH_RESULT) == 12_350
    assert SEARCH_RESULT.index(TAVILY) == 9_059
    assert FX["reflect"]["verdict"] == "retry"


def test_the_grader_now_sees_the_web_results():
    sid = _session()
    view: dict = {}
    _, evidence = _build_evidence(sid, attempt=1, tool_view_out=view)

    assert TAVILY in evidence
    assert "machinelearningplus.com" in evidence
    assert "forum.langchain.com" in evidence
    assert f"[TOOL RESULT: search_web, call_id={FX['search_call_id']}, 12350 chars]" in evidence
    assert view["elided"] == "", "nothing was cut: the whole turn fits the budget"


def test_a_late_grade_of_the_previous_turn_is_not_evidence():
    _, evidence = _build_evidence(_session(), attempt=1)
    assert "PREVIOUS-TURN-GRADE" not in evidence


def test_a_forced_cut_keeps_the_tail_and_names_what_it_hid(monkeypatch):
    monkeypatch.setattr(reflect, "_TOOL_RESULT_CAPS", (5000,))
    view: dict = {}
    _, evidence = _build_evidence(_session(), attempt=1, tool_view_out=view)

    assert f'section headers inside the cut: "{TAVILY}"' in evidence
    assert "the result continues below" in evidence
    assert SEARCH_RESULT[-2000:] in evidence, "the tail is shown, not only the head"
    assert TAVILY in view["elided"]


def test_the_live_verdict_is_caught_as_blindness(monkeypatch):
    # Cut hard enough that, like the live grade, none of the Tavily results show.
    monkeypatch.setattr(reflect, "_TOOL_RESULT_CAPS", (1500,))
    view: dict = {}
    _, evidence = _build_evidence(_session(), attempt=1, tool_view_out=view)
    assert "machinelearningplus" not in view["shown"]

    why = phantom_evidence_reason(_verdict(), evidence, "complete", view)

    # "Tavily" itself is no longer hidden: the cut marker names the Tavily
    # section, so the grader was told it exists.
    assert why and "machinelearningplus" in why


def test_a_grader_that_saw_the_results_keeps_its_verdict():
    view: dict = {}
    _, evidence = _build_evidence(_session(), attempt=1, tool_view_out=view)
    assert phantom_evidence_reason(_verdict(), evidence, "complete", view) is None


def test_one_hidden_token_is_not_enough():
    r = _verdict()
    view = {"shown": "memory only", "elided": "see Tavily here", "final": "Tavily works"}
    r.reasoning, r.diagnostic, r.what_failed = "the Tavily claim is unsupported", "", ""
    assert phantom_evidence_reason(r, "evidence", "complete", view) is None


@pytest.mark.parametrize("verdict", ["pass"])
def test_a_pass_is_never_touched(verdict):
    r = _verdict()
    r.verdict = verdict
    view = {"shown": "", "elided": SEARCH_RESULT, "final": FX["final"]}
    assert phantom_evidence_reason(r, "evidence", "complete", view) is None


def test_the_call_site_hands_the_guard_its_view():
    src = (ROOT / "core" / "reflect.py").read_text()
    assert "phantom_evidence_reason(result, evidence, termination_reason, tool_view)" in src
    assert "tool_view_out=tool_view," in src


def test_the_rubric_says_a_cut_result_continued():
    assert "means the tool result CONTINUED" in reflect.REFLECT_PROMPT


# ── search_web's memory block ────────────────────────────────────────────────


def _recall(memory_entries: int, sessions: int = 0):
    from core.memory.internal_recall import InternalRecall

    r = InternalRecall()
    r.memory_text = "\n\n".join(f"[pernix.note{i} score=2.0] " + "m" * 380 for i in range(memory_entries))
    r.session_text = "\n".join(f"  [user] prior hit {i} " + "s" * 200 for i in range(sessions))
    r.memory_strong = True
    return r


def test_the_memory_block_is_capped_at_entry_boundaries_and_keeps_the_nudge():
    from core.memory.internal_recall import format_for_tool_output

    out = format_for_tool_output(_recall(8, sessions=6), max_chars=1800)

    assert len(out) < 2400
    assert out.startswith("=== INTERNAL KNOWLEDGE")
    assert "[!] Strong internal match" in out
    assert "left out; recall() shows them]" in out
    assert out.count("[pernix.note") >= 2
    for chunk in out.split("\n\n"):
        if chunk.startswith("[pernix.note"):
            assert chunk.endswith("m"), "an entry is kept whole or not at all"


def test_uncapped_callers_are_unchanged():
    from core.memory.internal_recall import format_for_tool_output

    out = format_for_tool_output(_recall(8))
    assert out.count("[pernix.note") == 8 and "left out" not in out


def test_search_web_puts_its_results_near_the_top(monkeypatch):
    import core.extensions.web as web
    import core.memory.internal_recall as ir

    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    monkeypatch.setattr(web, "_tavily_search", lambda *a, **kw: "STUB-WEB-RESULT")
    monkeypatch.setattr(web, "_emit_backend_alert", lambda *a, **kw: None)
    monkeypatch.setattr(ir, "internal_recall", lambda *a, **kw: _recall(8, sessions=6))

    out = web.search_web("langsmith setup")

    assert out.index("INTERNAL KNOWLEDGE") < out.index("WEB SEARCH RESULTS"), "memory stays first"
    assert out.index("STUB-WEB-RESULT") < 2600


def test_the_ui_says_a_deferred_grade_is_observe_only():
    app = (ROOT / "static" / "js" / "app.js").read_text()
    body = app[app.index("function renderReflectCard(") :]
    body = body[: body.index("\nfunction ", 10)]
    assert "event.reflect_mode === 'deferred'" in body
    assert "graded after the turn, observe only" in body
    assert "nothing was retried" in body
