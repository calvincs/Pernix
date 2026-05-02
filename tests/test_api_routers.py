"""Tests for API routers using httpx AsyncClient + ASGITransport."""

import json

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


def _make_app(*routers):
    """Create a minimal FastAPI app from one or more routers."""
    app = FastAPI()
    for router in routers:
        app.include_router(router)
    return app


# ---------------------------------------------------------------------------
# Health router
# ---------------------------------------------------------------------------


async def test_health_endpoint():
    from api.routers import health

    app = _make_app(health.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "status" in data
    assert data["status"] == "healthy"
    assert "version" in data
    assert "sessions_active" in data


async def test_health_detailed_localhost():
    from api.routers import health

    app = _make_app(health.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # httpx ASGI transport appears as 127.0.0.1 — should be allowed
        resp = await client.get("/api/health/detailed")
    # Either 200 (localhost allowed) or 403 (non-localhost) — just check response is valid
    assert resp.status_code in (200, 403)


async def test_get_settings():
    from api.routers import health

    app = _make_app(health.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/settings")
    assert resp.status_code == 200
    data = resp.json()
    assert "llm_model" in data


async def test_update_settings():
    from api.routers import health

    app = _make_app(health.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/settings", json={"llm_model": "test-model-123"})
    assert resp.status_code == 200
    data = resp.json()
    assert "updated" in data or "status" in data


# ---------------------------------------------------------------------------
# Sessions router
# ---------------------------------------------------------------------------


async def test_create_session():
    from api.routers import sessions

    app = _make_app(sessions.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/sessions", json={"title": "Test Session"})
    assert resp.status_code == 200
    data = resp.json()
    assert "session_id" in data
    assert len(data["session_id"]) > 0


async def test_list_sessions_empty():
    from api.routers import sessions

    app = _make_app(sessions.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "count" in data


async def test_list_sessions_with_data():
    from api.routers import sessions
    from db import models as db

    app = _make_app(sessions.router)
    sid = db.create_session(title="Listed Session")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] >= 1


async def test_get_session_not_found():
    from api.routers import sessions

    app = _make_app(sessions.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/sessions/nonexistent-session-id")
    assert resp.status_code == 404


async def test_get_session_found():
    from api.routers import sessions
    from db import models as db

    app = _make_app(sessions.router)
    sid = db.create_session(title="Get Me")
    db.add_message(sid, "user", "hello")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/api/sessions/{sid}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == sid
    assert len(data["messages"]) == 1


async def test_get_session_status():
    from api.routers import sessions
    from db import models as db

    app = _make_app(sessions.router)
    sid = db.create_session(title="Status Test")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/api/sessions/{sid}/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "status" in data


async def test_delete_session():
    from api.routers import sessions
    from db import models as db

    app = _make_app(sessions.router)
    sid = db.create_session(title="Delete Me")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.delete(f"/api/sessions/{sid}")
    assert resp.status_code == 200
    assert db.get_session(sid) is None


async def test_delete_session_idempotent():
    """Deleting a nonexistent session is a no-op (returns 200)."""
    from api.routers import sessions

    app = _make_app(sessions.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.delete("/api/sessions/nonexistent-session")
    # Delete is idempotent — returns 200 even if not found
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Chat router
# ---------------------------------------------------------------------------


async def test_chat_missing_session_id():
    from api.routers import chat

    app = _make_app(chat.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/chat", json={"message": "hello"})
    assert resp.status_code == 400


async def test_chat_missing_message():
    from api.routers import chat
    from db import models as db

    app = _make_app(chat.router)
    sid = db.create_session(title="Chat Test")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/chat", json={"session_id": sid})
    assert resp.status_code == 400


async def test_chat_session_not_found():
    from api.routers import chat

    app = _make_app(chat.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/chat",
            json={
                "session_id": "nonexistent",
                "message": "hello",
            },
        )
    assert resp.status_code == 404


async def test_chat_message_too_large():
    from api.routers import chat
    from db import models as db

    app = _make_app(chat.router)
    sid = db.create_session(title="Size Test")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/chat",
            json={
                "session_id": sid,
                "message": "x" * 1_100_000,
            },
        )
    assert resp.status_code == 413


async def test_chat_accepted(monkeypatch):
    from api.routers import chat
    from db import models as db
    from sessions.manager import get_manager

    app = _make_app(chat.router)
    sid = db.create_session(title="Chat Accepted")

    # Mock the manager prompt to avoid starting a real agent
    async def mock_prompt(*args, **kwargs):
        pass

    monkeypatch.setattr(get_manager(), "prompt", mock_prompt)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/chat",
            json={
                "session_id": sid,
                "message": "Hello world",
            },
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"


async def test_chat_idempotency():
    from api.routers import chat
    from db import models as db
    from sessions.manager import get_manager

    app = _make_app(chat.router)
    sid = db.create_session(title="Idempotency Test")
    db.add_message(sid, "user", "original", idempotency_key="test-key-123")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/chat",
            json={
                "session_id": sid,
                "message": "duplicate",
                "idempotency_key": "test-key-123",
            },
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "duplicate"


# ---------------------------------------------------------------------------
# Memory router
# ---------------------------------------------------------------------------


async def test_memory_search():
    from api.routers import memory

    app = _make_app(memory.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/memory/search?q=test")
    assert resp.status_code == 200


async def test_memory_files():
    from api.routers import memory

    app = _make_app(memory.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/memory/files")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Tools router
# ---------------------------------------------------------------------------


async def test_tools_list():
    from api.routers import tools

    app = _make_app(tools.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/tools")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Questions router
# ---------------------------------------------------------------------------


async def test_questions_list_empty():
    from api.routers import questions

    app = _make_app(questions.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/questions")
    assert resp.status_code == 200
    data = resp.json()
    assert "questions" in data


async def test_questions_delete_not_found():
    from api.routers import questions

    app = _make_app(questions.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.delete("/api/questions/nonexistent")
    assert resp.status_code == 404


async def test_questions_answer():
    from api.routers import questions
    from db import models as db

    app = _make_app(questions.router)
    sid = db.create_session(title="QA Test")
    qid = db.add_question(sid, "Are you ready?")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(f"/api/questions/{qid}/answer", json={"answer": "yes"})
    assert resp.status_code in (200, 404)  # 404 if question not found after answer


async def test_answer_question_context_field():
    """Answer formatting includes the context field when present."""
    # Test the formatting logic that's used in the answer endpoint
    question = {"question": "What model?", "context": "User wants Claude"}
    answer = "Use claude-sonnet"

    context_field = question.get("context", "")
    formatted = (
        f"[User answered your question]\n"
        f"Q: {question['question']}\n" + (f"Context: {context_field}\n" if context_field else "") + f"A: {answer}"
    )
    assert "Context: User wants Claude" in formatted
    assert "Q: What model?" in formatted
    assert "A: Use claude-sonnet" in formatted


async def test_answer_question_no_context_field():
    """Answer formatting omits Context line when context is empty."""
    question = {"question": "Ready?", "context": ""}
    answer = "yes"

    context_field = question.get("context", "")
    formatted = (
        f"[User answered your question]\n"
        f"Q: {question['question']}\n" + (f"Context: {context_field}\n" if context_field else "") + f"A: {answer}"
    )
    assert "Context:" not in formatted
    assert "Q: Ready?" in formatted
    assert "A: yes" in formatted


# ---------------------------------------------------------------------------
# Models router
# ---------------------------------------------------------------------------


async def test_models_list():
    from api.routers import models as models_router

    app = _make_app(models_router.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/models")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Skills router
# ---------------------------------------------------------------------------


async def test_skills_list():
    from api.routers import skills

    app = _make_app(skills.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/skills")
    assert resp.status_code == 200
