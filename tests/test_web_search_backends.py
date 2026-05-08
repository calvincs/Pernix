"""Tests for the Tavily backend and search_web gating in the web extension.

DuckDuckGo fallback was removed. search_web now requires TAVILY_API_KEY; without
one it returns a user-actionable setup hint instead of silently degrading.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_alert_state():
    """Reset the one-shot alert flag between tests so each test sees a fresh
    'first occurrence'. Otherwise the second test would silently no-op."""
    import core.extensions.web as web

    web._tavily_alerted = False
    yield
    web._tavily_alerted = False


def test_tavily_limit_emits_one_shot_alert(monkeypatch):
    """First Tavily-over-limit fault fires an operator notification.
    Subsequent ones must NOT spam — that's the whole point of one-shot."""
    import core.extensions.web as web

    monkeypatch.setenv("TAVILY_API_KEY", "test-key")

    def fake_tavily(*a, **kw):
        raise web._TavilyLimitError("plan limit exceeded")

    monkeypatch.setattr(web, "_tavily_search", fake_tavily)

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

    notifications: list[tuple] = []
    monkeypatch.setattr(
        web,
        "_emit_backend_alert",
        lambda title, body, urgency: notifications.append((title, body, urgency)),
    )

    out = web.search_web("anything")
    assert len(notifications) == 1
    title, _body, urgency = notifications[0]
    assert "rejected" in title.lower() or "invalid" in title.lower()
    assert urgency == "high"
    # Return value must be an actionable error, not a silent empty
    assert "Error" in out
    assert "Tavily" in out


def test_tavily_limit_returns_actionable_error(monkeypatch):
    """When the Tavily limit is hit, search_web returns an error string that
    tells the user what to do — not a generic failure."""
    import core.extensions.web as web

    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    monkeypatch.setattr(web, "_tavily_search", lambda *a, **kw: (_ for _ in ()).throw(web._TavilyLimitError("over")))
    monkeypatch.setattr(web, "_emit_backend_alert", lambda *a, **kw: None)

    out = web.search_web("query")
    assert "Error" in out
    assert "limit" in out.lower() or "Tavily" in out


def test_search_web_no_tavily_key_returns_setup_hint(monkeypatch):
    """Without TAVILY_API_KEY, search_web must return a user-actionable setup
    hint — not a generic error and not a silent empty result."""
    import core.extensions.web as web

    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    out = web.search_web("best pizza in Chicago")
    assert "Error" in out
    assert "Tavily" in out
    assert "Settings" in out  # points user to the configuration UI


def test_search_web_no_tavily_key_does_not_call_tavily(monkeypatch):
    """Tavily must never be invoked when the key is absent — the key gate
    must short-circuit before the network call."""
    import core.extensions.web as web

    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    calls: list = []
    monkeypatch.setattr(web, "_tavily_search", lambda *a, **kw: calls.append(1) or "ok")
    web.search_web("anything")
    assert calls == [], "Tavily must not be called without a key"


def test_search_web_disabled_returns_error(monkeypatch):
    """web_search_enabled=False must return the disabled error before any key
    check — the setting takes priority."""
    import core.extensions.web as web
    from config import settings

    monkeypatch.setattr(settings, "web_search_enabled", False)
    out = web.search_web("query")
    assert "Error" in out
    assert "disabled" in out.lower()
