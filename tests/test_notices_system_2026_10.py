"""Tiered notifications (2026-10 noise audit), stream 2d: system-health producers.

Each producer below used to write a bell row directly with its own urgency.
Pinned here: the category (and so the tier) each one now raises, that the two
outage notices (embeddings, MCP) leave the bell by themselves when the cause
clears, and that the dedup keys predating the registry are passed through
unchanged — so the `notify_dedup:<date>:<key>` markers written before the
deploy still suppress a same-day repeat after it.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

import httpx
import pytest

from core import notices
from db import models as db


def _today_marker(key: str) -> str:
    return f"notify_dedup:{datetime.now(timezone.utc).strftime('%Y-%m-%d')}:{key}"


def _log_rows(category: str) -> list[dict]:
    return [n for n in db.list_notifications("log", limit=500) if n["category"] == category]


# --- the registry: what each producer's category means ----------------------


@pytest.mark.parametrize(
    "category,tier",
    [
        ("system.embeddings_down", "bell"),
        ("system.embeddings_switched", "log"),
        ("system.embeddings_recovered", "log"),
        ("system.mcp_down", "bell"),
        ("system.tavily_key", "bell"),
        ("system.tavily_limit", "bell"),
        ("system.tool_quarantined", "bell"),
        ("system.memory_oversized", "bell"),
        ("system.push_rejected", "bell"),
        ("skills.rolled_back", "log"),
        ("spaces.suggested", "log"),
    ],
)
def test_each_producer_category_is_registered_with_its_tier(category, tier):
    assert category in notices.CATEGORIES, "an unregistered category falls back to a generic bell item"
    assert notices.resolve_tier(category) == tier


# --- embeddings ---------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def emb_env(monkeypatch):
    import core.llm.embeddings as emb

    c = _Clock()
    monkeypatch.setattr(emb.time, "monotonic", c)
    monkeypatch.setattr("config.settings.embedding_model", "nomic-embed-text-v2-moe:latest")
    monkeypatch.setattr("config.settings.llm_base_url", "http://aibox:11434")
    monkeypatch.setattr("config.settings.embedding_fallback_model", "")  # the lexical-only path
    for name in ("_last_failure_at", "_failing_since", "_last_notified_at", "_last_warm_at", "_degraded_since"):
        monkeypatch.setattr(emb, name, 0.0)
    monkeypatch.setattr(emb, "_outage_resolve_pending", False)
    return emb, c


def _failing(url, json=None, timeout=None):
    req = httpx.Request("POST", url)
    raise httpx.HTTPStatusError("500", request=req, response=httpx.Response(500, request=req, text="oom"))


def _ok(url, json=None, timeout=None):
    return httpx.Response(200, request=httpx.Request("POST", url), json={"embeddings": [[0.1]]})


def _drive_outage(emb, clock, monkeypatch) -> None:
    monkeypatch.setattr(httpx, "post", _failing)
    assert emb.embed_query_sync("q") is None
    clock.t += 31 * 60
    assert emb.embed_query_sync("q") is None


def test_embeddings_outage_is_one_bell_row_resolved_by_the_next_success(emb_env, monkeypatch):
    emb, clock = emb_env
    _drive_outage(emb, clock, monkeypatch)
    bell = db.get_notifications()
    assert [(n["category"], n["tier"]) for n in bell] == [("system.embeddings_down", "bell")]
    assert bell[0]["title"] == "Embeddings unavailable — memory recall is lexical-only"

    monkeypatch.setattr(httpx, "post", _ok)
    clock.t += 120
    assert emb.embed_query_sync("q") == [0.1]
    assert db.get_notifications() == [], "a successful embed clears it — the body promises so"
    rows = _log_rows("system.embeddings_down")
    assert len(rows) == 1 and rows[0]["resolved_at"]

    # A healthy box must not touch the notifications table on every embed.
    calls = []
    monkeypatch.setattr(db, "resolve_notifications", lambda *a, **kw: calls.append(a) or 0)
    clock.t += 120
    emb.embed_query_sync("q")
    assert calls == []


def test_first_success_after_a_restart_closes_a_row_the_last_process_left_open(emb_env, monkeypatch):
    emb, clock = emb_env
    notices.notify("system.embeddings_down", "Embeddings unavailable — memory recall is lexical-only", "x")
    monkeypatch.setattr(emb, "_outage_resolve_pending", True)  # the import-time value
    monkeypatch.setattr(httpx, "post", _ok)
    assert emb.embed_query_sync("q") == [0.1]
    assert db.get_notifications() == []


def test_fallback_switch_and_recovery_are_log_rows_and_recovery_resolves_the_outage(emb_env, monkeypatch):
    emb, clock = emb_env
    import core.llm.local_embed as le

    monkeypatch.setattr("config.settings.embedding_fallback_model", "BAAI/bge-small-en-v1.5")
    monkeypatch.setattr("config.settings.embedding_fallback_after_minutes", 30)
    monkeypatch.setattr("config.settings.embedding_fallback_recover_minutes", 60)
    monkeypatch.setattr(le, "available", lambda: True)
    monkeypatch.setattr(emb, "_remote_ok_since", 0.0)

    _drive_outage(emb, clock, monkeypatch)  # 31 min: degrades AND the outage notice lands
    assert emb.degraded()
    assert [n["title"] for n in _log_rows("system.embeddings_switched")] == [
        "Embeddings switched to the local CPU fallback"
    ]
    assert [n["category"] for n in db.get_notifications()] == ["system.embeddings_down"]

    monkeypatch.setattr(httpx, "post", _ok)
    assert emb.check_remote_recovery() is False
    clock.t += 61 * 60
    assert emb.check_remote_recovery() is True
    assert [n["title"] for n in _log_rows("system.embeddings_recovered")] == ["Embeddings back on the remote server"]
    assert db.get_notifications() == [], "back on the remote: the outage row resolves"


# --- MCP ---------------------------------------------------------------------


@pytest.fixture
def mcp_env(monkeypatch, tmp_path):
    from core.tools import registry as regmod

    monkeypatch.setattr(regmod, "_registry", regmod.ToolRegistry())
    monkeypatch.setattr("core.tools.registry.TOOLS_CONFIG_PATH", tmp_path / "tools.json")
    monkeypatch.setattr("core.extensions.mcp.config.MCP_SERVERS_PATH", tmp_path / "mcp_servers.json")
    monkeypatch.setattr("core.extensions.mcp.manager.LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr("config.settings.mcp_enabled", True)
    monkeypatch.setattr("config.settings.mcp_stdio_enabled", True)
    monkeypatch.setattr("config.settings.mcp_connect_timeout", 25)


def _stub_cfg(name: str):
    from core.extensions.mcp.config import MCPServerConfig

    return MCPServerConfig(name=name, transport="stdio", command=sys.executable, args=["-m", "tests.fixtures.mcp_stub"])


async def test_mcp_down_is_a_per_server_bell_row_resolved_when_that_server_is_ready(mcp_env):
    from core.extensions.mcp.manager import MCPConnection, MCPManager

    for name in ("stub", "other"):
        conn = MCPConnection(_stub_cfg(name), manager=None)
        conn._was_ready = True  # a server that WAS ready alerts on the first drop
        conn._record_incident(ConnectionRefusedError("connection refused"))
    bell = db.get_notifications()
    assert sorted((n["category"], n["subject"]) for n in bell) == [
        ("system.mcp_down", "other"),
        ("system.mcp_down", "stub"),
    ]
    assert {n["title"] for n in bell} == {"MCP server 'stub' unreachable", "MCP server 'other' unreachable"}

    mgr = MCPManager()
    await mgr.start()
    try:
        conn = await mgr.add_server(_stub_cfg("stub"))
        assert conn.status == "ready"
        # Only the server that came back leaves the bell.
        assert [n["subject"] for n in db.get_notifications()] == ["other"]
    finally:
        await mgr.shutdown()


# --- one-shot producers -----------------------------------------------------


def test_tavily_alerts_raise_their_own_categories(monkeypatch):
    import core.extensions.web as web

    monkeypatch.setenv("TAVILY_API_KEY", "bogus")
    monkeypatch.setattr(web, "_tavily_alerted", False)
    monkeypatch.setattr(web, "_tavily_search", lambda *a, **kw: (_ for _ in ()).throw(web._TavilyKeyError("x")))
    web.search_web("q")
    assert [(n["category"], n["title"]) for n in db.get_notifications()] == [
        ("system.tavily_key", "Tavily API key rejected")
    ]

    db.delete_notification(db.get_notifications()[0]["id"])
    monkeypatch.setattr(web, "_tavily_alerted", False)
    monkeypatch.setattr(web, "_tavily_search", lambda *a, **kw: (_ for _ in ()).throw(web._TavilyLimitError("x")))
    web.search_web("q")
    assert [n["category"] for n in db.get_notifications()] == ["system.tavily_limit"]


def test_quarantined_tool_keeps_its_dedup_key(tmp_path):
    from core.tools.builtin import _quarantine_custom_module

    class _Pkg:
        __path__ = [str(tmp_path)]

    (tmp_path / "custom_bad.py").write_text("raise RuntimeError\n")
    db.set_snooze_state(_today_marker("custom-tool-broken:custom_bad"), "1")  # announced before the deploy
    _quarantine_custom_module(_Pkg(), "custom_bad", RuntimeError("boom"))
    assert db.get_notifications() == [], "a marker written before the registry still suppresses the repeat"

    (tmp_path / "custom_other.py").write_text("raise RuntimeError\n")
    _quarantine_custom_module(_Pkg(), "custom_other", RuntimeError("boom"))
    (n,) = db.get_notifications()
    assert (n["category"], n["subject"]) == ("system.tool_quarantined", "custom_other")
    assert db.get_snooze_state(_today_marker("custom-tool-broken:custom_other"))


def test_oversized_memory_file_keeps_its_dedup_key(tmp_path):
    from core.memory.store import _notify_oversized_file

    big = tmp_path / "huge.md"
    _notify_oversized_file(big)
    _notify_oversized_file(big)
    rows = db.get_notifications()
    assert [(n["category"], n["title"]) for n in rows] == [
        ("system.memory_oversized", "A memory file is too large to index")
    ]
    assert db.get_snooze_state(_today_marker("memory-oversized:huge.md"))


def test_push_rejected_is_a_bell_row_with_its_old_dedup_key():
    from core.notify import _notice_push_rejected

    _notice_push_rejected("web.push.apple.com", 403, "BadJwtToken")
    _notice_push_rejected("web.push.apple.com", 403, "BadJwtToken")
    (n,) = db.get_notifications()
    assert (n["category"], n["tier"], n["subject"]) == ("system.push_rejected", "bell", "web.push.apple.com")
    assert n["title"] == "Push rejected by web.push.apple.com"
    assert "HTTP 403 BadJwtToken" in n["body"]
    assert db.get_snooze_state(_today_marker("push_rejected:web.push.apple.com"))
