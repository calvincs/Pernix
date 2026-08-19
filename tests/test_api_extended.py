"""Extended API router tests for better coverage."""

import json

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


def _make_app(*routers):
    app = FastAPI()
    for r in routers:
        app.include_router(r)
    return app


# ---------------------------------------------------------------------------
# Health router - more endpoints
# ---------------------------------------------------------------------------


async def test_settings_get():
    from api.routers import health

    app = _make_app(health.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/settings")
    assert resp.status_code == 200


async def test_settings_apikey():
    from api.routers import health

    app = _make_app(health.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/settings/apikey", json={"key": "test-key-123"})
    assert resp.status_code in (200, 400)


async def test_get_settings_auth_token_serves_any_authorised_caller():
    """No localhost gate on the endpoint itself.

    It used to 403 anything is_local_client() rejected, which under `docker
    compose` with a published port is EVERY browser — including one on the
    server — because Docker-bridge sources are deliberately not local. The
    Settings UI could only ever 403. _AuthMiddleware is the gate now: the path
    is absent from _PUBLIC_EXACT/_PUBLIC_PREFIXES, so reaching the handler
    already required the token this endpoint returns.
    """
    from api.routers import health

    app = _make_app(health.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/settings/auth-token")
    assert resp.status_code == 200
    assert "token" in resp.json()


async def test_auth_token_endpoints_are_not_public_paths():
    """The middleware is the only thing guarding these, so it must actually run.

    If either path ever drifts into the public list the token becomes readable,
    and rotatable, by anyone who can reach the port.
    """
    from api.app import _PUBLIC_EXACT, _PUBLIC_PREFIXES

    for path in ("/api/settings/auth-token", "/api/settings/auth-token/regenerate"):
        assert path not in _PUBLIC_EXACT
        assert not any(path.startswith(prefix) for prefix in _PUBLIC_PREFIXES)


async def test_regenerate_auth_token_replaces_and_returns_it(monkeypatch):
    """The new token must come back in the body.

    The caller has to adopt it in the same breath — the old token dies the
    moment settings.save() lands, so a client that does not swap it is the next
    device signed out by its own button press.
    """
    from api.routers import health
    from config import settings

    # monkeypatch, not a bare assignment: settings is module-global and xdist
    # reuses a worker across tests, so a leaked token would follow us.
    # conftest's isolate_data already points SETTINGS_PATH at tmp_path, so the
    # save() below cannot touch the real data/settings.json.
    monkeypatch.setattr("config.settings.auth_token", "original-token-value")
    app = _make_app(health.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/settings/auth-token/regenerate")

    assert resp.status_code == 200
    minted = resp.json()["token"]
    assert minted and minted != "original-token-value"
    assert settings.auth_token == minted


async def test_env_vars():
    from api.routers import health

    app = _make_app(health.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/env-vars")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, dict)


# ---------------------------------------------------------------------------
# Sessions router - more endpoints
# ---------------------------------------------------------------------------


async def test_session_cancel():
    from api.routers import sessions
    from sessions.manager import get_manager

    app = _make_app(sessions.router)
    # Create session in memory so cancel can find it
    mgr = get_manager()
    sid = mgr.create_session(title="Cancel Target")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(f"/api/sessions/{sid}/cancel")
    # cancel returns 200 if session in memory
    assert resp.status_code == 200


async def test_session_cancel_not_found():
    from api.routers import sessions

    app = _make_app(sessions.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/sessions/nonexistent-xyz/cancel")
    assert resp.status_code == 404


async def test_session_clear():
    from api.routers import sessions
    from db import models as db

    app = _make_app(sessions.router)
    sid = db.create_session(title="Clear Target")
    db.add_message(sid, "user", "message to clear")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(f"/api/sessions/{sid}/clear")
    assert resp.status_code in (200, 400)


# ---------------------------------------------------------------------------
# Chat router - more endpoints
# ---------------------------------------------------------------------------


async def test_chat_inject():
    from api.routers import chat
    from db import models as db

    app = _make_app(chat.router)
    sid = db.create_session(title="Inject Test")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/chat/inject",
            json={
                "session_id": sid,
                "content": "injected message",
            },
        )
    assert resp.status_code in (200, 400)


async def test_chat_inject_stamps_turn_root_when_loop_live():
    """Mid-turn injections must carry parent_user_msg_id = the active turn's
    root so compile_context sorts them inside the turn instead of permanently
    after every reply (session b23ffafde5ba re-acknowledgment loop)."""
    from api.routers import chat
    from db import models as db
    from sessions import state_v2 as sv2

    app = _make_app(chat.router)
    sid = db.create_session(title="Inject Stamp Test")
    root = db.add_message(sid, "user", "the turn root")

    class _StubSession:
        _state_v2 = sv2.SessionStateV2.PROCESSING
        current_turn_user_msg_id = root

        def emit_event(self, _evt):
            pass

    class _StubManager:
        def get(self, _sid):
            return _StubSession()

    orig_get_manager = chat.get_manager
    chat.get_manager = lambda: _StubManager()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/chat/inject",
                json={"session_id": sid, "message": "use port 8137"},
            )
    finally:
        chat.get_manager = orig_get_manager

    assert resp.status_code == 200
    assert resp.json()["status"] == "injected"
    rows = db.get_messages(sid)
    injected = [r for r in rows if r["content"] == "use port 8137"]
    assert injected, rows
    meta = json.loads(injected[-1]["metadata"])
    assert meta["injected"] is True
    assert meta["parent_user_msg_id"] == root


async def test_chat_usage():
    from api.routers import chat
    from db import models as db

    app = _make_app(chat.router)
    sid = db.create_session(title="Usage Test")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/api/usage/{sid}")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Questions router - more coverage
# ---------------------------------------------------------------------------


async def test_questions_dismiss():
    from api.routers import questions
    from db import models as db

    app = _make_app(questions.router)
    sid = db.create_session(title="Dismiss Test")
    qid = db.add_question(sid, "Dismiss me?")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(f"/api/questions/{qid}/dismiss")
    assert resp.status_code in (200, 404)


async def test_notifications_list():
    from api.routers import questions

    app = _make_app(questions.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/notifications")
    assert resp.status_code == 200


async def test_notifications_dismiss():
    from api.routers import questions
    from db import models as db

    app = _make_app(questions.router)
    nid = db.add_notification(title="Test", body="Hello")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(f"/api/notifications/{nid}/dismiss")
    assert resp.status_code in (200, 404)


async def test_send_notification():
    from api.routers import questions

    app = _make_app(questions.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/notify",
            json={
                "title": "Test Alert",
                "body": "Something happened",
                "urgency": "high",
            },
        )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Models router - more coverage
# ---------------------------------------------------------------------------


async def test_models_list():
    from api.routers import models as models_router

    app = _make_app(models_router.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/models")
    assert resp.status_code == 200


async def test_models_refresh(mock_llm_client):
    from api.routers import models as models_router

    app = _make_app(models_router.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/models/refresh")
    assert resp.status_code in (200, 404)


# ---------------------------------------------------------------------------
# Context router
# ---------------------------------------------------------------------------


async def test_context_endpoint():
    from api.routers import context
    from db import models as db

    app = _make_app(context.router)
    sid = db.create_session(title="Context Test")
    db.add_message(sid, "user", "Hello")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/api/context/{sid}")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Memory router - more coverage
# ---------------------------------------------------------------------------


async def test_memory_maintenance(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from api.routers import memory

    app = _make_app(memory.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/memory/maintenance")
    assert resp.status_code == 200


async def test_memory_file_detail(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from core.memory.store import MemoryStore

    store = MemoryStore(str(tmp_path / "memories"))
    store.add_entry("Test entry content", file_name="pernix.notes")
    from api.routers import memory

    app = _make_app(memory.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/memory/files/pernix.notes")
    assert resp.status_code in (200, 404)


# ---------------------------------------------------------------------------
# Skills router
# ---------------------------------------------------------------------------


async def test_skills_detail_not_found():
    from api.routers import skills

    app = _make_app(skills.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/skills/nonexistent-skill")
    assert resp.status_code == 404
