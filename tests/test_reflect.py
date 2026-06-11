"""Tests for core/reflect.py: ReflectResult, JSON parsing, verdict logic."""

import json

import pytest

from core.llm.types import ChatResponse, TokenUsage
from core.reflect import (
    ReflectResult,
    _format_message,
    _has_pass_with_lessons,
    _result_from_data,
    _sanitize_turn_digest,
    _try_repair_json,
    build_retry_context,
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
# build_retry_context
# ---------------------------------------------------------------------------


def test_build_retry_context():
    result = ReflectResult(
        verdict="retry",
        reasoning="Task was not completed",
        diagnostic="Missing file permissions",
        what_worked="file_read worked",
        what_failed="bash returned error",
        strategy="Try using file_write directly",
    )
    lessons = build_retry_context(result, attempt=1, max_attempts=2)
    assert "Retry #1 of 2" in lessons
    assert "Task was not completed" in lessons
    assert "Missing file permissions" in lessons
    assert "file_read worked" in lessons
    assert "bash returned error" in lessons
    assert "file_write" in lessons


def test_build_retry_context_empty_fields():
    result = ReflectResult(verdict="retry", reasoning="failed")
    lessons = build_retry_context(result, attempt=2, max_attempts=3)
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


# ---------------------------------------------------------------------------
# Termination-history awareness + ceiling-loop guard
# ---------------------------------------------------------------------------


async def test_reflect_evidence_includes_termination_history(mock_llm_client):
    """Termination history must surface in the evidence sent to the LLM."""
    from db import models as db

    sid = db.create_session(title="History Test")
    db.add_message(sid, "user", "Do a complex task")
    db.add_message(sid, "assistant", "I made some progress but ran out of rounds.")

    mock_llm_client.responses = [
        ChatResponse(
            content=json.dumps({"verdict": "retry", "reasoning": "more rounds needed"}),
            tool_calls=None,
            usage=TokenUsage(10, 5, 15),
            model="test",
            provider="fake",
            finish_reason="stop",
        )
    ]

    await reflect_on_session(
        sid,
        termination_reason="round_ceiling",
        prior_termination_reasons=[],
    )
    # The user-content portion of the call must contain the termination block.
    user_content = mock_llm_client.calls[-1]["messages"][-1]["content"]
    assert "TERMINATION HISTORY" in user_content
    assert "round_ceiling" in user_content


async def test_reflect_ceiling_loop_overrides_retry_to_escalate(mock_llm_client):
    """Two consecutive round_ceiling hits → guard forces verdict to escalate."""
    from db import models as db

    sid = db.create_session(title="Ceiling Loop")
    db.add_message(sid, "user", "Transcribe massive video")
    db.add_message(sid, "assistant", "Hit max rounds before completing")

    mock_llm_client.responses = [
        ChatResponse(
            # LLM picks 'retry' — guard must override.
            content=json.dumps(
                {
                    "verdict": "retry",
                    "reasoning": "More rounds might help",
                    "strategy": "Retry with smaller chunks",
                }
            ),
            tool_calls=None,
            usage=TokenUsage(10, 5, 15),
            model="test",
            provider="fake",
            finish_reason="stop",
        )
    ]

    result = await reflect_on_session(
        sid,
        termination_reason="round_ceiling",
        prior_termination_reasons=["round_ceiling"],
    )
    assert result.verdict == "escalate"
    assert "round_ceiling" in result.missing.lower() or "max_tool_rounds" in result.missing.lower()


async def test_reflect_first_ceiling_does_not_override(mock_llm_client):
    """Single round_ceiling (no prior) leaves the LLM's verdict alone."""
    from db import models as db

    sid = db.create_session(title="First Ceiling")
    db.add_message(sid, "user", "Do something")
    db.add_message(sid, "assistant", "First attempt hit ceiling")

    mock_llm_client.responses = [
        ChatResponse(
            content=json.dumps({"verdict": "retry", "reasoning": "first attempt"}),
            tool_calls=None,
            usage=TokenUsage(10, 5, 15),
            model="test",
            provider="fake",
            finish_reason="stop",
        )
    ]

    result = await reflect_on_session(
        sid,
        termination_reason="round_ceiling",
        prior_termination_reasons=[],  # no prior
    )
    # First ceiling — guard does NOT fire; verdict stays 'retry'.
    assert result.verdict == "retry"


async def test_reflect_other_terminal_reason_no_override(mock_llm_client):
    """Repeated non-ceiling reasons (e.g. compaction_failed) DON'T override."""
    from db import models as db

    sid = db.create_session(title="Compaction")
    db.add_message(sid, "user", "Do something")
    db.add_message(sid, "assistant", "Compaction blew up twice")

    mock_llm_client.responses = [
        ChatResponse(
            content=json.dumps({"verdict": "retry", "reasoning": "transient compaction issue"}),
            tool_calls=None,
            usage=TokenUsage(10, 5, 15),
            model="test",
            provider="fake",
            finish_reason="stop",
        )
    ]

    result = await reflect_on_session(
        sid,
        termination_reason="compaction_failed",
        prior_termination_reasons=["compaction_failed"],
    )
    # Code guard is round_ceiling-only; compaction stays LLM-decided.
    assert result.verdict == "retry"


# ---------------------------------------------------------------------------
# turn_digest emission, sanitization, and persistence
# ---------------------------------------------------------------------------


def test_sanitize_turn_digest_clamps_excerpt_length():
    """A misbehaving model can't blow up the post_mortem payload by emitting
    a megabyte excerpt — the sanitizer enforces the per-call char cap."""
    raw = {
        "scout_plan_summary": "X" * 5000,
        "agent_final_response": "Y" * 5000,
        "what_was_tried": "Z" * 5000,
        "key_findings": ["A" * 1000 for _ in range(20)],
        "tool_calls": [
            {
                "tool": "browse_web",
                "args": "url=https://example.com/" + "q" * 1000,
                "outcome": "success",
                "result_excerpt": "B" * 50_000,
            }
        ],
    }
    cleaned = _sanitize_turn_digest(raw)
    assert len(cleaned["scout_plan_summary"]) <= 1000
    assert len(cleaned["agent_final_response"]) <= 2000
    assert len(cleaned["what_was_tried"]) <= 1000
    assert len(cleaned["key_findings"]) <= 10
    assert all(len(f) <= 500 for f in cleaned["key_findings"])
    assert len(cleaned["tool_calls"][0]["result_excerpt"]) <= 2000
    assert len(cleaned["tool_calls"][0]["args"]) <= 500


def test_sanitize_turn_digest_normalizes_outcome():
    raw = {"tool_calls": [{"tool": "x", "outcome": "weird-status", "result_excerpt": "y"}]}
    cleaned = _sanitize_turn_digest(raw)
    assert cleaned["tool_calls"][0]["outcome"] == "unknown"


def test_sanitize_turn_digest_handles_missing_fields():
    """A minimal digest (just verdict's worth of fields filled) parses OK."""
    cleaned = _sanitize_turn_digest({})
    assert cleaned["scout_plan_summary"] == ""
    assert cleaned["tool_calls"] == []
    assert cleaned["key_findings"] == []


def test_result_from_data_parses_turn_digest():
    data = {
        "verdict": "retry",
        "reasoning": "missed the deliverable",
        "turn_digest": {
            "scout_plan_summary": "search then crawl",
            "tool_calls": [
                {
                    "tool": "search_web",
                    "args": "query=foo",
                    "outcome": "success",
                    "result_excerpt": "some real result body",
                }
            ],
            "agent_final_response": "Done.",
            "key_findings": ["Found X"],
            "what_was_tried": "Tried Y",
        },
    }
    r = _result_from_data(data, "m", 0)
    assert r.turn_digest["scout_plan_summary"] == "search then crawl"
    assert len(r.turn_digest["tool_calls"]) == 1
    assert r.turn_digest["tool_calls"][0]["result_excerpt"] == "some real result body"


def test_result_from_data_omits_digest_when_absent():
    """When verdict is pass and digest is absent, ReflectResult.turn_digest
    is an empty dict — not None, not a partial. Downstream consumers can
    treat empty as 'no digest available'."""
    r = _result_from_data({"verdict": "pass", "reasoning": "ok"}, "m", 0)
    assert r.turn_digest == {}


# ---------------------------------------------------------------------------
# build_retry_context (renamed from build_lessons_context)
# ---------------------------------------------------------------------------


def test_build_retry_context_includes_prior_digest():
    """The retry-context string injected into scout-N must carry the prior
    turn_digest so scout can plan around the actual evidence the previous
    attempt collected, not just reflect's free-form summary."""
    digest = {
        "scout_plan_summary": "search then verify URL",
        "tool_calls": [
            {
                "tool": "crawl4ai-fetch",
                "args": "url=https://www.war.gov/ufo/",
                "outcome": "success",
                "result_excerpt": "verbatim page body about disclosure images",
            }
        ],
        "agent_final_response": "URL is https://www.war.gov/ufo/",
        "key_findings": ["page returned 200 with real content"],
        "what_was_tried": "search → 403 on browse → fallback to crawl",
    }
    result = ReflectResult(
        verdict="retry",
        reasoning="claim looked unsupported",
        strategy="verify the URL is canonical via official .gov index",
        turn_digest=digest,
    )
    out = build_retry_context(result, attempt=1, max_attempts=2)
    assert "Retry #1 of 2" in out
    assert "PRIOR ATTEMPT DIGEST" in out
    assert "crawl4ai-fetch" in out
    assert "verbatim page body about disclosure images" in out
    assert "war.gov/ufo" in out
    assert "URL is https://www.war.gov/ufo/" in out


def test_build_retry_context_no_digest_still_works():
    """Back-compat: an old reflect run without a digest still produces a
    sensible retry-context string (just no PRIOR ATTEMPT DIGEST block)."""
    result = ReflectResult(
        verdict="retry",
        reasoning="failed",
        strategy="try X next",
    )
    out = build_retry_context(result, attempt=2, max_attempts=3)
    assert "Retry #2 of 3" in out
    assert "PRIOR ATTEMPT DIGEST" not in out
    assert "try X next" in out


def test_build_retry_context_flags_executed_side_effect_tools():
    """Successful one-shot external actions (notify_user, schedule_job, …)
    must surface as a hard DO-NOT-REPEAT block — reflect is biased toward
    retry on unverifiable side effects, and without this the retry attempt
    double-fires the action."""
    result = ReflectResult(verdict="retry", reasoning="could not verify the notification arrived")
    summary = {
        "notify_user": {"calls": 1, "failures": 0, "errors": [], "total_latency_ms": 5},
        "search_web": {"calls": 2, "failures": 0, "errors": [], "total_latency_ms": 900},
    }
    out = build_retry_context(result, attempt=1, max_attempts=2, tool_summary=summary)
    assert "ALREADY EXECUTED" in out
    assert "notify_user" in out.split("ALREADY EXECUTED")[1]
    # Read-only tools must not be flagged as side effects.
    assert "search_web" not in out.split("ALREADY EXECUTED")[1]


def test_build_retry_context_no_side_effect_block_when_all_failed():
    """A side-effecting tool that only FAILED is safe to retry — no guard."""
    result = ReflectResult(verdict="retry", reasoning="notify failed")
    summary = {
        "notify_user": {"calls": 1, "failures": 1, "errors": ["boom"], "total_latency_ms": 5},
    }
    out = build_retry_context(result, attempt=1, max_attempts=2, tool_summary=summary)
    assert "ALREADY EXECUTED" not in out


# ---------------------------------------------------------------------------
# Reflect emits and persists turn_digest end-to-end
# ---------------------------------------------------------------------------


async def test_reflect_emits_digest_on_retry(mock_llm_client):
    """When the LLM returns a retry verdict with a turn_digest, the digest
    must land on ReflectResult AND be persisted in the post_mortem
    payload_json so the next scout can read it."""
    from db import models as db

    sid = db.create_session(title="Digest emission test")
    db.add_message(sid, "user", "find the official US gov UFO disclosure site")
    db.add_message(sid, "assistant", "URL is https://www.war.gov/ufo/")

    digest = {
        "scout_plan_summary": "search + crawl",
        "tool_calls": [
            {
                "tool": "crawl4ai-fetch",
                "args": "url=https://www.war.gov/ufo/",
                "outcome": "success",
                "result_excerpt": "PURSUE program disclosure page",
            }
        ],
        "agent_final_response": "Done. URL is war.gov/ufo/",
        "key_findings": ["page fetched with real body"],
        "what_was_tried": "fetched the site directly",
    }
    mock_llm_client.responses = [
        ChatResponse(
            content=json.dumps(
                {
                    "verdict": "retry",
                    "reasoning": "URL was not validated against official directory",
                    "strategy": "verify via canonical .gov index",
                    "turn_digest": digest,
                }
            ),
            tool_calls=None,
            usage=TokenUsage(10, 5, 15),
            model="test",
            provider="fake",
            finish_reason="stop",
        )
    ]

    result = await reflect_on_session(sid)
    assert result.verdict == "retry"
    assert result.turn_digest["scout_plan_summary"] == "search + crawl"
    assert result.turn_digest["tool_calls"][0]["result_excerpt"] == "PURSUE program disclosure page"
    # The post_mortem row should have the digest too.
    pms = db.list_post_mortems(session_id=sid)
    assert pms, "post_mortem row was not written"
    payload = json.loads(pms[0]["payload_json"])
    assert "turn_digest" in payload
    assert payload["turn_digest"]["tool_calls"][0]["tool"] == "crawl4ai-fetch"
    assert payload["turn_digest"]["tool_calls"][0]["result_excerpt"] == "PURSUE program disclosure page"


async def test_reflect_omits_digest_on_pass_by_default(mock_llm_client):
    """Common-case optimization: pass verdicts skip the digest. The model is
    free to omit the key entirely, and ReflectResult.turn_digest stays empty."""
    from db import models as db

    sid = db.create_session(title="Pass without digest")
    db.add_message(sid, "user", "what time is it")
    db.add_message(sid, "assistant", "It is 3pm")

    mock_llm_client.responses = [
        ChatResponse(
            content=json.dumps({"verdict": "pass", "reasoning": "answered the simple question"}),
            tool_calls=None,
            usage=TokenUsage(10, 5, 15),
            model="test",
            provider="fake",
            finish_reason="stop",
        )
    ]

    result = await reflect_on_session(sid)
    assert result.verdict == "pass"
    assert result.turn_digest == {}


# ---------------------------------------------------------------------------
# Cornerstone: reflect sees real tool result body, not just stats
# (Regression for session f7885259462e — war.gov UFO disclosure case.)
# ---------------------------------------------------------------------------


async def test_reflect_sees_tool_result_body_in_evidence(mock_llm_client):
    """Cornerstone test for the turn-digest redesign.

    Pre-redesign, reflect saw only per-tool counts/failures and the agent's
    final response — never the actual tool result bodies. When the agent
    answered "URL is https://www.war.gov/ufo/" backed by a successful
    crawl4ai-fetch returning real content, reflect couldn't see that body
    and dismissed the URL as hallucinated based on its own training-data
    priors.

    Post-redesign, the per-attempt transcript is in the evidence and the
    crawl result body appears verbatim. This test verifies that — it does
    NOT assert a specific verdict (LLM nondeterminism out of scope); it
    asserts what reflect actually sees."""
    from db import models as db

    sid = db.create_session(title="war.gov regression")
    db.add_message(sid, "user", "find the website that has the UFO disclosure images from the US government")
    db.add_message(sid, "scout", '{"type":"scout.done","attempt":1}')
    db.add_message(
        sid,
        "assistant",
        "I'll search, then crawl the top result.",
        tool_calls=json.dumps([{"name": "crawl4ai-fetch", "arguments": '{"url": "https://www.war.gov/ufo/"}'}]),
    )
    real_page_body = (
        "PURSUE: Presidential Unsealing and Reporting System for UAP Encounters. "
        "Official disclosure images are available below. " + ("LOREM " * 100)
    )
    db.add_message(sid, "tool", real_page_body)
    db.add_message(sid, "assistant", "Done. The URL is https://www.war.gov/ufo/.")

    # Capture what evidence reaches the LLM. Don't care what verdict it picks.
    mock_llm_client.responses = [
        ChatResponse(
            content=json.dumps({"verdict": "pass", "reasoning": "URL backed by fetched content"}),
            tool_calls=None,
            usage=TokenUsage(10, 5, 15),
            model="test",
            provider="fake",
            finish_reason="stop",
        )
    ]
    await reflect_on_session(sid)

    user_content = mock_llm_client.calls[-1]["messages"][-1]["content"]
    # The crawl result body must appear verbatim — this is the change that
    # lets reflect verify URL claims against real evidence.
    assert "PURSUE: Presidential Unsealing and Reporting System" in user_content
    # The fetch's URL argument must appear so reflect can match the answer to the call.
    assert "https://www.war.gov/ufo/" in user_content
    # Section ordering: USER REQUEST appears before the transcript header.
    assert user_content.index("USER REQUEST") < user_content.index("ATTEMPT TRANSCRIPT")


def test_recent_termination_reasons_helper():
    """db.recent_termination_reasons returns most-recent reasons newest-first."""
    from db import models as db

    sid = db.create_session(title="Helper Test")
    # Manually insert state-log rows.
    from db.database import connect_sessions

    with connect_sessions() as conn:
        for i, reason in enumerate(["complete", "round_ceiling", "round_ceiling"], start=1):
            conn.execute(
                """INSERT INTO session_state_log
                   (session_id, turn_id, retry_index, compaction_count,
                    from_state, to_state, reason, termination_reason,
                    reflect_count, eval_count, timestamp_ms)
                   VALUES (?, 1, 0, 0, 'processing', 'finalizing', 'loop-complete', ?, 0, 0, ?)""",
                (sid, reason, 1_700_000_000_000 + i),
            )
        conn.commit()

    out = db.recent_termination_reasons(sid, limit=3)
    # Newest-first
    assert out[0] == "round_ceiling"
    assert out[1] == "round_ceiling"
    assert out[2] == "complete"
