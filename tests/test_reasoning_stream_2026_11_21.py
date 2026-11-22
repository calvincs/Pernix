"""Providers report reasoning apart from the answer (2026-11-21).

vLLM serving a reasoning model returns the thinking in its own field:
non-streaming `message.reasoning` (older builds `reasoning_content`),
streaming `delta.reasoning`, and one delta can carry both a reasoning tail
and the first answer token (checked against the live server). It used to be
dropped. It now travels as StreamEventType.REASONING / ChatResponse.reasoning,
and the ladder emits it as `stream.reasoning`, never as `stream.token`.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from core.llm.providers.openai import OpenAIProvider
from core.llm.types import ProviderConfig, StreamEventType

SELF_HOSTED = "http://llm.example.internal:8000/v1"
MODEL = "inkling-2-mini"
WINDOW = 262_144


def _sse(*chunks: dict | str) -> bytes:
    return "".join(f"data: {c if isinstance(c, str) else json.dumps(c)}\n\n" for c in chunks).encode()


def _provider(handler=None, *, base_url: str = SELF_HOSTED, key: str = "") -> OpenAIProvider:
    p = OpenAIProvider(ProviderConfig(name="openai", base_url=base_url, api_key=key))
    if handler is not None:
        p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        p._quick_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return p


# ---------------------------------------------------------------------------
# Parsing: non-streaming and streaming
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_stream_reasoning_is_returned_beside_the_answer():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "5", "reasoning": "2+3 is 5."},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 27,
                    "completion_tokens": 40,
                    "total_tokens": 67,
                    "completion_tokens_details": {"reasoning_tokens": 32},
                },
            },
        )

    resp = await _provider(handler).chat([{"role": "user", "content": "2+3?"}], model=MODEL)
    assert resp.content == "5"
    assert resp.reasoning == "2+3 is 5."
    assert resp.usage.extra == {"reasoning_tokens": 32}


@pytest.mark.asyncio
async def test_non_stream_reads_the_older_reasoning_content_name():
    def handler(request):
        msg = {"role": "assistant", "content": "ok", "reasoning_content": "older vLLM"}
        return httpx.Response(200, json={"choices": [{"message": msg, "finish_reason": "stop"}]})

    resp = await _provider(handler).chat([{"role": "user", "content": "x"}], model=MODEL)
    assert (resp.content, resp.reasoning) == ("ok", "older vLLM")


@pytest.mark.asyncio
async def test_stream_emits_reasoning_events_and_never_mixes_them_into_tokens():
    body = _sse(
        {"choices": [{"delta": {"role": "assistant", "content": ""}}]},
        {"choices": [{"delta": {"reasoning": "The user asks"}}]},
        {"choices": [{"delta": {"reasoning_content": " 2+3."}}]},
        # The live server sends the reasoning tail and the first answer token
        # in ONE delta.
        {"choices": [{"delta": {"content": "5", "reasoning": " Done."}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 27, "completion_tokens": 39, "total_tokens": 66}},
        "[DONE]",
    )
    events = [
        e
        async for e in _provider(lambda r: httpx.Response(200, content=body)).chat_stream(
            [{"role": "user", "content": "2+3?"}], model=MODEL
        )
    ]
    thought = "".join(e.reasoning for e in events if e.type == StreamEventType.REASONING)
    answer = "".join(e.content for e in events if e.type == StreamEventType.TOKEN)
    assert thought == "The user asks 2+3. Done."
    assert answer == "5"
    assert all(e.content is None for e in events if e.type == StreamEventType.REASONING)
    # Reasoning arrives before the token it shared a delta with.
    kinds = [e.type for e in events if e.type in (StreamEventType.REASONING, StreamEventType.TOKEN)]
    assert kinds[-2:] == [StreamEventType.REASONING, StreamEventType.TOKEN]


@pytest.mark.asyncio
async def test_a_stream_that_fails_while_thinking_still_fails_over():
    """Reasoning is not answer output: a failure before the first answer token
    must reach the ladder as a typed error (it retries and the UI's
    stream.reset clears the thinking), not end the stream as a partial."""
    from core.llm.errors import FailoverError

    body = _sse(
        {"choices": [{"delta": {"reasoning": "thinking..."}}]},
        {"error": {"message": "upstream exploded", "code": 503}},
    )
    stream = _provider(lambda r: httpx.Response(200, content=body)).chat_stream(
        [{"role": "user", "content": "x"}], model=MODEL
    )
    with pytest.raises(FailoverError):
        async for _ in stream:
            pass


@pytest.mark.asyncio
async def test_length_with_no_answer_is_called_out_in_the_log(caplog):
    body = _sse(
        {"choices": [{"delta": {"reasoning": "So much to consider"}}]},
        {"choices": [{"delta": {}, "finish_reason": "length"}]},
        "[DONE]",
    )
    with caplog.at_level("WARNING", logger="pernix.llm.openai"):
        events = [
            e
            async for e in _provider(lambda r: httpx.Response(200, content=body)).chat_stream(
                [{"role": "user", "content": "x"}], model=MODEL
            )
        ]
    assert events[-1].type == StreamEventType.DONE and events[-1].finish_reason == "length"
    assert "reasoning consumed the output budget" in caplog.text

    def handler(request):
        msg = {"role": "assistant", "content": None, "reasoning": "still thinking"}
        return httpx.Response(200, json={"choices": [{"message": msg, "finish_reason": "length"}]})

    caplog.clear()
    with caplog.at_level("WARNING", logger="pernix.llm.openai"):
        resp = await _provider(handler).chat([{"role": "user", "content": "x"}], model=MODEL)
    assert resp.content == "" and resp.finish_reason == "length"
    assert "reasoning consumed the output budget" in caplog.text


# ---------------------------------------------------------------------------
# Ladder: reasoning reaches the UI as its own event and the outcome apart
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ladder_emits_stream_reasoning_and_keeps_it_out_of_content(monkeypatch):
    from core.llm import stream_ladder
    from core.llm.types import StreamEvent

    async def chat_stream(*a, **kw):
        yield StreamEvent(type=StreamEventType.REASONING, reasoning="think ")
        yield StreamEvent(type=StreamEventType.REASONING, reasoning="hard")
        yield StreamEvent(type=StreamEventType.TOKEN, content="answer")
        yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")

    client = SimpleNamespace(chat_stream=chat_stream, has_capacity=lambda m: True, resolve_provider=lambda m: "openai")
    monkeypatch.setattr(stream_ladder, "derive_max_output", lambda m: 1000)
    events = []
    outcome = await stream_ladder.stream_with_failover(
        client=client,
        session_id="s",
        emit=events.append,
        messages=[],
        base_messages=[],
        static_prefix_chars=0,
        tools=None,
        model=MODEL,
        max_output_cap=0,
        goal_id=None,
        sched_created_at=0.0,
        sched_priority=0,
    )
    assert outcome.content == "answer" and outcome.reasoning == "think hard"
    assert [e for e in events if e["type"] == "stream.reasoning"] == [
        {"type": "stream.reasoning", "content": "think "},
        {"type": "stream.reasoning", "content": "hard"},
    ]
    assert [e["content"] for e in events if e["type"] == "stream.token"] == ["answer"]
