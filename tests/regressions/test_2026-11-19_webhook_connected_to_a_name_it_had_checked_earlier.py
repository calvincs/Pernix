"""The notification webhook checked its URL, then connected to it separately.

`_send_webhook` ran the URL through `_validate_url`, which resolves the name
and refuses private addresses, and then `_post` handed the URL to urllib,
which resolved the name AGAIN to connect. A name that answers public to the
check and private to the connect (DNS rebinding) got the event payload
delivered to an internal address. urllib also followed redirects anywhere.

The POST now goes through the egress proxy, which resolves once and connects
to the address it vetted; if the proxy cannot start, nothing is sent; and
redirects are not followed at all (a 3xx is a failed delivery).
"""

from __future__ import annotations

import asyncio
import http.server
import logging
import socket
import threading

import pytest

import core.extensions.web as web
import core.notify as notify
from core.extensions.web import egress_proxy

OWN = 8090


class _Hook(http.server.BaseHTTPRequestHandler):
    """Stands in for a webhook receiver (public or internal: a test can only
    listen on loopback, so which one it plays is up to the resolver stub)."""

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.server.seen.append((self.path, self.headers.get("Content-Type"), body))
        redirect = getattr(self.server, "redirect_to", None)
        if redirect:
            # 302: the code urllib used to follow for a POST (as a GET).
            self.send_response(302)
            self.send_header("Location", redirect)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        self.server.seen.append((self.path, None, b""))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


def _server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Hook)
    srv.seen = []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def hook():
    srv = _server()
    yield srv
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def internal():
    srv = _server()
    yield srv
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def proxy(monkeypatch):
    monkeypatch.setattr(web.settings, "port", OWN, raising=False)
    egress_proxy.ensure_started()
    yield
    egress_proxy.stop()


@pytest.fixture
def dns(monkeypatch):
    """Resolver stub for *.example.com: `answers[name]` is a list of answers,
    one per call (the last repeats). Other names resolve for real."""
    real = socket.getaddrinfo
    answers: dict[str, list] = {}
    calls: dict[str, int] = {}

    def fake(host, port, *a, **k):
        if isinstance(host, str) and host.endswith(".example.com"):
            n = calls.get(host, 0)
            calls[host] = n + 1
            seq = answers[host]
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, int(port or 0))) for ip in seq[min(n, len(seq) - 1)]
            ]
        return real(host, port, *a, **k)

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    fake.answers, fake.calls = answers, calls
    return fake


@pytest.fixture
def send(monkeypatch):
    """Run _send_webhook for url; the early check is the real one."""

    def _run(url: str) -> None:
        monkeypatch.setattr(notify.settings, "notify_webhook_url", url, raising=False)
        monkeypatch.setattr(notify.settings, "notify_webhook_timeout", 10, raising=False)
        asyncio.run(notify.NotificationDispatcher()._send_webhook({"type": "dialog.question", "question": "q?"}))

    return _run


def test_a_webhook_to_a_public_name_is_delivered_through_the_proxy(proxy, dns, hook, send, monkeypatch, caplog):
    # Loopback stands in for the public answer at connect time; the early
    # check is told the name is public.
    monkeypatch.setattr(web, "_loopback_allowed", lambda: True)
    monkeypatch.setattr(web, "_is_blocked_host", lambda h, allow_loopback=False: False)
    dns.answers["hooks.example.com"] = [["127.0.0.1"]]
    with caplog.at_level(logging.WARNING, logger="pernix.notify"):
        send(f"http://hooks.example.com:{hook.server_address[1]}/notify")
    assert not caplog.records, caplog.text
    assert len(hook.seen) == 1
    path, ctype, body = hook.seen[0]
    assert path == "/notify" and ctype == "application/json" and b'"dialog.question"' in body
    assert dns.calls["hooks.example.com"] == 1  # resolved by the proxy, once


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_a_name_that_turns_private_at_connect_time_is_refused(proxy, dns, internal, send, monkeypatch, scheme, caplog):
    """The early check saw a public answer; the connect-time answer is private."""
    monkeypatch.setattr(web, "_is_blocked_host", lambda h, allow_loopback=False: False)
    dns.answers["rebind.example.com"] = [["10.0.0.9"]]
    with caplog.at_level(logging.WARNING, logger="pernix.notify"):
        send(f"{scheme}://rebind.example.com:{internal.server_address[1]}/notify")
    assert internal.seen == []
    assert "not sent" in caplog.text and "egress guard" in caplog.text


def test_no_proxy_means_no_send(dns, send, monkeypatch, caplog):
    def _fail():
        raise egress_proxy.EgressProxyError("the egress proxy could not start: test")

    monkeypatch.setattr(egress_proxy, "ensure_started", _fail)
    monkeypatch.setattr(web, "_is_blocked_host", lambda h, allow_loopback=False: False)
    monkeypatch.setattr("httpx.Client", lambda *a, **k: pytest.fail("connected without the proxy"))
    with caplog.at_level(logging.WARNING, logger="pernix.notify"):
        send("https://hooks.example.com/notify")
    assert "could not start" in caplog.text and "not sent" in caplog.text


def test_a_redirect_is_not_followed_to_an_internal_address(proxy, dns, hook, internal, send, monkeypatch, caplog):
    monkeypatch.setattr(web, "_loopback_allowed", lambda: True)
    monkeypatch.setattr(web, "_is_blocked_host", lambda h, allow_loopback=False: False)
    dns.answers["hooks.example.com"] = [["127.0.0.1"]]
    hook.redirect_to = f"http://127.0.0.1:{internal.server_address[1]}/latest/meta-data/"
    with caplog.at_level(logging.WARNING, logger="pernix.notify"):
        send(f"http://hooks.example.com:{hook.server_address[1]}/notify")
    assert len(hook.seen) == 1
    assert internal.seen == []  # no second request, anywhere
    assert "redirects are not followed" in caplog.text


def test_environment_proxies_are_not_used(monkeypatch):
    monkeypatch.setattr(egress_proxy, "ensure_started", lambda: "http://127.0.0.1:1")
    seen: list = []

    class _C:
        def __init__(self, *a, **k):
            seen.append(k)
            raise RuntimeError("stop here")

    monkeypatch.setattr("httpx.Client", _C)
    with pytest.raises(RuntimeError, match="stop here"):
        notify.NotificationDispatcher._post("https://hooks.example.com/", b"{}")
    assert seen == [{"proxy": "http://127.0.0.1:1", "follow_redirects": False, "trust_env": False, "timeout": 10}]
