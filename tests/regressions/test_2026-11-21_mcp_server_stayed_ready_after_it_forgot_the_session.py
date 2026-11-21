"""A hosted MCP server forgot its session and the client never reconnected.

Box, 2026-11-21: a remote MCP server answered every request with 404 and the
JSON-RPC error "Unknown mcp-session-id". The connection still read "ready".
The five-minute tool refresh failed each time and only logged a warning, so
the server stayed unusable until someone reloaded it by hand. A tool call had
to fail three times before the call breaker reconnected.

The same log showed a second fault: the HTTP client wrote each request URL in
full, and this service takes its API key in the query string.

Now a "session is gone" answer reconnects at once, from the refresh and from
a tool call, and outbound URLs are logged without their query values.
"""

from __future__ import annotations

import logging

import pytest
from mcp.shared.exceptions import MCPError

from core.extensions.mcp.config import MCPServerConfig
from core.extensions.mcp.manager import MCPConnection, MCPUnavailable, describe_error, session_lost
from core.logging_setup import OutboundUrlRedactFilter, install_outbound_redaction, redact_url

KEY = "bb_test_0123456789abcdef"


class _Manager:
    def register_server_tools(self, conn, tools):
        raise AssertionError("a failed refresh must not register tools")


class _Session:
    def __init__(self, error):
        self.error = error

    async def list_tools(self, params=None):
        raise self.error

    async def call_tool(self, *a, **kw):
        raise self.error


def _ready(error) -> MCPConnection:
    conn = MCPConnection(
        MCPServerConfig(name="hosted", transport="http", url="https://mcp.example.com/mcp"), _Manager()
    )
    conn.status = "ready"
    conn._session = _Session(error)
    return conn


@pytest.fixture(autouse=True)
def _mcp_on(monkeypatch):
    monkeypatch.setattr("config.settings.mcp_enabled", True)


@pytest.mark.parametrize(
    "message",
    [
        "Unknown mcp-session-id",
        "Session terminated",
        "Session not found",
        "session expired",
        "No valid session ID provided",
    ],
)
def test_the_servers_wording_for_a_lost_session_is_recognised(message):
    assert session_lost(MCPError(code=-32600, message=message))


@pytest.mark.parametrize(
    "message", ["Rate limit exceeded", "Tool not found", "Invalid params", "Server returned an error response"]
)
def test_other_errors_are_not_a_lost_session(message):
    assert not session_lost(MCPError(code=-32603, message=message))


async def test_a_refresh_that_finds_the_session_gone_reconnects():
    conn = _ready(MCPError(code=-32600, message="Unknown mcp-session-id"))
    await conn._refresh_tools()
    assert conn._drop_evt.is_set()


async def test_a_refresh_that_fails_for_another_reason_keeps_the_connection():
    conn = _ready(MCPError(code=-32603, message="Rate limit exceeded"))
    await conn._refresh_tools()
    assert not conn._drop_evt.is_set()


async def test_a_tool_call_that_finds_the_session_gone_reconnects_on_the_first_failure():
    conn = _ready(MCPError(code=-32600, message="Session terminated"))
    with pytest.raises(MCPUnavailable, match="call the tool again"):
        await conn.call_tool("search", {})
    assert conn._drop_evt.is_set()


async def test_an_ordinary_tool_error_still_waits_for_the_breaker():
    conn = _ready(MCPError(code=-32602, message="Invalid params"))
    with pytest.raises(MCPError):
        await conn.call_tool("search", {})
    assert not conn._drop_evt.is_set()


# ── the key in the request log ──────────────────────────────────────────


def _request_record(url) -> logging.LogRecord:
    """A record shaped the way the HTTP client emits one."""
    return logging.LogRecord(
        name="httpx2",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='HTTP Request: %s %s "%s %d %s"',
        args=("POST", url, "HTTP/1.1", 404, "Not Found"),
        exc_info=None,
    )


class _Url:
    """Stands in for the client's URL object, which is not a str."""

    def __init__(self, text):
        self.text = text

    def __str__(self):
        return self.text


def test_query_values_are_removed_from_the_request_line():
    record = _request_record(_Url(f"https://mcp.example.com/mcp?apiKey={KEY}&mode=fast"))
    assert OutboundUrlRedactFilter().filter(record) is True
    line = record.getMessage()
    assert KEY not in line and "fast" not in line
    assert 'POST https://mcp.example.com/mcp?apiKey=REDACTED&mode=REDACTED "HTTP/1.1 404 Not Found"' in line


def test_a_url_without_a_query_is_unchanged():
    record = _request_record(_Url("https://mcp.example.com/mcp"))
    OutboundUrlRedactFilter().filter(record)
    assert record.getMessage() == 'HTTP Request: POST https://mcp.example.com/mcp "HTTP/1.1 404 Not Found"'


def test_a_password_in_the_url_is_removed():
    assert redact_url("https://user:hunter2@mcp.example.com/x") == "https://REDACTED@mcp.example.com/x"


def test_the_filter_is_installed_on_the_client_loggers_once():
    install_outbound_redaction()
    install_outbound_redaction()
    for name in ("httpx", "httpx2"):
        found = [f for f in logging.getLogger(name).filters if isinstance(f, OutboundUrlRedactFilter)]
        assert len(found) == 1


def test_an_error_shown_to_a_person_carries_no_key():
    e = RuntimeError(f"Client error '404 Not Found' for url 'https://mcp.example.com/mcp?apiKey={KEY}'")
    assert KEY not in describe_error(e)
