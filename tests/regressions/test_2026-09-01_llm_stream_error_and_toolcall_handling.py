"""Four provider-layer defects around errors and streamed tool calls.

* Ollama raised inside the stream with raise_for_status(), discarding the
  body — the one place it says WHY (out of memory, bad message order, a
  context-length complaint). A 500 showed only its status line and burned
  three retries; a 400 about context could never classify as overflow, so
  compaction never fired.
* OpenRouter/OpenAI in-stream error objects were classified as HTTP 400
  with the `code` dropped, so a documented 200-plus-{"code": 502} body was
  non-retryable and a 429 was never a rate limit.
* Ollama numbered id-less tool calls per CHUNK, so two calls in two chunks
  both became "call_0" and the ladder merged them into one corrupt call.
* A retry or fallback re-streamed the answer with the previous partial
  still on screen, so the viewer saw <partial><full answer> while the
  database stored only the second.
"""

import inspect

from core.llm import stream_ladder
from core.llm.errors import FailoverReason, classify_http_error
from core.llm.providers import ollama as ollama_mod
from core.llm.stream_ladder import _merge_tool_call_deltas, is_stream_retryable


class _TC:
    def __init__(self, id, name, arguments):
        self.id, self.name, self.arguments = id, name, arguments


def test_two_named_calls_sharing_an_id_stay_separate():
    collected = []
    _merge_tool_call_deltas(collected, [_TC("call_0", "file_read", '{"path":"a"}')])
    _merge_tool_call_deltas(collected, [_TC("call_0", "bash", '{"command":"ls"}')])
    assert [c["name"] for c in collected] == ["file_read", "bash"], "an id collision must not merge two real calls"
    assert collected[0]["arguments"] == '{"path":"a"}', "nor concatenate their argument bodies"


def test_a_true_delta_still_merges():
    collected = []
    _merge_tool_call_deltas(collected, [_TC("call_1", "bash", '{"comm')])
    _merge_tool_call_deltas(collected, [_TC("call_1", "", 'and":"ls"}')])
    assert len(collected) == 1
    assert collected[0]["arguments"] == '{"command":"ls"}'


def test_ollama_ids_span_the_whole_stream():
    src = inspect.getsource(ollama_mod)
    assert "_tc_seq" in src, "a per-chunk index makes every chunk restart at call_0"
    assert 'tc.get("id")' in src, "the server's own id wins when it sends one"


def test_ollama_reads_the_error_body():
    src = inspect.getsource(ollama_mod)
    assert "await resp.aread()" in src
    assert 'http_status_failover("Ollama"' in src, "so the body can be classified, not just logged"


def test_an_in_stream_code_drives_the_classification():
    # 502 is retryable; the old code called every in-stream error a 400.
    assert classify_http_error(502, "Provider returned error") != FailoverReason.FORMAT_ERROR
    assert classify_http_error(429, "slow down") == FailoverReason.RATE_LIMIT
    assert is_stream_retryable("OpenRouter 502: Provider returned error")


def test_retry_and_fallback_tell_the_ui_to_drop_the_partial():
    src = inspect.getsource(stream_ladder.stream_with_failover)
    assert src.count('"type": "stream.reset"') == 2, "both the retry and the fallback rung re-stream from scratch"
    assert '"discard_partial": True' in src
