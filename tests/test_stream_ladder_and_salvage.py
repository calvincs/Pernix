"""The provider-layer seams the agent loop leans on.

Two pieces moved out of run_agent's body and now have to hold their contract
on their own: the text-salvage parsers (a model that wrote its tool call as
prose still made the call) and the single retry/fallback ladder that both the
tool loop and the final-answer path run through.
"""

from __future__ import annotations

from core.llm.providers.salvage import salvage_tool_calls
from core.llm.stream_ladder import is_stream_retryable, stream_with_failover
from core.llm.types import StreamEvent, StreamEventType, TokenUsage

# ===========================================================================
# Tool-call salvage
# ===========================================================================


def _always(_name: str) -> bool:
    return True


# Built at runtime rather than written literally: the point of these parsers
# is markup that leaks where it shouldn't, and a test file is exactly such a
# place. `p` is the vendor's tag decoration (DeepSeek ships one, others don't).
def _xml_call(name: str, params: dict, p: str = "") -> str:
    body = "".join('<{p}parameter name="{k}">{v}</{p}parameter>'.format(p=p, k=k, v=v) for k, v in params.items())
    return '<{p}invoke name="{n}">{b}</{p}invoke>'.format(p=p, n=name, b=body)


def test_salvage_returns_none_for_ordinary_prose():
    assert salvage_tool_calls("Here is the answer.", _always) is None
    assert salvage_tool_calls("", _always) is None


def test_salvage_kimi_special_tokens():
    content = (
        "thinking out loud\n"
        "<|tool_calls_section_begin|>"
        '<|tool_call_begin|>file_read:7<|tool_call_argument_begin|>{"path": "a.txt"}<|tool_call_end|>'
        "<|tool_calls_section_end|>"
    )
    out = salvage_tool_calls(content, _always)
    assert out is not None and out.format == "kimi"
    assert [tc["name"] for tc in out.tool_calls] == ["file_read"]
    assert out.tool_calls[0]["id"] == "kimi_7"
    assert out.tool_calls[0]["arguments"] == '{"path": "a.txt"}'
    # The leaked markup is stripped from what the caller will persist.
    assert "tool_call_begin" not in out.content
    assert out.content == "thinking out loud"


def test_salvage_xml_invoke_shape():
    import json

    content = "before " + _xml_call("bash", {"command": "ls"}) + " after"
    out = salvage_tool_calls(content, _always)
    assert out is not None and out.format == "xml-invoke"
    assert out.tool_calls[0]["name"] == "bash"
    assert json.loads(out.tool_calls[0]["arguments"]) == {"command": "ls"}
    assert "invoke" not in out.content


def test_salvage_xml_honors_the_vendor_tag_prefix():
    content = _xml_call("bash", {"command": "ls"}, p="antml:")
    out = salvage_tool_calls(content, _always)
    assert out is not None and "first prefix='antml:'" in out.summary


def test_salvage_xml_refuses_a_tool_the_registry_does_not_have():
    """Prose that merely looks like markup must not become an execution."""
    content = _xml_call("definitely_not_a_tool", {"x": "1"})
    assert salvage_tool_calls(content, lambda name: False) is None


def test_salvage_xml_requires_at_least_one_parameter():
    """Structural minimum — a bare invoke tag is not a call."""
    assert salvage_tool_calls(_xml_call("bash", {}), _always) is None


def test_kimi_parser_wins_when_both_shapes_are_present():
    content = "<|tool_call_begin|>file_read<|tool_call_argument_begin|>{}<|tool_call_end|>\n" + _xml_call(
        "bash", {"command": "ls"}
    )
    out = salvage_tool_calls(content, _always)
    assert out is not None and out.format == "kimi"


# ===========================================================================
# Retry / fallback ladder
# ===========================================================================


def test_retryable_classification():
    assert is_stream_retryable("HTTP 503 from upstream")
    assert is_stream_retryable("httpx.ConnectTimeout")
    assert not is_stream_retryable("401 Unauthorized")


