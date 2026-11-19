"""The agent's web tools could reach things the address guard was meant to
keep them away from, in two ways.

The harness's own server was exempt from the guard on its own port, so the
agent could look at workspace files it had written. The exemption covered the
whole port. The server trusts loopback clients without a token, and its API
hands out the access token, so one http_get (no approval prompt) could read
the token and a second could send it anywhere. The exemption now covers only
/workspace/ paths, judged on the path a server would actually see (dot
segments, encoded dots, doubled slashes and backslashes are refused), in
network mode and localhost mode alike. The browser enforces the same rule on
every request a page makes.

The private-address check had gaps and a race. It missed the shared address
space carrier NAT and overlay VPNs use, multicast, IPv4 wrapped in IPv6 and
the NAT64 prefix. And it resolved a name to check it, after which httpx or
Chromium resolved the name AGAIN to connect: a name that answers public first
and private second passed the check and reached the private address. All
traffic now goes through a local egress proxy that resolves each name once,
tests every address, and connects to the vetted address by IP literal.
"""

from __future__ import annotations

import http.server
import socket
import threading

import pytest

import core.extensions.web as web
from core.extensions.web import browser_session, egress_proxy

OWN = 8090
TOKEN_URL = f"http://localhost:{OWN}/api/settings/auth-token"


@pytest.fixture(params=["network", "localhost"])
def mode(request, monkeypatch):
    monkeypatch.setattr(web.settings, "network_enabled", request.param == "network", raising=False)
    monkeypatch.setattr(web.settings, "port", OWN, raising=False)
    return request.param


# --- the own port: /workspace/ only ------------------------------------------


@pytest.mark.parametrize("scheme", ["http", "https"])
@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_the_own_api_is_refused_and_workspace_files_are_not(mode, scheme, host):
    base = f"{scheme}://{host}:{OWN}"
    with pytest.raises(ValueError, match=r"only /workspace/ paths|Only /workspace/ paths"):
        web._validate_url(f"{base}/api/settings/auth-token", allow_loopback=web._loopback_allowed())
    assert web._validate_url(f"{base}/workspace/x.html", allow_loopback=web._loopback_allowed()).endswith(
        "/workspace/x.html"
    )


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/",
        "/workspace",
        "/static/app.js",
        "/openapi.json",
        "/workspace/../api/settings/auth-token",
        "/workspace/%2e%2e/api/settings/auth-token",
        "/workspace/%2E%2E/api/settings/auth-token",
        "/workspace/..%2fapi/settings/auth-token",
        "/workspace/%252e%252e/api/settings/auth-token",
        "/workspace/./../api",
        "/workspace//api/settings/auth-token",
        "/workspace\\..\\api\\settings\\auth-token",
        "/workspace/%5c..%5capi",
        "/workspace/a%00.html",
        "//workspace/x.html",
    ],
)
def test_traversal_and_encoding_tricks_are_refused(mode, path):
    with pytest.raises(ValueError, match="own server"):
        web._validate_url(f"http://localhost:{OWN}{path}", allow_loopback=web._loopback_allowed())


@pytest.mark.parametrize("host", ["127.1", "0.0.0.0", "127.0.0.1."])
def test_another_name_for_this_machine_gets_no_exemption(mode, host):
    """127.1 and 0.0.0.0 reach the same server; they are not given the direct
    route, so they are refused even at a /workspace/ path."""
    with pytest.raises(ValueError, match="own server|private/internal"):
        web._validate_url(f"http://{host}:{OWN}/api/settings/auth-token", allow_loopback=web._loopback_allowed())


def test_the_default_port_counts_as_the_own_port(monkeypatch):
    monkeypatch.setattr(web.settings, "network_enabled", False, raising=False)
    monkeypatch.setattr(web.settings, "port", 443, raising=False)
    with pytest.raises(ValueError, match="own server"):
        web._validate_url("https://localhost/api/settings/auth-token", allow_loopback=True)


def test_the_refusal_tells_the_agent_what_is_reachable(mode):
    with pytest.raises(ValueError) as e:
        web._validate_url(TOKEN_URL, allow_loopback=web._loopback_allowed())
    assert "/workspace/" in str(e.value) and "own" in str(e.value)


def _client_returning(responses, seen):
    class _Resp:
        def __init__(self, status, headers):
            self.status_code, self.headers = status, headers
            self.is_redirect = 300 <= status < 400
            self.encoding = "utf-8"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        def iter_bytes(self):
            yield b"body"

    class _C:
        def __init__(self, *a, **k):
            seen.append(k)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def stream(self, method, url, timeout=None):
            seen[-1]["url"] = url
            return _Resp(*responses.pop(0))

    return _C


