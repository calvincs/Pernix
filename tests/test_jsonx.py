"""core.llm.jsonx — the shared background-model JSON extractor.

One extractor instead of three ad-hoc fence-strippers (memory file-split,
dream hypothesize, telos soup). The cases here are the shapes the live box
actually produced from the qwen3.8 MTP tag on 2026-08-14/15.
"""

from __future__ import annotations

from core.llm.jsonx import extract_json


def test_plain_object():
    assert extract_json('{"groups": [{"file": "a", "entries": [1]}]}') == {"groups": [{"file": "a", "entries": [1]}]}


def test_plain_array():
    assert extract_json('[{"kind": "contradiction"}]') == [{"kind": "contradiction"}]


def test_closed_fence_with_json_tag():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_closed_fence_without_tag():
    assert extract_json("```\n[1, 2, 3]\n```") == [1, 2, 3]


def test_prose_preamble_before_fence():
    text = 'Here is the grouping you asked for:\n```json\n{"groups": []}\n```\nHope that helps!'
    assert extract_json(text) == {"groups": []}


def test_object_embedded_in_prose_without_fence():
    text = 'Sure! The result is {"a": [1, 2], "b": "x"} as requested.'
    assert extract_json(text) == {"a": [1, 2], "b": "x"}


def test_unclosed_fence_from_truncated_output_still_yields_complete_json():
    # The model opened a fence, emitted complete JSON, then got cut before
    # the closing fence — the payload itself is intact and must survive.
    assert extract_json('```json\n{"groups": [{"file": "a", "entries": [0]}]}') == {
        "groups": [{"file": "a", "entries": [0]}]
    }


def test_think_block_is_stripped():
    text = '<think>{"decoy": true} reasoning about brackets ]</think>{"real": 1}'
    assert extract_json(text) == {"real": 1}


def test_brackets_inside_json_strings_do_not_confuse_the_scanner():
    text = 'Note: {"msg": "a } tricky ] string with \\" escape", "n": 1} done'
    assert extract_json(text) == {"msg": 'a } tricky ] string with " escape', "n": 1}


def test_truncated_output_is_none_not_garbage():
    # The live failure shape: MTP early-stop mid-generation.
    assert extract_json("[") is None
    assert extract_json('[{"kind":"tool_pattern","statement":"The') is None


def test_empty_and_none_are_none():
    assert extract_json("") is None
    assert extract_json(None) is None
    assert extract_json("no json here at all") is None
