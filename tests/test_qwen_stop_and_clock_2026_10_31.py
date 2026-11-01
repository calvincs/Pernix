"""Live debug 2026-10-31: repeated answers and a guessed weekday."""

from datetime import datetime

from core.context.compiler import _build_volatile_tail
from core.llm.providers.openai import OpenAIProvider
from core.llm.providers.salvage import TextCut
from core.llm.types import ProviderConfig


def _provider(base_url: str) -> OpenAIProvider:
    return OpenAIProvider(ProviderConfig(name="openai", base_url=base_url, api_key="k"))


def _reply(content: str) -> dict:
    return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}


def test_no_server_stop_sequence_so_tool_arguments_stay_whole():
    assert "stop" not in _provider("http://llm.example.internal:8000/v1")._build_payload(
        [], "m", 100, None, None, stream=True
    )


def test_self_hosted_reply_ends_at_a_stray_think_close_tag():
    resp = _provider("http://llm.example.internal:8000/v1")._parse_response(_reply("done</think>done"), "m")
    assert resp.content == "done"


def test_real_openai_reply_is_left_alone():
    resp = _provider("https://api.openai.com/v1")._parse_response(_reply("done</think>done"), "m")
    assert resp.content == "done</think>done"


def test_text_cut_finds_a_marker_split_across_chunks():
    cut = TextCut("</think>")
    assert cut.feed("answer<") == "answer"
    assert cut.feed("/thi") == ""
    assert cut.feed("nk>again") == ""
    assert cut.done


def test_text_cut_returns_held_text_that_was_not_the_marker():
    cut = TextCut("</think>")
    assert cut.feed("a </") == "a "
    assert cut.feed("div>") == "</div>"
    assert cut.feed("end <") == "end "
    assert cut.flush() == "<"
    assert not cut.done


def test_empty_marker_passes_everything():
    assert TextCut("").feed("</think>") == "</think>"


def test_clock_names_the_local_weekday():
    tail = _build_volatile_tail("")
    local_line = next(line for line in tail.splitlines() if line.startswith("Current time (local):"))
    assert datetime.now().astimezone().strftime("%A") in local_line