class _FakeClient:
    """Records every chat_stream call and replays a scripted response."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []

    def has_capacity(self, model=""):
        return True

    def resolve_provider(self, model=""):
        return "ollama"  # not an OPENAI_FORMAT provider — no re-normalization

    def chat_stream(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        events = self.script.pop(0)

        async def _gen():
            for e in events:
                yield e

        return _gen()


async def _run(client, sid, **overrides):
    kwargs = dict(
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
    kwargs.update(overrides)
    return await stream_with_failover(**kwargs)


async def test_ladder_returns_content_and_usage(monkeypatch):
    from db import models as db

    sid = db.create_session(title="ladder")
    client = _FakeClient(
        [
            [
                StreamEvent(type=StreamEventType.TOKEN, content="hello"),
                StreamEvent(type=StreamEventType.USAGE, usage=TokenUsage(10, 5, 15)),
                StreamEvent(type=StreamEventType.DONE, finish_reason="stop"),
            ]
        ]
    )
    out = await _run(client, sid)
    assert out.error is None
    assert out.content == "hello"
    assert out.finish_reason == "stop"
    assert out.usage.total_tokens == 15
    assert not out.tried_fallback


async def test_ladder_falls_back_once_then_reports(monkeypatch):
    """A non-retryable error skips the backoffs and goes straight to the
    fallback model; a second failure there is terminal."""
    from db import models as db

    monkeypatch.setattr("config.settings.fallback_model", "other/model")
    sid = db.create_session(title="ladder-fb")
    client = _FakeClient(
        [
            [StreamEvent(type=StreamEventType.ERROR, error="401 Unauthorized")],
            [StreamEvent(type=StreamEventType.ERROR, error="401 Unauthorized")],
        ]
    )
    out = await _run(client, sid)
    assert out.error == "401 Unauthorized"
    assert out.tried_fallback
    assert out.model == "other/model"
    assert [c["model"] for c in client.calls] == ["test/model", "other/model"]


async def test_ladder_caps_max_tokens_at_the_compilers_reservation(monkeypatch):
    """Goal C: the compiler shrinks its output reservation when the context is
    tight, so the request must not still ask for the model's full max_tokens."""
    from db import models as db

    monkeypatch.setattr("core.llm.stream_ladder.derive_max_output", lambda m: 8000)
    sid = db.create_session(title="ladder-cap")
    client = _FakeClient([[StreamEvent(type=StreamEventType.DONE)]])
    await _run(client, sid, max_output_cap=1200)
    assert client.calls[0]["max_tokens"] == 1200

    client = _FakeClient([[StreamEvent(type=StreamEventType.DONE)]])
    await _run(client, sid, max_output_cap=0)  # compiler reported nothing
    assert client.calls[0]["max_tokens"] == 8000


async def test_ladder_surfaces_context_overflow_instead_of_retrying(monkeypatch):
    """Retrying an oversized request can only fail the same way — the caller
    has to compact, so the overflow comes back rather than laddering."""
    from core.llm.errors import FailoverError, FailoverReason
    from db import models as db

    monkeypatch.setattr("config.settings.fallback_model", "other/model")
    sid = db.create_session(title="ladder-overflow")

    class _Overflowing(_FakeClient):
        def chat_stream(self, messages, **kwargs):
            self.calls.append({"messages": messages, **kwargs})
            raise FailoverError(FailoverReason.CONTEXT_OVERFLOW, "too long")

    client = _Overflowing([])
    out = await _run(client, sid, surface_context_overflow=True)
    assert out.context_overflow is not None
    assert out.error is None
    assert len(client.calls) == 1, "must not retry or fail over on an overflow"

    # Without the opt-in it is just another stream error and takes the ladder.
    client = _Overflowing([])
    out = await _run(client, sid, surface_context_overflow=False)
    assert out.context_overflow is None
    assert out.error == "too long"
    assert [c["model"] for c in client.calls] == ["test/model", "other/model"]
