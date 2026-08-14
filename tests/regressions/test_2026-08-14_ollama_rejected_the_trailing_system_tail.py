"""Regression: Ollama 500'd on the volatile system tail, wedging every turn.

Shipped defect (box.ventibean.com, 2026-08-14): on `qwen3.8:27b-mtp-q8_0` —
and any model on Ollama's newer chat-renderer path — /api/chat answers

    500  chat prompt error: "system message must be at the beginning"

before the model is reached. The compile deliberately emits system messages
after the first one: the compaction summary, trim notices, and the volatile
clock/resource/telos tail, which is appended *last* precisely so the
cache-busting content sits in the prompt suffix. normalize_for_openrouter()
already rewrites those into user-role carriers, but it is applied only to
OPENAI_FORMAT_PROVIDERS on the theory that "Ollama is more permissive and
gets the raw compile output". That stopped being true.

The blast radius was every local-model turn: three stream attempts (10s and
15s backoff between them) all failing identically, ~45 seconds burned, then
failover to the backup model on OpenRouter. Sessions looked like they were
hanging and the local model was never actually used.

Fix: _to_native_format() — the single funnel for both Ollama chat paths —
carries mid-conversation system messages as user-role text, the same
treatment the OpenAI-format providers get. Dropping them instead (what
sanitize_for_fallback does) would take the compaction summary with it.
"""

from core.llm.providers.ollama import _to_native_format


def _compiled_turn() -> list[dict]:
    """The shape compile_context() produces: system head … volatile tail."""
    return [
        {"role": "system", "content": "You are Pernix."},
        {"role": "system", "content": "[Previous conversation summary] …"},
        {"role": "user", "content": "what is the weather"},
        {"role": "assistant", "content": "checking"},
        {"role": "system", "content": "[CURRENT STATE] clock 13:07 · 48 rounds left"},
    ]


def test_only_the_leading_system_message_keeps_its_role():
    native = _to_native_format(_compiled_turn())

    roles = [m["role"] for m in native]
    assert roles == ["system", "user", "user", "assistant", "user"]
    assert roles.count("system") == 1, "Ollama rejects any system message that is not first"


def test_the_tail_content_survives_the_rewrite():
    """Carried, not dropped — the model still sees the state and the summary."""
    native = _to_native_format(_compiled_turn())

    joined = " ".join(m["content"] for m in native)
    assert "[CURRENT STATE] clock 13:07 · 48 rounds left" in joined
    assert "[Previous conversation summary] …" in joined
    # And the tail stays last, where the prompt-suffix placement puts it.
    assert native[-1]["content"].startswith("[CURRENT STATE]")


def test_a_single_system_message_is_untouched():
    native = _to_native_format(
        [
            {"role": "system", "content": "You are Pernix."},
            {"role": "user", "content": "hi"},
        ]
    )

    assert [m["role"] for m in native] == ["system", "user"]


def test_tool_calls_still_convert_after_a_carried_system_message():
    """The rewrite must not disturb the native tool-call conversion."""
    native = _to_native_format(
        [
            {"role": "system", "content": "You are Pernix."},
            {"role": "system", "content": "[CURRENT STATE] …"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "bash", "arguments": '{"cmd": "ls"}'}}],
            },
            {"role": "tool", "content": "a.txt", "tool_call_id": "call_0"},
        ]
    )

    assert native[1]["role"] == "user"
    assert native[2]["tool_calls"] == [{"function": {"name": "bash", "arguments": {"cmd": "ls"}}}]
    assert native[3]["role"] == "tool"
