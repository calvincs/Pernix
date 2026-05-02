"""Mid-turn harness nudge tests.

These pin down the bot-detection / SSRF / 4xx-5xx → crawl4ai-fetch hint
mapping so future regex tweaks don't silently disable the nudge.
"""

from __future__ import annotations

import pytest

from core.harness.nudges import CRAWL4AI_HINT, evaluate


def test_bot_detection_fires_on_browse_web():
    fired: set[str] = set()
    body = (
        "<html><body><h1>Just a moment...</h1>" "<p>Checking your browser before accessing the site.</p></body></html>"
    )
    hint = evaluate("browse_web", body, fired)
    assert hint == CRAWL4AI_HINT
    assert "bot_detection_wall" in fired


def test_bot_detection_fires_only_once_per_turn():
    fired: set[str] = set()
    body = "Please verify you are human before continuing."
    first = evaluate("browse_web", body, fired)
    second = evaluate("browse_web", body, fired)
    assert first == CRAWL4AI_HINT
    assert second is None  # one-shot per turn


def test_http_403_429_503_fires():
    fired: set[str] = set()
    assert evaluate("http_get", "HTTP/1.1 429 Too Many Requests", fired) == CRAWL4AI_HINT
    fired.clear()
    assert evaluate("http_get", "HTTP/1.1 503 Service Unavailable", fired) == CRAWL4AI_HINT
    fired.clear()
    assert evaluate("http_get", "HTTP/1.1 403 Forbidden", fired) == CRAWL4AI_HINT


def test_ssrf_block_message_fires_the_nudge():
    fired: set[str] = set()
    # Exact text from core/extensions/web/__init__.py:446 — the user's
    # turn 3 spiral hit this twice on developers.roku.com.
    msg = "Error: Blocked: developers.roku.com resolves to a private/internal address"
    assert evaluate("browse_web", msg, fired) == CRAWL4AI_HINT


def test_ssrf_block_on_loopback_does_not_fire_crawl4ai_nudge():
    """crawl4ai uses a remote egress IP and cannot reach loopback —
    suggesting it for localhost blocks misleads the agent (session 444e33b3968e)."""
    for host in (
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "127.0.0.1",
        "127.1.2.3",
        "::1",
    ):
        fired: set[str] = set()
        msg = f"Error: Blocked: {host} resolves to a private/internal address"
        assert evaluate("browse_web", msg, fired) is None, f"crawl4ai hint must not fire for loopback host {host!r}"


def test_ssrf_block_on_localhost_lookalike_still_fires():
    """A hostname starting with 'localhost' but not actually loopback
    (e.g. localhost.example.com) is a public domain — nudge should fire."""
    fired: set[str] = set()
    msg = "Error: Blocked: localhost.example.com resolves to a private/internal address"
    assert evaluate("browse_web", msg, fired) == CRAWL4AI_HINT


def test_unrelated_content_does_not_fire():
    fired: set[str] = set()
    assert evaluate("browse_web", "<html><body>Some article body</body></html>", fired) is None
    assert evaluate("http_get", 'HTTP/1.1 200 OK\n\n{"ok": true}', fired) is None
    assert fired == set()


def test_404_does_not_fire():
    """A 404 is usually a wrong URL, not a block — must not nudge."""
    fired: set[str] = set()
    assert evaluate("http_get", "HTTP/1.1 404 Not Found", fired) is None


def test_nudge_only_for_relevant_tools():
    """bash output that mentions 403 should NOT trigger the http nudge."""
    fired: set[str] = set()
    assert evaluate("bash", "echo 403; exit 0", fired) is None
    assert evaluate("file_read", "the number 503 appears here", fired) is None


def test_empty_input_safe():
    fired: set[str] = set()
    assert evaluate("browse_web", "", fired) is None
    assert evaluate("http_get", None or "", fired) is None
