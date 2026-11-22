"""Stored reasoning goes back to the model that produced it (2026-11-21).

The chat template of a reasoning model on vLLM (Inkling 2 Mini) re-renders a
prior assistant turn's reasoning only when the client sends it back on that
message as `reasoning` (or `reasoning_content`); verified on the live server
by prompt_tokens (40 without, 53 with either name). The compiler attaches it
for the self-hosted OpenAI-compatible provider and the same model only,
counts it against the budget, and sheds it oldest-first before trim drops a
message. api.openai.com and OpenRouter never receive it.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from config import settings
from core.llm.providers.openai import OpenAIProvider
from core.llm.providers.openrouter import OpenRouterProvider
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


def _windowed(window: int = WINDOW) -> OpenAIProvider:
    p = _provider()
    p._models_cache = [{"id": MODEL, "max_model_len": window}]
    return p


async def test_openrouter_surfaces_reasoning_and_strips_it_from_requests():
    seen = {}
    body = _sse(
        {"choices": [{"delta": {"reasoning": "hmm"}}]},
        {"choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}]},
        "[DONE]",
    )

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=body)

    p = OpenRouterProvider(ProviderConfig(name="openrouter", base_url="https://openrouter.test/api/v1", api_key="k"))
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    msgs = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b", "reasoning": "secret", "reasoning_model": "x/y"},
        {"role": "user", "content": "c"},
    ]
    events = [e async for e in p.chat_stream(msgs, model="x/y")]
    assert [e.reasoning for e in events if e.type == StreamEventType.REASONING] == ["hmm"]
    assert "".join(e.content for e in events if e.type == StreamEventType.TOKEN) == "hi"
    sent = seen["body"]["messages"][1]
    assert "reasoning" not in sent and "reasoning_model" not in sent


def test_prior_reasoning_goes_back_only_to_the_model_that_produced_it():
    msgs = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "", "reasoning": "mine", "reasoning_model": MODEL, "tool_calls": []},
        {"role": "assistant", "content": "b", "reasoning": "someone else's", "reasoning_model": "other-model"},
        {"role": "user", "content": "c", "reasoning": "users have none"},
    ]
    sent = _windowed()._build_payload(msgs, MODEL, 100, None, None, stream=True)["messages"]
    assert sent[1]["reasoning"] == "mine"
    assert "reasoning" not in sent[2]
    assert "reasoning" not in sent[3]
    assert not any("reasoning_model" in m for m in sent)
    # The caller's list is not mutated.
    assert msgs[1]["reasoning_model"] == MODEL


# ---------------------------------------------------------------------------
# Compilation: echo-back, budget accounting, trim order
# ---------------------------------------------------------------------------


@pytest.fixture
def echo_on(monkeypatch):
    """A registry that routes MODEL to the openai provider on a self-hosted URL."""
    monkeypatch.setattr(settings, "openai_base_url", SELF_HOSTED)
    fake = SimpleNamespace(resolve_provider=lambda m: "openai" if m == MODEL else "openrouter")
    monkeypatch.setattr("core.llm.client.get_llm_client", lambda: fake)


def _turn(db, sid, reasoning_model=MODEL):
    db.add_message(sid, "user", "first question")
    db.add_message(
        sid,
        "assistant",
        "",
        tool_calls=json.dumps([{"id": "c1", "name": "bash", "arguments": "{}"}]),
        metadata=json.dumps({"model": reasoning_model, "reasoning_model": reasoning_model}),
        reasoning="I should run a command.",
    )
    db.add_message(sid, "tool", "ok", tool_call_id="c1")
    db.add_message(
        sid,
        "assistant",
        "done",
        metadata=json.dumps({"model": reasoning_model}),
        reasoning="The command worked.",
    )
    return db.add_message(sid, "user", "second question")


def test_compiled_history_carries_same_model_reasoning(echo_on):
    from core.context.compiler import compile_context
    from db import models as db

    sid = db.create_session()
    uid = _turn(db, sid)
    payload = compile_context(sid, model_name=MODEL, turn_user_msg_id=uid)
    assistants = [m for m in payload.messages if m["role"] == "assistant"]
    assert [m.get("reasoning") for m in assistants] == ["I should run a command.", "The command worked."]
    assert all(m["reasoning_model"] == MODEL for m in assistants)
    # Answer text is untouched by it.
    assert assistants[1]["content"] == "done"
    # And the provider puts exactly that on the wire.
    sent = _windowed()._build_payload(payload.messages, MODEL, 1000, None, None, stream=True)["messages"]
    assert [m.get("reasoning") for m in sent if m["role"] == "assistant"] == [
        "I should run a command.",
        "The command worked.",
    ]


def test_reasoning_is_counted_against_the_history_budget(echo_on, monkeypatch):
    from core.context.compiler import compile_context
    from db import models as db

    sid = db.create_session()
    uid = _turn(db, sid)
    with_echo = compile_context(sid, model_name=MODEL, turn_user_msg_id=uid).metadata.history_tokens
    monkeypatch.setattr(settings, "openai_base_url", "https://api.openai.com/v1")
    without = compile_context(sid, model_name=MODEL, turn_user_msg_id=uid).metadata.history_tokens
    assert with_echo > without


def test_no_echo_for_another_model_or_another_provider(echo_on):
    from core.context.compiler import compile_context
    from db import models as db

    sid = db.create_session()
    uid = _turn(db, sid, reasoning_model="some-other-model")
    payload = compile_context(sid, model_name=MODEL, turn_user_msg_id=uid)
    assert not any("reasoning" in m for m in payload.messages)

    sid2 = db.create_session()
    uid2 = _turn(db, sid2, reasoning_model="org/model")
    payload = compile_context(sid2, model_name="org/model", turn_user_msg_id=uid2)
    assert not any("reasoning" in m for m in payload.messages)


def test_trim_sheds_old_reasoning_before_dropping_messages():
    from core.context.compiler import _trim_history
    from core.context.tokens import get_estimator

    est = get_estimator()
    thought = "deliberation " * 400
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1", "reasoning": thought, "reasoning_model": MODEL},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "a2", "reasoning": thought, "reasoning_model": MODEL},
    ]
    one_thought = est.count(thought)
    total = sum(est.count_message(m) for m in messages[1:]) + 2 * one_thought
    kept, trimmed, dropped = _trim_history(list(messages), total - one_thought // 2, est)
    assert trimmed == 0 and dropped == []
    assert [m["content"] for m in kept[1:]] == ["q1", "a1", "q2", "a2"]
    # Oldest first: the first answer lost its thinking, the latest kept it.
    assert "reasoning" not in kept[2] and "reasoning_model" not in kept[2]
    assert kept[4]["reasoning"] == thought
