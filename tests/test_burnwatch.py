"""Tests for the fallback-burn watch + cost_estimate pricing plumbing.

The watch encodes the 2026-08-19 silent-reroute signature (primary provider
wedged → every call billed to the fallback tier) as a standing snooze check.
Watch-only: it mints a notification, never touches routing.
"""

from __future__ import annotations

from core.llm.burnwatch import evaluate_fallback_burn
from core.llm.stream_ladder import estimate_cost


def _rows(primary_tokens: int, fallback_tokens: int) -> list[dict]:
    return [
        {"model": "local-27b", "provider": "openai", "total": primary_tokens, "calls": 10},
        {"model": "paid-remote", "provider": "openrouter", "total": fallback_tokens, "calls": 4},
    ]


def test_burn_fires_on_dominant_fallback_share():
    finding = evaluate_fallback_burn(_rows(10_000, 90_000), "paid-remote", 0.25, 50_000)
    assert finding is not None
    assert finding["model"] == "paid-remote"
    assert finding["share"] == 0.9
    assert finding["tokens"] == 90_000 and finding["total_tokens"] == 100_000
    assert finding["calls"] == 4


def test_burn_quiet_below_share_threshold():
    assert evaluate_fallback_burn(_rows(90_000, 10_000), "paid-remote", 0.25, 50_000) is None


def test_burn_quiet_below_volume_floor():
    """A quiet day where the only traffic happened to fail over is noise,
    not the incident signature."""
    assert evaluate_fallback_burn(_rows(1_000, 9_000), "paid-remote", 0.25, 50_000) is None


def test_burn_disabled_by_config():
    rows = _rows(0, 100_000)
    assert evaluate_fallback_burn(rows, "", 0.25, 0) is None  # no fallback model
    assert evaluate_fallback_burn(rows, "paid-remote", 0, 0) is None  # share=0 disables


def test_burn_zero_fallback_usage_is_quiet():
    assert evaluate_fallback_burn(_rows(100_000, 0), "paid-remote", 0.25, 50_000) is None


# ---------------------------------------------------------------------------
# cost_estimate pricing
# ---------------------------------------------------------------------------


def test_estimate_cost_priced_model(monkeypatch):
    monkeypatch.setattr("config.settings.model_prices", {"paid-remote": {"in": 3.0, "out": 15.0}})
    # 1M prompt at $3 + 100k completion at $15 = 3.0 + 1.5
    assert estimate_cost("paid-remote", 1_000_000, 100_000) == 4.5


def test_estimate_cost_unpriced_model_stays_null(monkeypatch):
    monkeypatch.setattr("config.settings.model_prices", {"paid-remote": {"in": 3.0, "out": 15.0}})
    assert estimate_cost("local-27b", 1_000_000, 100_000) is None
    monkeypatch.setattr("config.settings.model_prices", {})
    assert estimate_cost("paid-remote", 1_000_000, 100_000) is None


def test_estimate_cost_tolerates_junk_config(monkeypatch):
    monkeypatch.setattr("config.settings.model_prices", {"m": "not-a-dict", "n": {"in": "abc"}})
    assert estimate_cost("m", 1000, 1000) is None
    assert estimate_cost("n", 1000, 1000) is None
