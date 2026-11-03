"""Live debug 2026-11-01: wrong local times, and scout skipping its report."""

import time

import pytest

from core.context.local_times import MAX_NOTES, local_time_note
from core.llm.providers.openai import OpenAIProvider
from core.llm.types import ProviderConfig


@pytest.fixture
def chicago(monkeypatch):
    monkeypatch.setenv("TZ", "America/Chicago")
    time.tzset()
    yield
    monkeypatch.delenv("TZ")
    time.tzset()


def _tool(name):
    return {"type": "function", "function": {"name": name, "parameters": {}}}


def _payload(tools, stream):
    provider = OpenAIProvider(ProviderConfig(name="openai", base_url="http://aibox:8000/v1", api_key="k"))
    return provider._build_payload([], "m", 100, tools, None, stream=stream)


def test_a_utc_stamp_gets_its_local_weekday_and_time(chicago):
    note = local_time_note("updated 2026-11-01T03:11:00+00:00")
    assert "= Saturday 2026-10-31 22:11 CDT" in note


def test_z_suffix_and_repeats_give_one_note(chicago):
    note = local_time_note("a 2026-11-01T03:11:00Z b 2026-11-01T03:11:00Z")
    assert note.count("= Saturday") == 1


def test_stamps_with_a_local_offset_are_left_alone():
    text = "updated 2026-10-31T22:11:00-05:00"
    assert local_time_note(text) == text


def test_notes_are_capped():
    text = " ".join(f"2026-11-01T03:{m:02d}:00Z" for m in range(20))
    assert local_time_note(text).count(" = ") == MAX_NOTES


def test_one_tool_is_forced_on_a_one_shot_call():
    assert _payload([_tool("submit_report")], stream=False)["tool_choice"]["function"]["name"] == "submit_report"


def test_several_tools_or_streaming_stay_free():
    assert "tool_choice" not in _payload([_tool("a"), _tool("b")], stream=False)
    assert "tool_choice" not in _payload([_tool("a")], stream=True)