def test_a_redirect_to_the_own_api_is_refused(mode, monkeypatch):
    real = web._is_blocked_host
    monkeypatch.setattr(
        web, "_is_blocked_host", lambda h, allow_loopback=False: h != "example.com" and real(h, allow_loopback)
    )
    monkeypatch.setattr(egress_proxy, "ensure_started", lambda: "http://127.0.0.1:1")
    seen: list = []
    monkeypatch.setattr("httpx.Client", _client_returning([(302, {"location": TOKEN_URL})], seen))
    text, status = web.http_get("https://example.com/start")
    assert status["fetch_status"] == "refused" and "own server" in text
    assert [s["url"] for s in seen] == ["https://example.com/start"]  # the token URL was never requested
    assert seen[0]["proxy"] == "http://127.0.0.1:1"  # and the first hop went through the proxy


def test_a_workspace_hop_goes_direct_and_everything_else_through_the_proxy(mode, monkeypatch):
    monkeypatch.setattr(egress_proxy, "ensure_started", lambda: "http://127.0.0.1:1")
    seen: list = []
    ws = f"http://localhost:{OWN}/workspace/page.html"
    monkeypatch.setattr("httpx.Client", _client_returning([(200, {"content-type": "text/html"})], seen))
    _text, status = web.http_get(ws)
    assert status["fetch_status"] == "ok" and seen[0]["proxy"] is None


def test_no_proxy_means_no_fetch(monkeypatch):
    def _fail():
        raise egress_proxy.EgressProxyError("the egress proxy could not start: test")

    monkeypatch.setattr(egress_proxy, "ensure_started", _fail)
    monkeypatch.setattr(web, "_validate_url", lambda url, allow_loopback=False: url)
    monkeypatch.setattr("httpx.Client", lambda *a, **k: pytest.fail("connected without the proxy"))
    text, status = web.http_get("https://example.com/")
    assert status["fetch_status"] == "error" and "could not start" in text


def test_the_browser_check_aborts_own_api_requests_and_keeps_workspace_ones(mode):
    allow = web._loopback_allowed()
    for url in (
        f"http://localhost:{OWN}/api/settings/auth-token",
        f"http://127.0.0.1:{OWN}/",
        f"http://127.0.0.1:{OWN}/static/app.js",
        f"ws://localhost:{OWN}/api/sessions/stream",
        f"http://localhost:{OWN}/workspace/%2e%2e/api/x",
    ):
        assert browser_session._host_blocked(None, url, allow), url
    assert not browser_session._host_blocked(None, f"http://localhost:{OWN}/workspace/app.js", allow)
    assert not browser_session._host_blocked(None, f"ws://127.0.0.1:{OWN}/workspace/sock", allow)


def test_browser_contexts_send_nothing_direct(monkeypatch):
    """The own port goes through the proxy too. A request route is not shown
    redirect hops, so a direct route to the own port let a page reach the API
    by bouncing a request off a redirect (seen in a real browser)."""
    monkeypatch.setattr(web.settings, "port", OWN, raising=False)
    assert web._browser_proxy("http://127.0.0.1:5555") == {"server": "http://127.0.0.1:5555"}


# --- the address test ---------------------------------------------------------


@pytest.mark.parametrize(
    "addr",
    [
        "100.64.0.1",  # shared address space (carrier NAT, overlay VPNs)
        "100.127.255.254",
        "224.0.0.1",  # multicast
        "239.255.255.250",
        "ff02::1",
        "::ffff:10.0.0.1",  # IPv4-mapped private
        "::ffff:169.254.169.254",
        "::ffff:127.0.0.1",
        "64:ff9b::a00:1",  # NAT64 of 10.0.0.1
        "64:ff9b::5db8:d822",  # NAT64 prefix even for a public v4
        "0.0.0.0",
        "::",
        "10.0.0.1",
        "172.17.0.1",
        "192.168.0.10",
        "169.254.169.254",
        "fe80::1",
        "fc00::1",
        "192.0.2.1",  # documentation
        "198.18.0.1",  # benchmarking
        "240.0.0.1",  # reserved
        "127.0.0.1",
    ],
)
def test_non_public_addresses_are_blocked(addr):
    assert web._ip_blocked(addr) is True


@pytest.mark.parametrize("addr", ["93.184.216.34", "2606:2800:220:1::1", "::ffff:93.184.216.34"])
def test_a_public_address_is_allowed(addr):
    # 93.184.216.34 / 2606:2800:220:1:: are the addresses example.com long used.
    assert web._ip_blocked(addr) is False


