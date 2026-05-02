"""Tests for core/reflect.py: ReflectResult, JSON parsing, verdict logic."""

import json

import pytest

from core.llm.types import ChatResponse, TokenUsage
from core.reflect import (
    ReflectResult,
    _format_message,
    _has_pass_with_lessons,
    _result_from_data,
    _try_repair_json,
    build_lessons_context,
    reflect_on_session,
)

# ---------------------------------------------------------------------------
# ReflectResult defaults
# ---------------------------------------------------------------------------


def test_reflect_result_defaults():
    r = ReflectResult()
    assert r.verdict == "pass"
    assert r.reasoning == ""
    assert r.diagnostic == ""
    assert r.reflect_latency_ms == 0


# ---------------------------------------------------------------------------
# build_lessons_context
# ---------------------------------------------------------------------------


def test_build_lessons_context():
    result = ReflectResult(
        verdict="retry",
        reasoning="Task was not completed",
        diagnostic="Missing file permissions",
        what_worked="file_read worked",
        what_failed="bash returned error",
        strategy="Try using file_write directly",
    )
    lessons = build_lessons_context(result, attempt=1, max_attempts=2)
    assert "Retry #1 of 2" in lessons
    assert "Task was not completed" in lessons
    assert "Missing file permissions" in lessons
    assert "file_read worked" in lessons
    assert "bash returned error" in lessons
    assert "file_write" in lessons


def test_build_lessons_context_empty_fields():
    result = ReflectResult(verdict="retry", reasoning="failed")
    lessons = build_lessons_context(result, attempt=2, max_attempts=3)
    assert "Retry #2 of 3" in lessons
    assert "failed" in lessons


# ---------------------------------------------------------------------------
# _try_repair_json
# ---------------------------------------------------------------------------


def test_try_repair_json_valid():
    raw = '{"verdict": "pass", "reasoning": "ok"}'
    result = _try_repair_json(raw)
    assert result == {"verdict": "pass", "reasoning": "ok"}


def test_try_repair_json_with_fences():
    raw = '```json\n{"verdict": "retry", "reasoning": "failed"}\n```'
    result = _try_repair_json(raw)
    assert result is not None
    assert result.get("verdict") == "retry"


def test_try_repair_json_empty():
    assert _try_repair_json("") is None
    assert _try_repair_json("   ") is None


def test_try_repair_json_no_json():
    assert _try_repair_json("just plain text") is None


def test_try_repair_json_with_thinking_tags():
    raw = '<think>reasoning here</think>\n{"verdict": "pass", "reasoning": "done"}'
    result = _try_repair_json(raw)
    assert result is not None
    assert result.get("verdict") == "pass"


# ---------------------------------------------------------------------------
# _result_from_data
# ---------------------------------------------------------------------------


def test_result_from_data_pass():
    data = {"verdict": "pass", "reasoning": "task done"}
    r = _result_from_data(data, "test-model", 100)
    assert r.verdict == "pass"
    assert r.reasoning == "task done"
    assert r.reflect_model == "test-model"
    assert r.reflect_latency_ms == 100


def test_result_from_data_retry():
    data = {
        "verdict": "retry",
        "reasoning": "failed",
        "diagnostic": "permission error",
        "what_worked": "file_read",
        "what_failed": "bash",
        "strategy": "use file_write",
    }
    r = _result_from_data(data, "model", 200)
    assert r.verdict == "retry"
    assert r.diagnostic == "permission error"
    assert r.what_worked == "file_read"
    assert r.strategy == "use file_write"


def test_result_from_data_invalid_verdict():
    """Invalid verdict coerces to 'retry' (NOT 'pass'). Historic behavior was
    to default-to-pass on garbage, but workflow run e8c94b86 (2026-04-27)
    showed that's dangerous — the model said verdict='fail' with reasoning
    that the file was missing, and the silent flip to pass would have
    shipped a fake success absent the orchestrator's pass-but-no-output
    guard. Coerce to retry so the retry budget catches it instead."""
    data = {"verdict": "invalid_verdict", "reasoning": "hmm"}
    r = _result_from_data(data, "m", 0)
    assert r.verdict == "retry"


