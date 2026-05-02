"""Tests for the Tavily/DuckDuckGo fallback chain in the web extension.

Regressions for the 2026-04-27 ai-tech-daily-brief workflow run:
- Tavily over-limit silently fell through to DDG; the operator only saw 21
  hours of WARNING logs and no actionable signal.
- DDG returns empty results (not exceptions) under concurrent-IP throttling;
  the wrapper presented this as "No results found", indistinguishable from a
  real empty query.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_alert_state():
    """Reset the one-shot alert flag between tests so each test sees a fresh
    'first occurrence'. Otherwise the second test would silently no-op."""
    import core.extensions.web as web

    web._tavily_alerted = False
    web._ddg_empty_history.clear()
    yield
    web._tavily_alerted = False
    web._ddg_empty_history.clear()


def test_tavily_limit_emits_one_shot_alert(monkeypatch):
    """First Tavily-over-limit fall-through fires an operator notification.
    Subsequent ones must NOT spam — that's the whole point of one-shot."""
    import core.extensions.web as web

    # Simulate Tavily key set + raise _TavilyLimitError on each call
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")

    def fake_tavily(*a, **kw):
        raise web._TavilyLimitError("plan limit exceeded")

    # DDG returns a normal empty result, not a rate-limit error
    def fake_ddg(*a, **kw):
        return "No results found for: anything"

    monkeypatch.setattr(web, "_tavily_search", fake_tavily)
    monkeypatch.setattr(web, "_ddg_search_with_retry", fake_ddg)

    notifications: list[tuple] = []
    monkeypatch.setattr(
        web,
        "_emit_backend_alert",
        lambda title, body, urgency: notifications.append((title, body, urgency)),
    )

    # First call: alert should fire
    web.search_web("query 1")
    assert len(notifications) == 1
    title, _body, urgency = notifications[0]
    assert "Tavily" in title and "limit" in title.lower()
    assert urgency == "normal"

    # Second call: no extra alert (one-shot)
    web.search_web("query 2")
    assert len(notifications) == 1, "alert fired again — one-shot guard broken"


def test_tavily_invalid_key_emits_separate_high_alert(monkeypatch):
    """Invalid key is a different fault than over-limit — fires its own
    high-urgency alert (operator must update the key)."""
    import core.extensions.web as web

    monkeypatch.setenv("TAVILY_API_KEY", "bogus")

    def fake_tavily(*a, **kw):
        raise web._TavilyKeyError("invalid")

    monkeypatch.setattr(web, "_tavily_search", fake_tavily)
    monkeypatch.setattr(web, "_ddg_search_with_retry", lambda *a, **kw: "No results found for: anything")

    notifications: list[tuple] = []
    monkeypatch.setattr(
        web,
        "_emit_backend_alert",
        lambda title, body, urgency: notifications.append((title, body, urgency)),
    )

    web.search_web("anything")
    assert len(notifications) == 1
    title, _body, urgency = notifications[0]
    assert "rejected" in title.lower() or "invalid" in title.lower()
    assert urgency == "high"


def test_ddg_burst_empty_raises_rate_limit_error(monkeypatch):
    """Four+ empty DDG results within 60s is the signature of concurrent
    rate-limiting (real backend would distribute results across queries).
    The wrapper must raise _DDGRateLimitError so the agent sees a distinct
    error, not 'No results found'."""
    import core.extensions.web as web

    # Force the empty-history threshold to 4 (matches default) and use a
    # tiny window for the test.
    monkeypatch.setattr(web, "_DDG_RATE_LIMIT_WINDOW_S", 60.0)
    monkeypatch.setattr(web, "_DDG_RATE_LIMIT_THRESHOLD", 4)

    # Stub DDGS to always return [].
    class _FakeDDGS:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def text(self, query, max_results=5):
            return iter([])

    import sys
    import types

    fake_module = types.ModuleType("ddgs")
    fake_module.DDGS = _FakeDDGS
    exc_module = types.ModuleType("ddgs.exceptions")
    exc_module.RatelimitException = type("RatelimitException", (Exception,), {})
    exc_module.TimeoutException = type("TimeoutException", (Exception,), {})
    fake_module.exceptions = exc_module
    monkeypatch.setitem(sys.modules, "ddgs", fake_module)
    monkeypatch.setitem(sys.modules, "ddgs.exceptions", exc_module)

    # Below threshold: returns "No results found"
    for i in range(3):
        out = web._ddg_search_with_retry(f"query {i}", 5)
        assert "No results found" in out, out

    # Crossing threshold: raises rate-limit error
    with pytest.raises(web._DDGRateLimitError) as exc_info:
        web._ddg_search_with_retry("query 4", 5)
    assert "throttling" in str(exc_info.value).lower() or "empty" in str(exc_info.value).lower()


def test_ddg_rate_limit_surfaces_through_search_web_wrapper(monkeypatch):
    """End-to-end: a _DDGRateLimitError from _ddg_search_with_retry should
    bubble out of search_web as a distinct error string the agent can act
    on (not 'No results found' / not generic 'Web search failed')."""
    import core.extensions.web as web

    # Tavily not configured: fallback path is DDG-only
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    def boom(*a, **kw):
        raise web._DDGRateLimitError("4 empty results in 60s")

    monkeypatch.setattr(web, "_ddg_search_with_retry", boom)

    out = web.search_web("query")
    assert "rate-limited" in out.lower(), out
    # And the message must hint at recovery (different tool, slower pace)
    assert "different tool" in out.lower() or "slow down" in out.lower(), out


def test_ddg_empty_for_genuinely_no_results_does_not_alert(monkeypatch):
    """Single empty result is normal — many DDG queries genuinely have no
    hits. Threshold is what distinguishes 'rate-limited' from 'nothing
    matches'. Don't false-positive on the first empty result."""
    import core.extensions.web as web

    class _FakeDDGS:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def text(self, query, max_results=5):
            return iter([])

    import sys
    import types

    fake_module = types.ModuleType("ddgs")
    fake_module.DDGS = _FakeDDGS
    exc_module = types.ModuleType("ddgs.exceptions")
    exc_module.RatelimitException = type("R", (Exception,), {})
    exc_module.TimeoutException = type("T", (Exception,), {})
    fake_module.exceptions = exc_module
    monkeypatch.setitem(sys.modules, "ddgs", fake_module)
    monkeypatch.setitem(sys.modules, "ddgs.exceptions", exc_module)

    out = web._ddg_search_with_retry("a very specific query", 5)
    assert "No results found" in out
    # No exception, no alert fired
