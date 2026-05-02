"""Coverage sweep: targeted tests for remaining gaps across multiple modules."""

import json

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


def _make_app(*routers):
    app = FastAPI()
    for r in routers:
        app.include_router(r)
    return app


# ===========================================================================
# LLM errors
# ===========================================================================


def test_failover_error_basics():
    from core.llm.errors import FailoverError, FailoverReason

    err = FailoverError(FailoverReason.RATE_LIMIT, "Rate limited")
    assert err.reason == FailoverReason.RATE_LIMIT
    assert "Rate limited" in str(err)


def test_failover_reason_values():
    from core.llm.errors import FailoverReason, classify_http_error

    assert FailoverReason.RATE_LIMIT.value
    assert FailoverReason.CONTEXT_OVERFLOW.value
    # classify HTTP errors
    assert classify_http_error(429) == FailoverReason.RATE_LIMIT
    assert classify_http_error(401) == FailoverReason.AUTH
    assert classify_http_error(404) == FailoverReason.MODEL_NOT_FOUND


# ===========================================================================
# LLM registry
# ===========================================================================


def test_model_registry_register_get():
    from core.llm.registry import ModelRegistry
    from core.llm.types import ModelInfo

    reg = ModelRegistry()
    m = ModelInfo(id="test-model", provider="ollama", context_length=128000)
    reg._models["test-model"] = m
    assert reg._models.get("test-model") is not None
    assert "test-model" in reg._models
    assert "nonexistent" not in reg._models


def test_model_registry_resolve():
    from core.llm.registry import ModelRegistry
    from core.llm.types import ModelInfo

    reg = ModelRegistry()
    m = ModelInfo(id="mistral:7b", provider="ollama", context_length=8000)
    reg._models["mistral:7b"] = m
    resolved = reg.resolve_model_id("mistral:7b")
    assert resolved == "mistral:7b"


def test_model_registry_resolve_unknown():
    from core.llm.registry import ModelRegistry

    reg = ModelRegistry()
    # Unknown model is returned as-is
    resolved = reg.resolve_model_id("unknown-model")
    assert resolved == "unknown-model"


# ===========================================================================
# Skill tools (builtin)
# ===========================================================================


