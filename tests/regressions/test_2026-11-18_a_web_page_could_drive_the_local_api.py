"""A web page on any domain could drive the local API.

By default the server listens on loopback with no token: anything that can
reach the port is trusted. A browser keeps other sites away by origin, but a
page on a hostile domain can point its own name at 127.0.0.1 (DNS rebinding).
The browser then counts the local server as that page's own origin and lets
its scripts read and write the whole API, which can run shell commands through
the agent. In network mode the loopback trust let the same request skip the
token on a single-host install.

The Host header still names the hostile domain, and a rebound name is the only
way in: a literal IP address or `localhost` cannot be rebound. With no token in
force, a request whose Host is not `localhost`, an IP literal or a name in
cors_origins is now refused with 403, the page itself included. In network
mode such a request no longer gets the loopback bypass and must show the token.

Separately, the app page could be put in a hidden frame by another site and
its clicks steered (clickjacking). index.html's meta CSP cannot forbid that, so
`/` now sends X-Frame-Options and a frame-ancestors header. Files under
/workspace/ keep their own headers.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from api.app import _AuthMiddleware

TOKEN = "correct-horse-battery-staple"
HOSTILE = "attacker.example.com"


class _Recorder:
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


def _scope(host: str | None, path="/api/sessions", client=("127.0.0.1", 50000), method="GET", extra=None):
    headers = [] if host is None else [(b"host", host.encode())]
    headers += extra or []
    return {
        "type": "http",
        "method": method,
        "path": path,
        "client": client,
        "headers": headers,
        "query_string": b"",
    }


async def _call(scope) -> _Recorder:
    rec = _Recorder()
    await _AuthMiddleware(rec.inner)(scope, lambda: None, rec.send)
    return rec


@pytest.fixture
def localhost_mode(monkeypatch):
    monkeypatch.setattr("config.settings.network_enabled", False)
    monkeypatch.setattr("config.settings.auth_token", "")
    monkeypatch.setattr("config.settings.cors_origins", [])


@pytest.fixture
def network_mode(monkeypatch):
    monkeypatch.setattr("config.settings.network_enabled", True)
    monkeypatch.setattr("config.settings.auth_token", TOKEN)
    monkeypatch.setattr("config.settings.trust_local_requests", True)
    monkeypatch.setattr("config.settings.cors_origins", [])


@pytest.fixture
def real_app():
    from api.app import app

    return app


# ---------------------------------------------------------------------------
# Localhost mode: no token, so the Host check is the guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/api/sessions", "/", "/static/js/sse.js"])
async def test_a_rebound_name_is_refused_everywhere(localhost_mode, real_app, path):
    async with AsyncClient(transport=ASGITransport(app=real_app), base_url=f"http://{HOSTILE}:8000") as c:
        resp = await c.get(path)
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert HOSTILE in detail
    assert "cors_origins" in detail


async def test_preflight_is_not_a_way_around_it(localhost_mode):
    rec = await _call(_scope(f"{HOSTILE}:8000", method="OPTIONS"))
    assert not rec.reached_inner
    assert rec.status == 403


@pytest.mark.parametrize(
    "host",
    [
        "localhost:8000",
        "LOCALHOST:8000",
        "localhost.:8000",
        "localhost",
        "127.0.0.1:8000",
        "[::1]:8000",
        "192.0.2.10:8000",
    ],
)
async def test_names_that_cannot_be_rebound_pass(localhost_mode, host):
    rec = await _call(_scope(host))
    assert rec.reached_inner, host


async def test_a_host_listed_in_cors_origins_passes(localhost_mode, monkeypatch):
    monkeypatch.setattr("config.settings.cors_origins", ["https://pernix.example.org:8443"])
    assert (await _call(_scope("pernix.example.org:8443"))).reached_inner
    assert (await _call(_scope("PERNIX.example.org"))).reached_inner
    # Listing one name does not open the door to others.
    assert (await _call(_scope(f"{HOSTILE}:8000"))).status == 403


@pytest.mark.parametrize("host", [None, "", "[::1:8000"])
async def test_a_missing_or_garbled_host_is_refused(localhost_mode, host):
    rec = await _call(_scope(host))
    assert not rec.reached_inner
    assert rec.status == 403


async def test_a_real_api_route_still_answers_on_localhost(localhost_mode, real_app):
    async with AsyncClient(transport=ASGITransport(app=real_app), base_url="http://localhost:8000") as c:
        resp = await c.get("/api/health")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Network mode: an untrusted Host loses the loopback bypass, nothing more
# ---------------------------------------------------------------------------


async def test_loopback_with_a_rebound_name_must_show_the_token(network_mode):
    rec = await _call(_scope(f"{HOSTILE}:8000"))
    assert not rec.reached_inner
    assert rec.status == 401


async def test_loopback_with_a_trusted_host_still_bypasses(network_mode):
    for host in ("localhost:8000", "127.0.0.1:8000", "[::1]:8000"):
        rec = await _call(_scope(host))
        assert rec.reached_inner, host


async def test_an_untrusted_host_with_the_token_passes(network_mode):
    auth = [(b"authorization", f"Bearer {TOKEN}".encode())]
    rec = await _call(_scope(f"{HOSTILE}:8000", extra=auth))
    assert rec.reached_inner


async def test_public_paths_and_preflight_are_unchanged_in_network_mode(network_mode):
    remote = ("198.51.100.7", 50000)
    for path in ("/", "/static/js/app.js", "/api/health"):
        assert (await _call(_scope(f"{HOSTILE}:8000", path=path, client=remote))).reached_inner, path
    assert (await _call(_scope(f"{HOSTILE}:8000", method="OPTIONS", client=remote))).reached_inner


# ---------------------------------------------------------------------------
# Clickjacking: only the app page refuses to be framed
# ---------------------------------------------------------------------------


async def test_the_app_page_refuses_to_be_framed(localhost_mode, real_app):
    async with AsyncClient(transport=ASGITransport(app=real_app), base_url="http://localhost:8000") as c:
        resp = await c.get("/")
    assert resp.status_code == 200
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["content-security-policy"] == "frame-ancestors 'none'"
    assert resp.headers["x-content-type-options"] == "nosniff"
    # The cross-origin isolation headers are still there.
    assert resp.headers["cross-origin-opener-policy"] == "same-origin"


async def test_workspace_files_are_not_given_the_frame_ban(localhost_mode, real_app):
    from pathlib import Path

    from config import settings

    ws = Path(settings.workspace_dir)
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "note.txt").write_text("hello", encoding="utf-8")
    async with AsyncClient(transport=ASGITransport(app=real_app), base_url="http://localhost:8000") as c:
        resp = await c.get("/workspace/note.txt")
    assert resp.status_code == 200
    assert "x-frame-options" not in resp.headers
    assert "frame-ancestors" not in resp.headers.get("content-security-policy", "")
