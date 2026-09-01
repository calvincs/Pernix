"""A stalled stream ended the turn as a clean, empty answer.

`str(httpx.ReadTimeout())` is `''`. The adapters yielded
`StreamEvent(ERROR, error=str(e))`, the ladder tested `event.error` for truth
and skipped it, and the adapter's finally-block DONE handed the agent loop a
clean end-of-stream: no message saved, no error shown, no retry, no fallback.
On the box that is a 192k prompt stalling past the 600s read timeout.
"""

import httpx

from core.llm.providers._shared import describe_exception
from core.llm.stream_ladder import is_stream_retryable, stream_with_failover
from core.llm.types import StreamEvent, StreamEventType


def test_describe_exception_never_returns_empty():
    assert describe_exception(httpx.ReadTimeout("")) == "ReadTimeout"
    assert describe_exception(httpx.ConnectError("All connection attempts failed")) == (
        "ConnectError: All connection attempts failed"
    )
    assert describe_exception(ValueError("bad")) == "ValueError: bad"


def test_described_transport_errors_are_retryable():
    # These used to miss every marker: '' matches nothing, and Ollama's
    # ConnectError text carries no class name.
    assert is_stream_retryable(describe_exception(httpx.ReadTimeout("")))
    assert is_stream_retryable(describe_exception(httpx.ConnectError("All connection attempts failed")))


class _Client:
    def __init__(self, script):
        self.script = list(script)

    def has_capacity(self, model=""):
        return True

    def resolve_provider(self, model=""):
        return "ollama"

    def chat_stream(self, messages, **kwargs):
        events = self.script.pop(0)

        async def _gen():
            for e in events:
                yield e

        return _gen()


async def test_ladder_treats_empty_error_event_as_an_error(monkeypatch):
    from db import models as db

    monkeypatch.setattr("config.settings.fallback_model", "")
    monkeypatch.setattr("core.llm.stream_ladder.STREAM_BACKOFFS", ())
    sid = db.create_session(title="stall")
    client = _Client(
        [
            [
                StreamEvent(type=StreamEventType.TOKEN, content="partial"),
                StreamEvent(type=StreamEventType.ERROR, error=""),
                StreamEvent(type=StreamEventType.DONE, finish_reason=None),
            ]
        ]
    )
    out = await stream_with_failover(
        client=client,
        session_id=sid,
        emit=lambda e: None,
        messages=[{"role": "user", "content": "hi"}],
        base_messages=[{"role": "user", "content": "hi"}],
        static_prefix_chars=0,
        tools=None,
        model="test/model",
        max_output_cap=0,
        goal_id=None,
        sched_created_at=0.0,
        sched_priority=0,
    )
    assert out.error, "an ERROR event with no text must still fail the stream"
    assert out.content == "partial"
