"""Two questions to one model were answered once, and nobody was told.

`_is_near_duplicate_call` compared `a_args["images"] == b_args["images"]` for
call_model. The tool has never had an `images` parameter — the live schema is
model / prompt / system / image_path / fallback_model, and `git log -S images`
over core/extensions/model_mgmt is empty. Both sides therefore matched on the
default empty list, and the prompt was ignored on purpose ("regardless of
prompt wording differences"), so the whole comparison reduced to "same model".

One call_model per model per round survived. Describe a.png and transcribe
b.png in the same response and only a.png was described; ask one model two
unrelated questions and only the first was answered. The suppressed call got a
"(near-duplicate call — see previous result)" transcript row, no event and no
recorded failure, and until F05 that row was stripped from the next request —
so the agent saw a result it had not asked for and no sign of the request that
went missing. Two of the three tests covering this certified the phantom key.

Dedup here suppresses work the user asked for, so it now has to be sure: same
model, same image, same system prompt, and the same question once
capitalisation and whitespace runs are normalised away.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from core.agent import _SEMANTIC_DEDUP_TOOLS, StuckDetector, _is_near_duplicate_call, _ToolCallGate
from core.tools.registry import ToolRegistry


def _call(call_id: str, **args) -> dict:
    return {"id": call_id, "name": "call_model", "arguments": json.dumps(args)}


def _registry() -> ToolRegistry:
    from core.extensions.model_mgmt import register

    reg = ToolRegistry()
    register(reg)
    return reg


def _gate(registry, saved: list) -> _ToolCallGate:
    async def _save(role, content, tool_call_id=""):
        saved.append((role, content, tool_call_id))

    return _ToolCallGate(
        registry=registry,
        session=SimpleNamespace(emit_event=lambda payload: None),
        save_turn_msg=_save,
        stuck=StuckDetector(),
        tool_failures={},
    )


# ---------------------------------------------------------------------------
# The comparison matches the tool that actually exists
# ---------------------------------------------------------------------------


def test_the_live_schema_has_image_path_and_no_images():
    """The premise of the bug, asserted so it cannot drift back."""
    schema = _registry().get("call_model").parameters
    assert "image_path" in schema["properties"]
    assert "images" not in schema["properties"]
    assert _SEMANTIC_DEDUP_TOOLS == {"call_model"}


def test_two_images_are_two_pieces_of_work():
    a = _call("m1", model="qwen/qwen3-vl", prompt="What colour is the car?", image_path="shots/car.png")
    b = _call("m2", model="qwen/qwen3-vl", prompt="Transcribe the sign text.", image_path="shots/sign.png")
    assert _is_near_duplicate_call(a, b, "call_model") is False


def test_two_questions_to_one_model_are_two_pieces_of_work():
    a = _call("m1", model="qwen/qwen3-vl", prompt="Summarise this paragraph.")
    b = _call("m2", model="qwen/qwen3-vl", prompt="List the entities in it.")
    assert _is_near_duplicate_call(a, b, "call_model") is False


def test_an_image_call_and_a_text_call_are_not_the_same():
    a = _call("m1", model="qwen/qwen3-vl", prompt="describe", image_path="shots/car.png")
    b = _call("m2", model="qwen/qwen3-vl", prompt="describe")
    assert _is_near_duplicate_call(a, b, "call_model") is False


def test_the_same_question_reworded_only_by_whitespace_is_still_one():
    a = _call("m1", model="qwen/qwen3-vl", prompt="Describe\n  the   CHART", image_path="c.png")
    b = _call("m2", model="qwen/qwen3-vl", prompt="describe the chart", image_path="c.png")
    assert _is_near_duplicate_call(a, b, "call_model") is True


def test_a_different_system_prompt_is_a_different_call():
    a = _call("m1", model="qwen/qwen3-vl", prompt="describe", system="answer in one word")
    b = _call("m2", model="qwen/qwen3-vl", prompt="describe", system="answer exhaustively")
    assert _is_near_duplicate_call(a, b, "call_model") is False


# ---------------------------------------------------------------------------
# Through the real gate
# ---------------------------------------------------------------------------


async def test_the_gate_admits_both_distinct_requests():
    saved: list = []
    gate = _gate(_registry(), saved)
    calls = [
        _call("m1", model="qwen/qwen3-vl", prompt="What colour is the car?", image_path="shots/car.png"),
        _call("m2", model="qwen/qwen3-vl", prompt="Transcribe the sign text.", image_path="shots/sign.png"),
        _call("m3", model="qwen/qwen3-vl", prompt="Summarise this paragraph."),
    ]

    parsed, _notes = await gate.admit(calls, ["call_model"])
    await gate.flush_rejections()

    assert [item["tc"]["id"] for item in parsed] == ["m1", "m2", "m3"]
    assert [c for _r, c, _i in saved if "near-duplicate" in c] == []


async def test_the_gate_still_suppresses_a_genuine_repeat():
    """The dedup that was worth having keeps working."""
    saved: list = []
    gate = _gate(_registry(), saved)
    calls = [
        _call("m1", model="qwen/qwen3-vl", prompt="Describe the chart", image_path="c.png"),
        _call("m2", model="qwen/qwen3-vl", prompt="describe   the CHART", image_path="c.png"),
    ]

    parsed, _notes = await gate.admit(calls, ["call_model"])
    await gate.flush_rejections()

    assert [item["tc"]["id"] for item in parsed] == ["m1"]
    assert [(c, i) for _r, c, i in saved if "near-duplicate" in c] == [
        ("(near-duplicate call — see previous result)", "m2")
    ]