# ---------------------------------------------------------------------------
# _has_pass_with_lessons
# ---------------------------------------------------------------------------


def test_has_pass_with_lessons_clean_pass():
    r = ReflectResult(verdict="pass", reasoning="all good")
    assert _has_pass_with_lessons(r) is False


def test_has_pass_with_lessons_pass_with_strategy():
    r = ReflectResult(verdict="pass", strategy="should have called X first")
    assert _has_pass_with_lessons(r) is True


def test_has_pass_with_lessons_pass_with_diagnostic():
    r = ReflectResult(verdict="pass", diagnostic="agent skipped scout's plan")
    assert _has_pass_with_lessons(r) is True


def test_has_pass_with_lessons_pass_with_what_failed():
    r = ReflectResult(verdict="pass", what_failed="ignored the recommended tool")
    assert _has_pass_with_lessons(r) is True


def test_has_pass_with_lessons_retry_never_qualifies():
    """Retry/escalate already trigger downstream paths; this helper is pass-only."""
    r = ReflectResult(verdict="retry", strategy="try again differently")
    assert _has_pass_with_lessons(r) is False


def test_has_pass_with_lessons_whitespace_only_treated_as_empty():
    r = ReflectResult(verdict="pass", strategy="   \n  ", diagnostic="\t")
    assert _has_pass_with_lessons(r) is False


# ---------------------------------------------------------------------------
# _format_message
# ---------------------------------------------------------------------------


def test_format_message_user():
    msg = {"role": "user", "content": "hello world"}
    result = _format_message(msg)
    assert "[USER]" in result
    assert "hello world" in result


def test_format_message_assistant():
    msg = {"role": "assistant", "content": "I'll help"}
    result = _format_message(msg)
    assert "[ASSISTANT]" in result
    assert "I'll help" in result


def test_format_message_assistant_with_tool_calls():
    msg = {
        "role": "assistant",
        "content": "",
        "tool_calls": json.dumps([{"name": "bash", "arguments": '{"command": "ls"}'}]),
    }
    result = _format_message(msg)
    assert "TOOL CALL: bash" in result


def test_format_message_tool():
    msg = {"role": "tool", "content": "file contents here"}
    result = _format_message(msg)
    assert "[TOOL RESULT]" in result
    assert "file contents here" in result


# ---------------------------------------------------------------------------
# reflect_on_session (integration)
# ---------------------------------------------------------------------------


async def test_reflect_pass_verdict(mock_llm_client):
    from db import models as db

    sid = db.create_session(title="Reflect Test")
    db.add_message(sid, "user", "Write a hello world file")
    db.add_message(sid, "assistant", "Done! I wrote the file.")

    mock_llm_client.responses = [
        ChatResponse(
            content='{"verdict": "pass", "reasoning": "File was created successfully"}',
            tool_calls=None,
            usage=TokenUsage(10, 5, 15),
            model="test",
            provider="fake",
            finish_reason="stop",
        )
    ]

    result = await reflect_on_session(sid)
    assert result.verdict == "pass"
    assert "File" in result.reasoning


async def test_reflect_retry_verdict(mock_llm_client):
    from db import models as db

    sid = db.create_session(title="Retry Test")
    db.add_message(sid, "user", "Create a complex analysis report")
    db.add_message(sid, "assistant", "I wasn't able to complete this.")

    mock_llm_client.responses = [
        ChatResponse(
            content=json.dumps(
                {
                    "verdict": "retry",
                    "reasoning": "Report was not created",
                    "diagnostic": "Wrong approach",
                    "what_worked": "nothing",
                    "what_failed": "all tools",
                    "strategy": "Try a different approach",
                }
            ),
            tool_calls=None,
            usage=TokenUsage(10, 20, 30),
            model="test",
            provider="fake",
            finish_reason="stop",
        )
    ]

    result = await reflect_on_session(sid)
    assert result.verdict == "retry"
    assert result.diagnostic == "Wrong approach"