def test_loopback_is_the_callers_choice_and_nothing_else_is():
    assert web._ip_blocked("127.0.0.1", allow_loopback=True) is False
    assert web._ip_blocked("::1", allow_loopback=True) is False
    assert web._ip_blocked("::ffff:127.0.0.1", allow_loopback=True) is False
    assert web._ip_blocked("10.0.0.1", allow_loopback=True) is True
    assert web._ip_blocked("100.64.0.1", allow_loopback=True) is True


# --- the egress proxy ---------------------------------------------------------

PUBLIC = "93.184.216.34"


class _Origin(http.server.BaseHTTPRequestHandler):
    """Stands in for a public server; it listens on loopback because that is
    the only address a test can listen on."""

    def do_GET(self):
        self.server.seen.append((self.path, self.headers.get("Connection"), self.headers.get("Proxy-Connection")))
        body = b"origin says hello"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def origin():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Origin)
    srv.seen = []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def proxy(monkeypatch):
    monkeypatch.setattr(web.settings, "port", OWN, raising=False)
    url = egress_proxy.ensure_started()
    yield int(url.rsplit(":", 1)[1])
    egress_proxy.stop()


@pytest.fixture
def dns(monkeypatch):
    """A resolver stub for *.example.com names; every other name resolves
    for real. `answers[name]` is a list of answers, one per call (the last
    one repeats), each a list of IP strings or an exception."""
    real = socket.getaddrinfo
    answers: dict[str, list] = {}
    calls: dict[str, int] = {}

    def fake(host, port, *a, **k):
        if isinstance(host, str) and host.endswith(".example.com"):
            n = calls.get(host, 0)
            calls[host] = n + 1
            seq = answers[host]
            ans = seq[min(n, len(seq) - 1)]
            if isinstance(ans, Exception):
                raise ans
            out = []
            for ip in ans:
                fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
                addr = (ip, int(port or 0)) if fam == socket.AF_INET else (ip, int(port or 0), 0, 0)
                out.append((fam, socket.SOCK_STREAM, 6, "", addr))
            return out
        return real(host, port, *a, **k)

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    fake.answers, fake.calls = answers, calls
    return fake


def _exchange(proxy_port: int, *messages: bytes) -> bytes:
    """Send each message after the previous reply's headers; read to EOF."""
    with socket.create_connection(("127.0.0.1", proxy_port), timeout=10) as s:
        data = b""
        for i, msg in enumerate(messages):
            s.sendall(msg)
            if i < len(messages) - 1:
                while b"\r\n\r\n" not in data:
                    chunk = s.recv(65536)
                    if not chunk:
                        return data
                    data += chunk
        while True:
            chunk = s.recv(65536)
            if not chunk:
                return data
            data += chunk


def _connect(host, dport):
    return f"CONNECT {host}:{dport} HTTP/1.1\r\nHost: {host}:{dport}\r\n\r\n".encode()


def _plain(host, dport, path="/hello"):
    return (
        f"GET http://{host}:{dport}{path} HTTP/1.1\r\nHost: {host}:{dport}\r\n"
        "Proxy-Connection: keep-alive\r\nProxy-Authorization: Basic eDp5\r\n\r\n"
    ).encode()


@pytest.mark.parametrize("answer", [["10.0.0.1"], ["100.64.0.9"], [PUBLIC, "10.0.0.1"], ["10.0.0.1", PUBLIC]])
def test_the_proxy_refuses_a_name_with_any_private_answer(proxy, dns, answer):
    dns.answers["target.example.com"] = [answer]
    for msg in (_connect("target.example.com", 443), _plain("target.example.com", 80)):
        reply = _exchange(proxy, msg)
        assert reply.startswith(b"HTTP/1.1 403 "), reply
        assert b"private or internal" in reply


def test_the_proxy_refuses_when_resolution_fails(proxy, dns):
    dns.answers["gone.example.com"] = [socket.gaierror(socket.EAI_NONAME, "Name or service not known")]
    for msg in (_connect("gone.example.com", 443), _plain("gone.example.com", 80)):
        assert _exchange(proxy, msg).startswith(b"HTTP/1.1 403 ")


def test_the_proxy_refuses_loopback_unless_allowed_and_the_own_port_always(proxy, monkeypatch, origin):
    port = origin.server_address[1]
    monkeypatch.setattr(web, "_loopback_allowed", lambda: False)
    assert _exchange(proxy, _connect("127.0.0.1", port)).startswith(b"HTTP/1.1 403 ")
    monkeypatch.setattr(web, "_loopback_allowed", lambda: True)
    reply = _exchange(proxy, _connect("127.0.0.1", OWN))
    assert reply.startswith(b"HTTP/1.1 403 ") and b"/workspace/" in reply
    assert _exchange(proxy, _plain("localhost", OWN, "/api/settings/auth-token")).startswith(b"HTTP/1.1 403 ")
    assert _exchange(proxy, _connect("127.0.0.1", proxy)).startswith(b"HTTP/1.1 403 ")