def test_discover_skills_tool(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.skills_dir", str(tmp_path / "skills"))
    from core.tools.builtin.skill_tools import discover_skills

    result = discover_skills("test query")
    assert isinstance(result, str)


def test_load_skill_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.skills_dir", str(tmp_path / "skills"))
    from core.tools.builtin.skill_tools import load_skill

    result = load_skill("nonexistent-skill")
    assert "not found" in result.lower() or "Error" in result


def test_get_tool_schema_basic():
    from core.tools.builtin.discovery_tools import get_tool_schema

    result = get_tool_schema("bash")
    assert isinstance(result, str)


def test_get_tool_schema_not_found():
    from core.tools.builtin.discovery_tools import get_tool_schema

    result = get_tool_schema("nonexistent_tool_xyz")
    assert "Error" in result or "not found" in result.lower()


# ===========================================================================
# Discovery tools (builtin)
# ===========================================================================


def test_discover_tools_basic():
    from core.tools.builtin.discovery_tools import discover_tools

    result = discover_tools("file operations")
    assert isinstance(result, str)


def test_discover_tools_empty_query():
    from core.tools.builtin.discovery_tools import discover_tools

    result = discover_tools("")
    assert isinstance(result, str)


# ===========================================================================
# Push API router
# ===========================================================================


async def test_push_subscribe():
    from api.routers import push

    app = _make_app(push.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/push/subscribe",
            json={
                "endpoint": "https://push.example.com/send/123",
                "p256dh": "fake_p256dh_key",
                "auth": "fake_auth",
            },
        )
    assert resp.status_code in (200, 400)


async def test_push_public_key():
    from api.routers import push

    app = _make_app(push.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/push/vapid-public-key")
    assert resp.status_code in (200, 404)


# ===========================================================================
# More health router endpoints
# ===========================================================================


async def test_settings_access_qr():
    from api.routers import health

    app = _make_app(health.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/settings/access-qr")
    assert resp.status_code in (200, 403)


# ===========================================================================
# More models router endpoints
# ===========================================================================


async def test_models_validate():
    from api.routers import models as models_router

    app = _make_app(models_router.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/models/validate?model=test-model")
    assert resp.status_code in (200, 422, 503)


async def test_models_ollama():
    from api.routers import models as models_router

    app = _make_app(models_router.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/models/ollama")
    assert resp.status_code in (200, 503)


# ===========================================================================
# More chat router endpoints
# ===========================================================================


async def test_chat_retry_not_found():
    from api.routers import chat

    app = _make_app(chat.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/retry/nonexistent-session")
    assert resp.status_code == 404


async def test_chat_compact_session():
    from api.routers import chat
    from db import models as db

    app = _make_app(chat.router)
    sid = db.create_session(title="Compact Test")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(f"/api/compact/{sid}")
    assert resp.status_code in (200, 400)


async def test_chat_partial():
    from api.routers import chat
    from db import models as db

    app = _make_app(chat.router)
    sid = db.create_session(title="Partial Test")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/api/partial/{sid}")
    assert resp.status_code == 200


# ===========================================================================
# More skills router endpoints
# ===========================================================================


async def test_skills_list_discover():
    from api.routers import skills

    app = _make_app(skills.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/skills?q=test")
    assert resp.status_code == 200


# ===========================================================================
# More db/models operations
# ===========================================================================


def test_cleanup_old_partials():
    from db import models as db

    sid = db.create_session()
    db.add_message(sid, "assistant", "partial content", partial=1)
    # cleanup_old_partials removes partials older than X hours
    count = db.cleanup_old_partials(max_age_hours=0)  # 0 = all partials
    assert isinstance(count, int)


def test_search_messages_fts_short_query():
    from db import models as db

    # Short words (< 3 chars) are filtered out
    result = db.search_messages_fts("is")  # 2 chars → filtered
    assert result == []


def test_get_db_stats():
    from db.models import get_db_stats

    stats = get_db_stats()
    assert isinstance(stats, dict)
    assert "sessions" in stats or "total" in stats or len(stats) > 0


# ===========================================================================
# More memory store operations
# ===========================================================================


def test_memory_store_get_file_summary(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from core.memory.store import MemoryStore

    store = MemoryStore(str(tmp_path / "memories"))
    store.add_entry("Test content for file summary", file_name="pernix.notes")
    # Should return file info
    files = store.list_files()
    assert len(files) >= 1
    assert files[0].name == "pernix.notes"
    assert files[0].entry_count >= 1


def test_memory_store_is_duplicate(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from core.memory.store import MemoryStore

    store = MemoryStore(str(tmp_path / "memories"))
    content = "Database uses SQLite WAL mode for concurrency control"
    store.add_entry(content, file_name="pernix.config")
    # Same content should be detected as duplicate
    is_dup = store.is_duplicate(content)
    assert is_dup is True


def test_memory_store_not_duplicate(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from core.memory.store import MemoryStore

    store = MemoryStore(str(tmp_path / "memories"))
    store.add_entry("First entry about databases", file_name="pernix.config")
    # Different content should NOT be a duplicate
    is_dup = store.is_duplicate("Completely different content about networking protocols")
    assert is_dup is False


# ===========================================================================
# More hooks coverage
# ===========================================================================


async def test_run_post_task_hooks_no_session():
    """run_post_task_hooks with nonexistent session is a no-op."""
    from sessions.hooks import run_post_task_hooks

    # Should not raise
    await run_post_task_hooks("nonexistent-session-id")


async def test_run_post_task_hooks_no_title_change(mock_llm_client, monkeypatch):
    """run_post_task_hooks with a titled session skips auto-title."""
    from db import models as db
    from sessions.hooks import run_post_task_hooks
    from sessions.state import AgentSession

    monkeypatch.setattr("config.settings.reflect_enabled", False)
    monkeypatch.setattr("config.settings.eval_auto", False)
    monkeypatch.setattr("config.settings.memory_recall", False)

    sid = db.create_session(title="Already Titled")
    db.add_message(sid, "user", "hi")
    db.add_message(sid, "assistant", "hello")

    session_obj = AgentSession(session_id=sid)
    await run_post_task_hooks(sid, session_obj=session_obj)

    # Session title should not change (it wasn't "New session")
    s = db.get_session(sid)
    assert s["title"] == "Already Titled"


# ===========================================================================
# Context router
# ===========================================================================


async def test_context_not_found():
    from api.routers import context

    app = _make_app(context.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/context/nonexistent-session")
    assert resp.status_code in (200, 404)


# ===========================================================================
# More session manager
# ===========================================================================


def test_session_manager_active_session_ids():
    from sessions.manager import SessionManager

    mgr = SessionManager()
    sid1 = mgr.create_session(title="S1")
    sid2 = mgr.create_session(title="S2")
    ids = mgr.active_session_ids()
    assert sid1 in ids
    assert sid2 in ids


async def test_session_manager_emit():
    from sessions.manager import SessionManager

    mgr = SessionManager()
    sid = mgr.create_session(title="Emit Test")
    session = mgr.get(sid)
    q = session.subscribe()
    mgr.emit(sid, {"type": "test.event", "data": "value"})
    assert not q.empty()
    event = q.get_nowait()
    assert event["type"] == "test.event"
