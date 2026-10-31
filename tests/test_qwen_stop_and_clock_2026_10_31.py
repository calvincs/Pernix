"""Live debug 2026-10-31: repeated answers and a guessed weekday."""

from datetime import datetime

from core.context.compiler import _build_volatile_tail
from core.llm.providers.openai import OpenAIProvider
from core.llm.types import ProviderConfig


def _payload(base_url: str) -> dict:
    provider = OpenAIProvider(ProviderConfig(name="openai", base_url=base_url, api_key="k"))
    return provider._build_payload([], "m", 100, None, None, stream=True)


def test_self_hosted_requests_stop_at_a_stray_think_close_tag():
    assert _payload("http://aibox.ventibean.com:8000/v1")["stop"] == ["</think>"]


def test_real_openai_requests_carry_no_stop_sequence():
    assert "stop" not in _payload("https://api.openai.com/v1")


def test_clock_names_the_local_weekday():
    tail = _build_volatile_tail("")
    local_line = next(line for line in tail.splitlines() if line.startswith("Current time (local):"))
    assert datetime.now().astimezone().strftime("%A") in local_line
