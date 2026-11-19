"""http_get reads the server's own /workspace/ pages over HTTPS by trusting
exactly the server's own certificate file. That worked for the built-in
self-signed certificate, which is its own issuer. A custom certificate signed
by a private CA is a leaf: its issuer is not in the file, and OpenSSL refuses
a non-root trust anchor unless partial chains are allowed. Every own-page
fetch on such an install failed with "unable to get local issuer
certificate", and the link-digest canary only passed when the agent went
around http_get and read the file from disk.

The pinned context now allows a partial chain. It still trusts only that one
certificate and still checks the hostname.
"""

from __future__ import annotations

import http.server
import ssl
import subprocess
import threading

import pytest


def _run(*args, cwd):
    subprocess.run(list(args), check=True, capture_output=True, cwd=cwd)


@pytest.fixture
def ca_signed_cert(tmp_path, monkeypatch):
    d = tmp_path / "pki"
    d.mkdir()
    _run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-keyout", "ca.key", "-out", "ca.crt", "-subj", "/CN=Test Private CA", cwd=d)  # fmt: skip
    _run("openssl", "req", "-newkey", "rsa:2048", "-nodes", "-keyout", "leaf.key", "-out", "leaf.csr", "-subj", "/CN=leaf", cwd=d)  # fmt: skip
    (d / "ext.cnf").write_text("subjectAltName=DNS:localhost,IP:127.0.0.1\n", encoding="utf-8")
    _run("openssl", "x509", "-req", "-in", "leaf.csr", "-CA", "ca.crt", "-CAkey", "ca.key", "-CAcreateserial", "-days", "1", "-extfile", "ext.cnf", "-out", "leaf.crt", cwd=d)  # fmt: skip
    monkeypatch.setattr("config.settings.ssl_mode", "custom")
    monkeypatch.setattr("config.settings.ssl_cert_path", str(d / "leaf.crt"))
    monkeypatch.setattr("config.settings.network_enabled", True)
    return d / "leaf.crt", d / "leaf.key"


def _serve(root, crt, key):
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(root), **k)  # noqa: E731
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(crt), str(key))
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_http_get_reads_an_own_page_behind_a_ca_signed_leaf(ca_signed_cert, monkeypatch, tmp_path):
    from core.extensions.web import http_get

    crt, key = ca_signed_cert
    root = tmp_path / "www"
    (root / "workspace").mkdir(parents=True)
    (root / "workspace" / "article.html").write_text("<h1>Leaf Title 7713</h1>", encoding="utf-8")
    server = _serve(root, crt, key)
    try:
        port = server.server_address[1]
        monkeypatch.setattr("config.settings.port", port)
        text, status = http_get(f"https://localhost:{port}/workspace/article.html")
        assert status["fetch_status"] in ("ok", "capped"), text
        assert "Leaf Title 7713" in text
    finally:
        server.shutdown()
        server.server_close()


def test_the_pinned_context_trusts_only_the_own_certificate(ca_signed_cert, monkeypatch, tmp_path):
    """A different certificate for the same name is still refused."""
    import httpx

    from core.extensions.web import _own_tls_verify

    other = tmp_path / "other"
    other.mkdir()
    _run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-keyout", "o.key", "-out", "o.crt", "-subj", "/CN=other", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1", cwd=other)  # fmt: skip
    root = tmp_path / "www"
    root.mkdir()
    server = _serve(root, other / "o.crt", other / "o.key")
    try:
        port = server.server_address[1]
        monkeypatch.setattr("config.settings.port", port)
        ctx = _own_tls_verify(f"https://localhost:{port}/workspace/x")
        assert isinstance(ctx, ssl.SSLContext) and ctx.check_hostname
        with pytest.raises(httpx.ConnectError), httpx.Client(verify=ctx, proxy=None, trust_env=False) as client:
            client.get(f"https://localhost:{port}/workspace/x")
    finally:
        server.shutdown()
        server.server_close()
