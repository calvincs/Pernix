"""Live grades 2026-11-01: claims of work with no tool call, and lost distills."""

from core.agent import _claims_unbacked_action
from core.memory.distill import _parse_entries


def test_a_claim_with_no_tool_call_is_caught():
    assert _claims_unbacked_action("Reran the tests, all green.", did_tool_calls=False)
    assert _claims_unbacked_action("I've fixed the file and saved it.", did_tool_calls=False)
    assert _claims_unbacked_action("Already handled.\n\nRan it just now this turn.", did_tool_calls=False)


def test_a_claim_after_real_tool_calls_is_left_alone():
    assert not _claims_unbacked_action("Reran the tests, all green.", did_tool_calls=True)


def test_ordinary_answers_are_not_claims():
    for text in (
        "Paris.",
        "Doing well, thanks.",
        "SCHD has the lower fee.",
        "The bug was fixed in 2019 by the vendor.",
    ):
        assert not _claims_unbacked_action(text, did_tool_calls=False)


def test_entries_survive_a_sentence_before_the_json():
    assert _parse_entries('Here you go:\n[{"file": "a", "content": "b"}]') == [{"file": "a", "content": "b"}]


def test_unreadable_output_gives_no_entries():
    assert _parse_entries("") == []
    assert _parse_entries("I'll skip this one.") == []
