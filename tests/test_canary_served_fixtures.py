"""Served fixtures, own-certificate TLS and declared tools for canaries (3.2).

link-digest needs to fetch a page without the internet. The runner publishes
a canary's `serve:` files under <workspace_dir>/.canary-serve/<token>/ for one
run and points {{SERVE_BASE}} at Pernix's own /workspace route; http_get
verifies that one host:port against Pernix's own certificate (never
verify=False); and `tools:` may add read-only web tools to the run's
allowlist — only from a fixed set.
"""

from __future__ import annotations

import http.server
import shutil
import ssl
import subprocess
import threading
from pathlib import Path

import pytest

from tests.test_canary_isolation_hardening import _fake_manager


def _write_canary(base: Path, name: str, extra: str) -> Path:
    d = base / name
    d.mkdir(parents=True)
    md = d / "CANARY.md"
    md.write_text(
        f"---\nname: {name}\nprompt: |\n  Fetch {{{{SERVE_BASE}}}}/article.html\n"
        "gates:\n  - name: g\n    command: grep -q '{{SERVE_BASE}}' summary.md\n"
        f"files:\n  article.html: '<h1>Hi</h1>'\n  notes.txt: keep\n{extra}---\nbody\n",
        encoding="utf-8",
    )
    return md


# ---------------------------------------------------------------------------
# Parsing: serve: and tools:
# ---------------------------------------------------------------------------


def test_serve_and_tools_parse(tmp_path):
    from core.canary.parser import parse_canary_md

    c = parse_canary_md(_write_canary(tmp_path, "web", "serve: [article.html]\ntools: [http_get]\n"))
    assert c.serve == ["article.html"] and c.tools == ["http_get"]


@pytest.mark.parametrize("tool", ["search_web", "bash", "spawn_worker", "remember"])
def test_only_read_only_web_tools_can_be_declared(tmp_path, tool):
    from core.canary.parser import CanaryParseError, parse_canary_md

    with pytest.raises(CanaryParseError, match="cannot be declared"):
        parse_canary_md(_write_canary(tmp_path, "web", f"tools: [{tool}]\n"))


def test_a_serve_entry_must_be_relative(tmp_path):
    from core.canary.parser import CanaryParseError, parse_canary_md

    with pytest.raises(CanaryParseError, match="relative"):
        parse_canary_md(_write_canary(tmp_path, "web", "serve: [../escape.html]\n"))


def test_the_allowlist_grows_only_by_the_declared_tools():
    from core.canary.parser import CanaryDef
    from core.canary.runner import CANARY_TOOL_ALLOWLIST, _run_allowlist

    gate = [{"name": "g", "command": "true"}]
    assert _run_allowlist(CanaryDef(name="plain", prompt="x", gates=gate)) == CANARY_TOOL_ALLOWLIST
    web = _run_allowlist(CanaryDef(name="web", prompt="x", gates=gate, tools=["http_get"]))
    assert web == CANARY_TOOL_ALLOWLIST | {"http_get"}
    assert "browse_web" not in web and "search_web" not in web
    # A CanaryDef built in code (not parsed) still cannot smuggle a tool in.
    sneaky = _run_allowlist(CanaryDef(name="x", prompt="x", gates=gate, tools=["search_web", "remember"]))
    assert sneaky == CANARY_TOOL_ALLOWLIST


def test_machine_written_canaries_never_carry_tools(tmp_path):
    """The create API's structured spec renders a fixed key set."""
    from core.canary.parser import load_canary
    from core.canary.propose import materialize_canary

    spec = {"name": "spec-made", "prompt": "p", "gates": [{"name": "g", "command": "true"}], "tools": ["http_get"]}
    assert materialize_canary(spec, base=tmp_path)[0] == "spec-made"
    assert load_canary("spec-made", base=tmp_path).tools == []


# ---------------------------------------------------------------------------
# The runner publishes, substitutes and cleans up
# ---------------------------------------------------------------------------


async def test_served_files_are_published_substituted_and_removed(monkeypatch, tmp_path):
    from core.canary.parser import parse_canary_md
    from core.canary.runner import run_canary

    workspace = tmp_path / "served-ws"
    workspace.mkdir()
    monkeypatch.setattr("config.settings.workspace_dir", str(workspace))
    monkeypatch.setattr("config.settings.network_enabled", True)
    monkeypatch.setattr("config.settings.port", 8090)
    canary = parse_canary_md(_write_canary(tmp_path / "c", "web", "serve: [article.html]\ntools: [http_get]\n"))

    seen: dict = {}

    def solve(ws: Path, message: str):
        seen["message"] = message
        seen["ws_files"] = sorted(p.name for p in ws.iterdir())
        seen["served"] = sorted(str(p.relative_to(workspace)) for p in workspace.rglob("*") if p.is_file())
        (ws / "summary.md").write_text(message.split("Fetch ", 1)[1], encoding="utf-8")

    mgr = _fake_manager(monkeypatch, solve)
    allowlists = []
    orig_get = mgr.get

    def _get(sid):
        s = orig_get(sid)
        if s is not None and s.tool_allowlist:
            allowlists.append(set(s.tool_allowlist))
        return s

    mgr.get = _get
    result = await run_canary(canary, trigger="manual")

    base = seen["message"].split("Fetch ", 1)[1].strip().rsplit("/", 1)[0]
    assert base.startswith("https://localhost:8090/workspace/.canary-serve/")
    assert "{{SERVE_BASE}}" not in seen["message"]
    token = base.rsplit("/", 1)[1]
    # Served, not seeded: the agent has to fetch the article.
    assert seen["served"] == [f".canary-serve/{token}/article.html"]
    assert seen["ws_files"] == ["notes.txt"]
    # The gate saw the same substitution, so it passed against summary.md.
    assert result.passed, result.gate_results
    assert any("http_get" in a for a in allowlists)
    # Cleaned up after the run.
    assert not (workspace / ".canary-serve" / token).exists()
    assert result.contamination == []