@pytest.mark.parametrize("allowed", [True, False])
def test_the_proxy_serves_only_workspace_paths_on_the_own_port(proxy, monkeypatch, origin, allowed):
    """Each request to the own port is judged on its own URL, whatever sent
    it there: a redirect hop arrives here as one more request."""
    own = origin.server_address[1]
    monkeypatch.setattr(web.settings, "port", own, raising=False)
    monkeypatch.setattr(web, "_loopback_allowed", lambda: allowed)
    reply = _exchange(proxy, _plain("localhost", own, "/workspace/page.html"))
    assert b" 200 " in reply.split(b"\r\n", 1)[0] and b"origin says hello" in reply
    for path in ("/api/settings/auth-token", "/", "/static/app.js", "/workspace/../api/x", "/workspace/%2e%2e/api/x"):
        reply = _exchange(proxy, _plain("localhost", own, path))
        assert reply.startswith(b"HTTP/1.1 403 ") and b"/workspace/" in reply, path
    assert origin.seen == [("/workspace/page.html", "close", None)]
    # Another name for this machine does not get the workspace route either.
    assert _exchange(proxy, _plain("0.0.0.0", own, "/workspace/page.html")).startswith(b"HTTP/1.1 403 ")


@pytest.mark.parametrize("form", ["connect", "plain"])
def test_the_proxy_connects_to_the_address_it_checked(proxy, dns, origin, monkeypatch, form):
    """A rebinding name: the first answer passes the check, every later answer
    is private. The proxy must resolve once and connect to that first answer.
    Loopback stands in for the public first answer (allowed here), since a
    test can only listen there."""
    monkeypatch.setattr(web, "_loopback_allowed", lambda: True)
    port = origin.server_address[1]
    dns.answers["rebind.example.com"] = [["127.0.0.1"], ["10.0.0.9"]]
    if form == "connect":
        inner = b"GET /hello HTTP/1.1\r\nHost: rebind.example.com\r\nConnection: close\r\n\r\n"
        reply = _exchange(proxy, _connect("rebind.example.com", port), inner)
        assert reply.startswith(b"HTTP/1.1 200 Connection established")
    else:
        reply = _exchange(proxy, _plain("rebind.example.com", port))
        # Rewritten to origin form, proxy headers dropped, one request per connection.
        assert origin.seen == [("/hello", "close", None)]
    assert b"origin says hello" in reply
    assert dns.calls["rebind.example.com"] == 1


def test_http_get_is_stopped_by_the_proxy_when_the_early_check_was_fooled(proxy, dns, monkeypatch):
    """The early check saw a public answer; the connect-time answer is private."""
    monkeypatch.setattr(web.settings, "network_enabled", True, raising=False)
    monkeypatch.setattr(web, "_is_blocked_host", lambda h, allow_loopback=False: False)
    dns.answers["rebind.example.com"] = [["10.0.0.7"]]
    text, status = web.http_get("http://rebind.example.com/")
    assert status["fetch_status"] == "refused" and "private or internal" in text
    text, status = web.http_get("https://rebind.example.com/")
    assert status["fetch_status"] == "refused" and "egress guard" in text


def test_the_proxy_survives_junk_and_oversized_headers(proxy):
    assert _exchange(proxy, b"NONSENSE\r\n\r\n").startswith(b"HTTP/1.1 400 ")
    assert _exchange(proxy, b"GET /origin-form HTTP/1.1\r\nHost: x\r\n\r\n").startswith(b"HTTP/1.1 400 ")
    huge = b"GET http://a.example.com/ HTTP/1.1\r\nX: " + b"a" * (70 * 1024) + b"\r\n\r\n"
    try:
        reply = _exchange(proxy, huge)
    except ConnectionResetError:
        reply = b"HTTP/1.1 431 (reset before the reply was read)"  # closed with input unread
    assert reply.startswith(b"HTTP/1.1 431 ")
    # Still serving afterwards.
    assert _exchange(proxy, b"NONSENSE\r\n\r\n").startswith(b"HTTP/1.1 400 ")


def test_the_proxy_starts_once_and_stops_cleanly(monkeypatch):
    a = egress_proxy.ensure_started()
    assert egress_proxy.ensure_started() == a
    thread = egress_proxy._thread
    assert thread.daemon
    egress_proxy.stop()
    assert not thread.is_alive()
