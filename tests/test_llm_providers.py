"""Tests for LLM provider parsing/formatting logic."""

import json

import pytest

from core.llm.providers.ollama import OllamaProvider, _to_native_format
from core.llm.types import ChatResponse, ProviderConfig, TokenUsage

# ---------------------------------------------------------------------------
# _to_native_format (Ollama message conversion)
# ---------------------------------------------------------------------------


def test_to_native_format_basic():
    messages = [{"role": "user", "content": "hello"}]
    result = _to_native_format(messages)
    assert result[0]["role"] == "user"
    assert result[0]["content"] == "hello"


def test_to_native_format_multimodal():
    """List content with image_url is converted to images field."""
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe this"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc123"}},
            ],
        }
    ]
    result = _to_native_format(messages)
    assert result[0]["content"] == "describe this"
    assert "abc123" in result[0].get("images", [])


def test_to_native_format_tool_calls_dict():
    """Tool call arguments as dict stay as dict."""
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"type": "function", "function": {"name": "bash", "arguments": {"command": "ls"}}}],
        }
    ]
    result = _to_native_format(messages)
    tc = result[0]["tool_calls"][0]
    assert tc["function"]["name"] == "bash"
    assert isinstance(tc["function"]["arguments"], dict)
    assert tc["function"]["arguments"]["command"] == "ls"


def test_to_native_format_tool_calls_json_string():
    """Tool call arguments as JSON string are parsed to dict."""
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "bash", "arguments": '{"command": "ls"}'}}],
        }
    ]
    result = _to_native_format(messages)
    tc = result[0]["tool_calls"][0]
    assert isinstance(tc["function"]["arguments"], dict)
    assert tc["function"]["arguments"]["command"] == "ls"


def test_to_native_format_system_message():
    messages = [{"role": "system", "content": "Be helpful"}]
    result = _to_native_format(messages)
    assert result[0]["role"] == "system"
    assert result[0]["content"] == "Be helpful"


def test_to_native_format_none_content():
    messages = [{"role": "assistant", "content": None}]
    result = _to_native_format(messages)
    assert result[0]["content"] == ""


def test_to_native_format_no_tool_call_id():
    """Tool responses don't include tool_call_id in native format."""
    messages = [{"role": "tool", "content": "result", "tool_call_id": "tc1"}]
    result = _to_native_format(messages)
    assert "tool_call_id" not in result[0]


# ---------------------------------------------------------------------------
# OllamaProvider: _parse_chat_response
# ---------------------------------------------------------------------------


def test_parse_chat_response_basic():
    provider = OllamaProvider()
    data = {
        "choices": [
            {
                "message": {"content": "Hello there!"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    result = provider._parse_chat_response(data, "llama3")
    assert result.content == "Hello there!"
    assert result.finish_reason == "stop"
    assert result.usage.prompt_tokens == 10


def test_parse_chat_response_with_tool_calls():
    provider = OllamaProvider()
    data = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "tc1",
                            "function": {"name": "bash", "arguments": '{"command": "ls"}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }
    result = provider._parse_chat_response(data, "llama3")
    assert result.tool_calls is not None
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "bash"
    assert result.finish_reason == "tool_calls"


def test_parse_chat_response_no_choices():
    provider = OllamaProvider()
    data = {"choices": []}
    with pytest.raises(ValueError):
        provider._parse_chat_response(data, "llama3")


def test_parse_chat_response_reasoning_fallback():
    """Content from 'reasoning' field when 'content' is empty."""
    provider = OllamaProvider()
    data = {
        "choices": [
            {
                "message": {"content": None, "reasoning": "This is the answer"},
                "finish_reason": "stop",
            }
        ],
        "usage": {},
    }
    result = provider._parse_chat_response(data, "qwen3")
    assert result.content == "This is the answer"


# ---------------------------------------------------------------------------
# OllamaProvider lifecycle
# ---------------------------------------------------------------------------


async def test_ollama_provider_close():
    """Close method handles already-closed clients gracefully."""
    provider = OllamaProvider()
    # Should not raise even without initialized clients
    await provider.close()


def test_ollama_provider_base_url():
    config = ProviderConfig(name="ollama", base_url="http://localhost:11434/v1")
    provider = OllamaProvider(config)
    assert provider._base_url() == "http://localhost:11434/v1"


# ---------------------------------------------------------------------------
# OpenRouter provider: formatting and parsing
# ---------------------------------------------------------------------------


def test_openrouter_provider_init():
    """OpenRouterProvider initializes with config."""
    from core.llm.providers.openrouter import OpenRouterProvider
    from core.llm.types import ProviderConfig

    config = ProviderConfig(name="openrouter", base_url="https://openrouter.ai/api/v1", api_key="test-key")
    provider = OpenRouterProvider(config)
    assert provider.name == "openrouter"
    assert provider.available is True


def test_openrouter_provider_not_available():
    """OpenRouterProvider is unavailable without API key."""
    from core.llm.providers.openrouter import OpenRouterProvider
    from core.llm.types import ProviderConfig

    config = ProviderConfig(name="openrouter", base_url="https://openrouter.ai/api/v1", api_key="")
    provider = OpenRouterProvider(config)
    assert provider.available is False


async def test_openrouter_provider_close():
    from core.llm.providers.openrouter import OpenRouterProvider
    from core.llm.types import ProviderConfig

    config = ProviderConfig(name="openrouter", base_url="https://openrouter.ai/api/v1", api_key="test-key")
    provider = OpenRouterProvider(config)
    await provider.close()  # Should not raise
