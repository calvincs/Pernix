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


# ---------------------------------------------------------------------------
# Internal-knowledge augmentation (search_web prepends memory + session hits)
# ---------------------------------------------------------------------------


@pytest.fixture
def _stub_tavily(monkeypatch):
    """Force search_web's external path to deterministic text so we can assert
    augmentation behavior without relying on a real API call."""
    import core.extensions.web as web

    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    monkeypatch.setattr(web, "_tavily_search", lambda *a, **kw: "STUB-WEB-RESULT")
    monkeypatch.setattr(web, "_emit_backend_alert", lambda *a, **kw: None)
    return web


def _stub_internal_recall(monkeypatch, *, memory_text="MEM-HIT", session_text="SESS-HIT", strong=True):
    """Replace internal_recall so we control exactly what augmentation sees."""
    import core.memory.internal_recall as ir_mod

    class _StubRecall:
        def __init__(self):
            self.memory_text = memory_text
            self.memory_seen_footer = ""
            self.session_text = session_text
            self.memory_strong = strong
            self.session_strong = strong
            self.queried = True

    monkeypatch.setattr(ir_mod, "internal_recall", lambda *a, **kw: _StubRecall())


def test_default_prepends_internal_knowledge_block(_stub_tavily, monkeypatch):
    """consult_memory defaults to True; internal-knowledge block must appear
    BEFORE the Tavily output."""
    _stub_internal_recall(monkeypatch)
    out = _stub_tavily.search_web("anything")
    assert "INTERNAL KNOWLEDGE" in out
    assert "WEB SEARCH RESULTS" in out
    assert out.index("INTERNAL KNOWLEDGE") < out.index("WEB SEARCH RESULTS")
    assert "STUB-WEB-RESULT" in out
    assert "MEM-HIT" in out


def test_consult_memory_false_suppresses_block(_stub_tavily, monkeypatch):
    """consult_memory=False short-circuits the internal recall entirely."""
    called: list = []

    import core.memory.internal_recall as ir_mod

    monkeypatch.setattr(ir_mod, "internal_recall", lambda *a, **kw: called.append(1) or None)

    out = _stub_tavily.search_web("anything", consult_memory=False)
    assert "INTERNAL KNOWLEDGE" not in out
    assert "WEB SEARCH RESULTS" not in out  # no merged-output header either
    assert out == "STUB-WEB-RESULT"
    assert called == [], "internal_recall must NOT be called when consult_memory=False"


def test_internal_recall_failure_does_not_break_search_web(_stub_tavily, monkeypatch):
    """If internal_recall raises, search_web must still return Tavily output."""
    import core.memory.internal_recall as ir_mod

    def boom(*a, **kw):
        raise RuntimeError("recall blew up")

    monkeypatch.setattr(ir_mod, "internal_recall", boom)

    out = _stub_tavily.search_web("anything")
    # Augmentation skipped → just the raw stub result, no error surfaced
    assert out == "STUB-WEB-RESULT"


# ---------------------------------------------------------------------------
# Registration decoupling — browse_web must register independently of
# web_search_enabled and the Tavily key.
# ---------------------------------------------------------------------------


def test_browse_web_registers_without_search_web_or_tavily(monkeypatch):
    """browse_web is a separate capability from search_web. With
    web_search_enabled=False and no TAVILY_API_KEY, browser_enabled=True
    must still register browse_web (and http_get), and search_web must NOT
    register. The agent can browse pages without a Tavily subscription."""
    import core.extensions.web as web
    from config import settings as _settings
    from core.tools.registry import ToolRegistry

    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr(_settings, "web_search_enabled", False)
    monkeypatch.setattr(_settings, "browser_enabled", True)

    reg = ToolRegistry()
    web.register(reg)

    assert reg.get("browse_web") is not None, "browse_web must register when browser_enabled=True"
    assert reg.get("http_get") is not None, "http_get must always register"
    assert reg.get("search_web") is None, "search_web must NOT register when web_search_enabled=False"


def test_browse_web_omitted_when_browser_disabled(monkeypatch):
    """browser_enabled=False suppresses browse_web even if web_search is on."""
    import core.extensions.web as web
    from config import settings as _settings
    from core.tools.registry import ToolRegistry

    monkeypatch.setattr(_settings, "web_search_enabled", True)
    monkeypatch.setattr(_settings, "browser_enabled", False)

    reg = ToolRegistry()
    web.register(reg)

    assert reg.get("browse_web") is None
    assert reg.get("search_web") is not None


def test_browser_enabled_defaults_to_true():
    """Default install ships with the Playwright tool active. Pin the
    default so future config refactors don't silently flip it back."""
    from config import Settings

    assert Settings().browser_enabled is True


def test_error_paths_still_trip_was_error_detection(monkeypatch):
    """executor.py classifies a tool result as was_error when it
    `startswith("Error:")`. The augmentation must not break that — error
    returns are NOT wrapped in the internal-knowledge block."""
    import core.extensions.web as web
    import core.memory.internal_recall as ir_mod

    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    monkeypatch.setattr(web, "_emit_backend_alert", lambda *a, **kw: None)
    _stub_internal_recall(monkeypatch)  # would prepend if reached

    # Invalid-key error path
    monkeypatch.setattr(web, "_tavily_search", lambda *a, **kw: (_ for _ in ()).throw(web._TavilyKeyError("bad")))
    out = web.search_web("anything")
    assert out.startswith("Error:"), "error result must remain a leading Error: string"
    assert "INTERNAL KNOWLEDGE" not in out, "error returns must not be wrapped with augmentation"

    # Generic exception path
    monkeypatch.setattr(web, "_tavily_search", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    out = web.search_web("anything")
    assert out.startswith("Error:")
    assert "INTERNAL KNOWLEDGE" not in out


def test_empty_internal_recall_produces_no_header(_stub_tavily, monkeypatch):
    """When recall finds nothing, search_web returns raw Tavily output — no
    bare 'INTERNAL KNOWLEDGE' header sitting on top of empty content."""
    _stub_internal_recall(monkeypatch, memory_text="", session_text="", strong=False)
    out = _stub_tavily.search_web("anything")
    assert "INTERNAL KNOWLEDGE" not in out
    assert out == "STUB-WEB-RESULT"


def test_session_id_threaded_through_context(_stub_tavily, monkeypatch):
    """search_web pulls session_id from its _context and forwards it to
    internal_recall so the caller's own session is excluded from cross-session
    hits."""
    import core.memory.internal_recall as ir_mod

    captured: dict = {}

    def fake_recall(query, current_session_id=None, **_kw):
        captured["query"] = query
        captured["sid"] = current_session_id
        # Return a stub-shaped object
        return type(
            "R",
            (),
            {"memory_text": "", "session_text": "", "memory_strong": False, "session_strong": False, "queried": True},
        )()

    monkeypatch.setattr(ir_mod, "internal_recall", fake_recall)

    _stub_tavily.search_web("hello", _context={"session_id": "sess-xyz"})
    assert captured["query"] == "hello"
    assert captured["sid"] == "sess-xyz"