async def test_a_serve_entry_missing_from_files_errors_cleanly(monkeypatch, tmp_path):
    from core.canary.parser import parse_canary_md
    from core.canary.runner import run_canary

    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path / "workspace"))
    canary = parse_canary_md(_write_canary(tmp_path / "c", "web", "serve: [nope.html]\n"))
    _fake_manager(monkeypatch, lambda ws, msg: None)
    result = await run_canary(canary, trigger="manual")
    assert not result.passed and "not among the canary's files" in result.error
    assert not list((tmp_path / "workspace").rglob("*.html"))


def test_serve_base_url_follows_the_server_scheme(monkeypatch):
    from core.canary.runner import serve_base_url

    monkeypatch.setattr("config.settings.port", 9123)
    monkeypatch.setattr("config.settings.network_enabled", False)
    assert serve_base_url("t") == "http://localhost:9123/workspace/.canary-serve/t"
    monkeypatch.setattr("config.settings.network_enabled", True)
    assert serve_base_url("t") == "https://localhost:9123/workspace/.canary-serve/t"


# ---------------------------------------------------------------------------
# Own-certificate TLS: only for Pernix's own host:port
# ---------------------------------------------------------------------------


@pytest.fixture
def own_cert(monkeypatch, tmp_path):
    if not shutil.which("openssl"):
        pytest.skip("openssl not installed")
    crt, key = tmp_path / "self.crt", tmp_path / "self.key"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-sha256",
            "-keyout",
            str(key),
            "-out",
            str(crt),
            "-days",
            "1",
            "-nodes",
            "-subj",
            "/CN=Pernix",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    from core import certs

    monkeypatch.setattr(certs, "SELF_SIGNED_CERT", crt)
    monkeypatch.setattr("config.settings.ssl_mode", "self_signed")
    monkeypatch.setattr("config.settings.network_enabled", True)
    monkeypatch.setattr("config.settings.port", 8090)
    return crt, key


@pytest.mark.parametrize(
    "url",
    [
        "https://localhost:8090/workspace/a.html",
        "https://127.0.0.1:8090/workspace/a.html",
    ],
)
def test_own_host_and_port_verify_against_the_own_certificate(own_cert, url):
    from core.extensions.web import _own_tls_verify

    ctx = _own_tls_verify(url)
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is True


@pytest.mark.parametrize(
    "url",
    [
        "https://localhost:8091/workspace/a.html",  # another local port
        "https://example.com:8090/a.html",  # another host on our port number
        "https://10.0.0.5:8090/a.html",
        "http://localhost:8090/workspace/a.html",  # no TLS at all
        "https://localhost/workspace/a.html",  # default port
    ],
)
def test_every_other_url_keeps_the_default_verification(own_cert, url):
    from core.extensions.web import _own_tls_verify

    assert _own_tls_verify(url) is True


def test_localhost_mode_never_uses_the_own_certificate(own_cert, monkeypatch):
    from core.extensions.web import _own_tls_verify

    monkeypatch.setattr("config.settings.network_enabled", False)
    assert _own_tls_verify("https://localhost:8090/x") is True


def test_a_missing_own_certificate_falls_back_to_the_default(own_cert, monkeypatch, tmp_path):
    from core import certs
    from core.extensions.web import _own_tls_verify

    monkeypatch.setattr(certs, "SELF_SIGNED_CERT", tmp_path / "absent.crt")
    assert _own_tls_verify("https://localhost:8090/x") is True


def test_http_get_reads_a_served_file_over_own_tls(own_cert, monkeypatch, tmp_path):
    """End to end: a real HTTPS server on a loopback port with the own
    certificate, settings.port pointed at it, http_get through the SSRF
    guard (self-loopback carve-out) and the pinned context."""
    from core.extensions.web import http_get

    crt, key = own_cert
    root = tmp_path / "www"
    root.mkdir()
    (root / "article.html").write_text("<h1>Seeded Title 4821</h1>", encoding="utf-8")

    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(root), **k)  # noqa: E731
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(crt), str(key))
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setattr("config.settings.port", port)
        text, status = http_get(f"https://localhost:{port}/article.html")
        assert status["fetch_status"] in ("ok", "capped"), text
        assert "Seeded Title 4821" in text
    finally:
        server.shutdown()
        server.server_close()
