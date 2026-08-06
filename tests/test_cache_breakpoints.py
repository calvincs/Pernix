"""Pernix — Prompt-cache breakpoints for Anthropic via OpenRouter (plan 1b).

compile_context returns the static-prefix boundary; attach_cache_breakpoints
splits the lead system message into cache_control parts for anthropic/*
models on the OpenRouter path only, flattens stale parts on model switches,
and nothing survives sanitize_for_fallback into the Ollama path.
"""

import pytest

from core.context.compiler import attach_cache_breakpoints, compile_context
from db import models as db


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    monkeypatch.setattr("config.settings.openrouter_cache_control", True)


def _msgs(content="STATIC-BYTES\n\nSCOUT-BYTES"):
    return [{"role": "system", "content": content}, {"role": "user", "content": "hi"}]


# ---------------------------------------------------------------------------
# Boundary offsets from compile_context
# ---------------------------------------------------------------------------


def test_compile_context_returns_static_prefix_boundary():
    sid = db.create_session(title="c")
    db.add_message(sid, "user", "hello")
    scout_text = "[SCOUT REPORT MARKER] plan things"
    payload = compile_context(sid, scout_report_text=scout_text)
    head = payload.messages[0]["content"]
    n = payload.static_prefix_chars
    assert 0 < n <= len(head)
    # Everything through the boundary is the static prefix; scout text sits
    # strictly after it.
    assert scout_text not in head[:n]
    assert scout_text in head[n:]


def test_boundary_stable_across_turn_variants():
    """Same session, different scout text → identical static prefix bytes."""
    sid = db.create_session(title="c")
    db.add_message(sid, "user", "hello")
    p1 = compile_context(sid, scout_report_text="[SCOUT A]")
    p2 = compile_context(sid, scout_report_text="[SCOUT B] totally different length")
    assert p1.static_prefix_chars == p2.static_prefix_chars
    assert p1.messages[0]["content"][: p1.static_prefix_chars] == p2.messages[0]["content"][: p2.static_prefix_chars]


# ---------------------------------------------------------------------------
# attach_cache_breakpoints
# ---------------------------------------------------------------------------


def test_attach_splits_anthropic_openrouter():
    msgs = _msgs()
    out = attach_cache_breakpoints(msgs, "anthropic/claude-sonnet-4", "openrouter", 12)
    parts = out[0]["content"]
    assert isinstance(parts, list) and len(parts) == 2
    assert parts[0]["text"] == "STATIC-BYTES"
    assert parts[0]["cache_control"] == {"type": "ephemeral"}
    assert parts[1]["text"] == "\n\nSCOUT-BYTES"
    assert parts[1]["cache_control"] == {"type": "ephemeral"}
    # Reassembly is byte-exact and the rest of the list is untouched.
    assert "".join(p["text"] for p in parts) == msgs[0]["content"]
    assert out[1] is msgs[1]
    # Original input not mutated.
    assert isinstance(msgs[0]["content"], str)


def test_attach_noop_wrong_model_provider_or_flag(monkeypatch):
    msgs = _msgs()
    assert attach_cache_breakpoints(msgs, "openai/gpt-5", "openrouter", 12) is msgs
    assert attach_cache_breakpoints(msgs, "anthropic/claude-sonnet-4", "openai", 12) is msgs
    assert attach_cache_breakpoints(msgs, "anthropic/claude-sonnet-4", "openrouter", 0) is msgs
    assert attach_cache_breakpoints(msgs, "anthropic/claude-sonnet-4", "openrouter", 10_000) is msgs
    monkeypatch.setattr("config.settings.openrouter_cache_control", False)
    assert attach_cache_breakpoints(msgs, "anthropic/claude-sonnet-4", "openrouter", 12) is msgs


def test_attach_idempotent_and_flattens_on_model_switch():
    msgs = _msgs()
    parted = attach_cache_breakpoints(msgs, "anthropic/claude-sonnet-4", "openrouter", 12)
    # Second call for the same model: unchanged.
    again = attach_cache_breakpoints(parted, "anthropic/claude-sonnet-4", "openrouter", 12)
    assert again is parted
    # Fallback to a non-Anthropic model: parts flatten back to the exact string.
    flat = attach_cache_breakpoints(parted, "openai/gpt-5", "openai", 12)
    assert flat[0]["content"] == msgs[0]["content"]


def test_attach_skips_non_system_head():
    msgs = [{"role": "user", "content": "no system head"}]
    assert attach_cache_breakpoints(msgs, "anthropic/claude-3", "openrouter", 5) is msgs


# ---------------------------------------------------------------------------
# Fallback sanitization: nothing reaches Ollama
# ---------------------------------------------------------------------------


def test_sanitize_for_fallback_strips_cache_parts():
    from core.llm.router import sanitize_for_fallback

    parted = attach_cache_breakpoints(_msgs(), "anthropic/claude-sonnet-4", "openrouter", 12)
    clean = sanitize_for_fallback(parted)
    assert isinstance(clean[0]["content"], str)
    assert "cache_control" not in str(clean)
    assert "STATIC-BYTES" in clean[0]["content"]


# ---------------------------------------------------------------------------
# Token counting tolerates parts (pre-existing vision path hardening)
# ---------------------------------------------------------------------------


def test_estimator_counts_parts_content():
    from core.context.tokens import get_estimator

    parted = attach_cache_breakpoints(_msgs(), "anthropic/claude-sonnet-4", "openrouter", 12)
    n = get_estimator().count_message(parted[0])
    assert n > 0


def test_is_pinned_tolerates_list_content():
    from core.context.compiler import _is_pinned

    assert _is_pinned({"role": "user", "content": [{"type": "text", "text": "x"}]}) is False
    assert _is_pinned({"role": "system", "content": [{"type": "text", "text": "x"}]}) is True
