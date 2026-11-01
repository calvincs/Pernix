"""Live debug 2026-10-31: a large paste stalled scout for minutes on every turn."""

from core.scout.runner import SEARCH_TEXT_EDGE, _search_text


def test_short_messages_are_searched_whole():
    assert _search_text("find my notes") == "find my notes"


def test_a_large_paste_keeps_only_both_ends():
    text = "ask" + "x" * 40000 + "end"
    query = _search_text(text)
    assert len(query) == 2 * SEARCH_TEXT_EDGE + 1
    assert query.startswith("ask")
    assert query.endswith("end")
