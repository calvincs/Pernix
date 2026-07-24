"""Tests for the network-mode auth middleware in api/app.py."""

import pytest

from api.app import _AuthMiddleware, _extract_token


class _Recorder:
    """Collects ASGI sends and records whether the inner app was reached."""

    def __init__(self):
        self.messages: list[dict] = []
        self.reached_inner = False

    async def inner(self, scope, receive, send):
        self.reached_inner = True

    async def send(self, message):
        self.messages.append(message)

    @property
    def status(self) -> int | None:
        for m in self.messages:
            if m.get("type") == "http.response.start":
                return m["status"]
        return None


def _scope(path="/api/sessions", client=("10.0.0.5", 5000), headers=None, query=b""):
    return {
        "type": "http",
        "method": "GET",
        "path": path,
        "client": client,
        "headers": headers or [],
        "query_string": query,
    }


@pytest.fixture
def network_mode(monkeypatch):
    monkeypatch.setattr("config.settings.network_enabled", True)
    monkeypatch.setattr("config.settings.auth_token", "correct-horse-battery-staple")
    monkeypatch.setattr("config.settings.trust_local_requests", True)


async def _call(scope):
    rec = _Recorder()
    await _AuthMiddleware(rec.inner)(scope, lambda: None, rec.send)
    return rec


# ---------------------------------------------------------------------------
# Token checking
# ---------------------------------------------------------------------------


async def test_valid_token_passes(network_mode):
    headers = [(b"authorization", b"Bearer correct-horse-battery-staple")]
    rec = await _call(_scope(headers=headers))
    assert rec.reached_inner


async def test_wrong_token_rejected(network_mode):
    headers = [(b"authorization", b"Bearer wrong")]
    rec = await _call(_scope(headers=headers))
    assert not rec.reached_inner
    assert rec.status == 401


async def test_missing_token_rejected(network_mode):
    rec = await _call(_scope())
    assert not rec.reached_inner
    assert rec.status == 401


async def test_token_prefix_is_not_accepted(network_mode):
    """A prefix of the real token must not authenticate."""
    headers = [(b"authorization", b"Bearer correct-horse")]
    rec = await _call(_scope(headers=headers))
    assert not rec.reached_inner
    assert rec.status == 401


async def test_comparison_is_constant_time():
    """A plain != leaks the secret's prefix through comparison timing."""
    import ast
    import pathlib

    src = pathlib.Path("api/app.py").read_text()
    assert "compare_digest" in src, "token comparison must be constant-time"
    # And the old direct-inequality form must be gone.
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Compare) and any(isinstance(op, ast.NotEq | ast.Eq) for op in node.ops):
            seg = ast.unparse(node)
            assert "auth_token" not in seg, f"non-constant-time token compare: {seg}"


# ---------------------------------------------------------------------------
# Loopback bypass
# ---------------------------------------------------------------------------


async def test_loopback_bypasses_by_default(network_mode):
    for host in ("127.0.0.1", "::1"):
        rec = await _call(_scope(client=(host, 5000)))
        assert rec.reached_inner, f"{host} should bypass when trust_local_requests is on"


async def test_loopback_challenged_when_untrusted(network_mode, monkeypatch):
    """Behind a reverse proxy every request arrives from loopback; without
    this switch they would all skip the token."""
    monkeypatch.setattr("config.settings.trust_local_requests", False)
    for host in ("127.0.0.1", "::1"):
        rec = await _call(_scope(client=(host, 5000)))
        assert not rec.reached_inner, f"{host} must be challenged when untrusted"
        assert rec.status == 401


async def test_untrusted_loopback_still_accepts_a_valid_token(network_mode, monkeypatch):
    monkeypatch.setattr("config.settings.trust_local_requests", False)
    headers = [(b"authorization", b"Bearer correct-horse-battery-staple")]
    rec = await _call(_scope(client=("127.0.0.1", 5000), headers=headers))
    assert rec.reached_inner


# ---------------------------------------------------------------------------
# Unchanged behaviour
# ---------------------------------------------------------------------------


async def test_public_paths_need_no_token(network_mode):
    for path in ("/", "/api/health", "/sw.js", "/static/js/app.js", "/favicon.ico"):
        rec = await _call(_scope(path=path))
        assert rec.reached_inner, f"{path} should stay public"


async def test_auth_disabled_outside_network_mode(monkeypatch):
    monkeypatch.setattr("config.settings.network_enabled", False)
    monkeypatch.setattr("config.settings.auth_token", "tok")
    rec = await _call(_scope())
    assert rec.reached_inner


async def test_options_preflight_passes(network_mode):
    scope = _scope()
    scope["method"] = "OPTIONS"
    rec = await _call(scope)
    assert rec.reached_inner


async def test_cookie_and_query_token_still_work(network_mode):
    """EventSource cannot set headers and QR onboarding uses ?token=."""
    cookie = [(b"cookie", b"pernix_auth=correct-horse-battery-staple")]
    assert (await _call(_scope(headers=cookie))).reached_inner

    query = b"token=correct-horse-battery-staple"
    assert (await _call(_scope(query=query))).reached_inner


def test_extract_token_prefers_header_then_cookie_then_query():
    assert _extract_token(_scope(headers=[(b"authorization", b"Bearer h")])) == "h"
    assert _extract_token(_scope(headers=[(b"cookie", b"pernix_auth=c")])) == "c"
    assert _extract_token(_scope(query=b"token=q")) == "q"
    assert _extract_token(_scope()) is None
