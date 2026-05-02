"""SSRF protection in the web extension — `_validate_url`, `_is_blocked_host`,
and the self-loopback carve-out.

Regression: in network mode, browsing/fetching the harness's own port over
loopback was blocked as SSRF, so the agent could not test workspace files it
just wrote (session 444e33b3968e). The fix carves out the harness's own
`localhost:<port>` while keeping co-tenant loopback ports blocked.
"""

from __future__ import annotations

import pytest

import core.extensions.web as web


@pytest.fixture
def network_mode(monkeypatch):
    """Simulate network mode: loopback normally blocked."""
    monkeypatch.setattr(web.settings, "network_enabled", True, raising=False)
    monkeypatch.setattr(web.settings, "port", 8090, raising=False)


@pytest.fixture
def localhost_mode(monkeypatch):
    """Simulate localhost (single-user) mode: loopback allowed."""
    monkeypatch.setattr(web.settings, "network_enabled", False, raising=False)
    monkeypatch.setattr(web.settings, "port", 8090, raising=False)


def test_self_loopback_allowed_in_network_mode(network_mode):
    """Harness's own port over loopback is reachable even when network mode
    is on — the agent owns this server, no privilege escalation."""
    url = web._validate_url("https://localhost:8090/workspace/index.html")
    assert url == "https://localhost:8090/workspace/index.html"


def test_self_loopback_allows_127_0_0_1(network_mode):
    url = web._validate_url("http://127.0.0.1:8090/workspace/file.html")
    assert url.endswith("/workspace/file.html")


def test_self_loopback_allows_ipv6(network_mode):
    url = web._validate_url("http://[::1]:8090/workspace/file.html")
    assert "[::1]:8090" in url


def test_other_loopback_port_still_blocked_in_network_mode(network_mode):
    """A different port on loopback could be a co-tenant service —
    must stay blocked under network mode."""
    with pytest.raises(ValueError, match="resolves to a private/internal address"):
        web._validate_url("http://localhost:9999/admin")


def test_other_loopback_127_port_still_blocked(network_mode):
    with pytest.raises(ValueError, match="resolves to a private/internal address"):
        web._validate_url("http://127.0.0.1:9999/admin")


def test_loopback_no_port_blocked_in_network_mode(network_mode):
    """URL with no explicit port (→ 80/443) is not the harness's port and
    must remain blocked in network mode."""
    with pytest.raises(ValueError, match="resolves to a private/internal address"):
        web._validate_url("http://localhost/foo")


def test_private_rfc1918_still_blocked_in_network_mode(network_mode):
    with pytest.raises(ValueError, match="resolves to a private/internal address"):
        web._validate_url("http://10.0.0.5/foo")


def test_metadata_endpoint_blocked_in_network_mode(network_mode):
    with pytest.raises(ValueError, match="resolves to a private/internal address"):
        web._validate_url("http://169.254.169.254/latest/meta-data/")


def test_loopback_allowed_in_localhost_mode(localhost_mode):
    """In single-user localhost mode, all loopback ports are reachable.
    Callers pass allow_loopback=_loopback_allowed() — simulate that here."""
    assert web._loopback_allowed() is True
    url = web._validate_url(
        "http://localhost:9999/foo",
        allow_loopback=web._loopback_allowed(),
    )
    assert url == "http://localhost:9999/foo"


def test_is_self_loopback_helper_matches_only_own_port():
    """Direct unit test of the helper — port match is the whole gate."""
    import core.extensions.web as web_mod

    orig_port = getattr(web_mod.settings, "port", 8090)
    try:
        web_mod.settings.port = 8090
        assert web_mod._is_self_loopback("localhost", 8090) is True
        assert web_mod._is_self_loopback("127.0.0.1", 8090) is True
        assert web_mod._is_self_loopback("::1", 8090) is True
        assert web_mod._is_self_loopback("localhost", 9999) is False
        assert web_mod._is_self_loopback("localhost", None) is False
        assert web_mod._is_self_loopback("example.com", 8090) is False
        # Lookalike public domain must not match
        assert web_mod._is_self_loopback("localhost.example.com", 8090) is False
    finally:
        web_mod.settings.port = orig_port