async def test_reflect_empty_session():
    """Empty session returns pass without LLM call."""
    from db import models as db

    sid = db.create_session(title="Empty")
    result = await reflect_on_session(sid)
    assert result.verdict == "pass"


async def test_reflect_json_parse_error(mock_llm_client):
    """Malformed JSON from LLM must NOT trigger a turn retry.

    A parse failure is a verifier-side problem — re-running scout + the
    full agent loop to recover from a malformed reflect response is a
    cost mismatch (~5-10 min per replay vs. a few hundred ms per parse).
    Inner reprompt + repair must absorb the failure, falling back to a
    soft "pass" with low confidence so dashboards/eval can flag it.
    """
    from db import models as db

    sid = db.create_session(title="Parse Error")
    db.add_message(sid, "user", "Do something")
    db.add_message(sid, "assistant", "Done")

    mock_llm_client.responses = [
        ChatResponse(
            content="this is not json at all",
            tool_calls=None,
            usage=TokenUsage(10, 5, 15),
            model="test",
            provider="fake",
            finish_reason="stop",
        )
    ]

    result = await reflect_on_session(sid)
    # Soft-pass on parse failure — must not trigger a turn-level retry.
    assert result.verdict == "pass"
    assert result.failure_cause == "none"
    assert result.confidence == 0.0
    assert "soft-pass" in result.reasoning.lower() or "parse" in result.reasoning.lower()
    # Inner reprompt should have happened: more than one chat call.
    assert mock_llm_client.call_count >= 2


async def test_reflect_llm_error(mock_llm_client):
    """LLM exception results in pass verdict."""
    from db import models as db

    sid = db.create_session(title="LLM Error")
    db.add_message(sid, "user", "Do something")
    db.add_message(sid, "assistant", "Done")

    async def failing_chat(*args, **kwargs):
        raise ConnectionError("LLM unavailable")

    mock_llm_client.chat = failing_chat

    result = await reflect_on_session(sid)
    assert result.verdict == "pass"
    assert "Reflect error" in result.reasoning


# ---------------------------------------------------------------------------
# reflect_model config chain
# ---------------------------------------------------------------------------


async def test_reflect_model_priority(monkeypatch, mock_llm_client):
    """reflect_model takes priority over background_model."""
    from db import models as db

    monkeypatch.setattr("config.settings.reflect_model", "reflect-special")
    monkeypatch.setattr("config.settings.background_model", "bg-model")
    monkeypatch.setattr("config.settings.scout_model", "scout-model")

    sid = db.create_session(title="Model Priority")
    db.add_message(sid, "user", "Build a widget")
    db.add_message(sid, "assistant", "Done building the widget")
    db.add_message(sid, "tool", "file_write succeeded")

    # Configure fake to return valid reflect JSON
    mock_llm_client.responses = [
        ChatResponse(
            content=json.dumps({"verdict": "pass", "reasoning": "looks good"}),
            tool_calls=None,
            usage=TokenUsage(10, 5, 15),
            model="reflect-special",
            provider="fake",
            finish_reason="stop",
        )
    ]

    await reflect_on_session(sid)
    assert mock_llm_client.calls[-1]["model"] == "reflect-special"


async def test_reflect_model_fallback_to_background(monkeypatch, mock_llm_client):
    """When reflect_model is empty, falls back to background_model."""
    from db import models as db

    monkeypatch.setattr("config.settings.reflect_model", "")
    monkeypatch.setattr("config.settings.background_model", "bg-model")
    monkeypatch.setattr("config.settings.scout_model", "scout-model")

    sid = db.create_session(title="Model Fallback")
    db.add_message(sid, "user", "Build a widget")
    db.add_message(sid, "assistant", "Done building the widget")
    db.add_message(sid, "tool", "file_write succeeded")

    mock_llm_client.responses = [
        ChatResponse(
            content=json.dumps({"verdict": "pass", "reasoning": "ok"}),
            tool_calls=None,
            usage=TokenUsage(10, 5, 15),
            model="bg-model",
            provider="fake",
            finish_reason="stop",
        )
    ]

    await reflect_on_session(sid)
    assert mock_llm_client.calls[-1]["model"] == "bg-model"
