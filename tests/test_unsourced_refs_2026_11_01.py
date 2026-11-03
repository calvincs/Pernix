"""A cited source must appear in something the model was shown."""

from core.agent import _unsourced_refs


def _msgs(tool_text):
    return [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "find it"},
        {"role": "tool", "content": tool_text},
    ]


def test_invented_url_and_pubmed_id_are_caught():
    text = "See https://www.naswil.org/supervision and PubMed 19329851."
    assert _unsourced_refs(text, _msgs("nothing useful")) == ["https://www.naswil.org/supervision", "PubMed 19329851"]


def test_fetched_sources_pass_despite_format_changes():
    tool = "url: https://www.ncbi.nlm.nih.gov/pubmed/19741313/ PMID: 19741313 doi 10.1097/JSM.0b013e3181"
    text = "Source: https://ncbi.nlm.nih.gov/pubmed/19741313, PMID 19741313, DOI 10.1097/JSM.0b013e3181."
    assert _unsourced_refs(text, _msgs(tool)) == []


def test_a_url_in_the_users_message_passes():
    msgs = [{"role": "user", "content": [{"type": "text", "text": "read https://example.com/a"}]}]
    assert _unsourced_refs("I read https://example.com/a.", msgs) == []


def test_the_models_own_earlier_reply_is_not_a_source():
    msgs = [{"role": "assistant", "content": "https://made.up/x"}]
    assert _unsourced_refs("Again: https://made.up/x", msgs) == ["https://made.up/x"]


def test_a_reply_with_no_sources_is_left_alone():
    assert _unsourced_refs("Paris.", _msgs("x")) == []
