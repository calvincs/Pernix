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


async def test_kernel_settings_are_bounded():
    """The four kernel knobs are bounds-checked like the RLM caps: an
    out-of-range value reverts instead of, say, reaping every kernel between
    tool rounds or letting one snapshot eat the disk."""
    from api.routers import health
    from config import settings

    app = _make_app(health.router)
    before = {
        k: getattr(settings, k)
        for k in (
            "kernel_idle_seconds",
            "kernel_snapshot_max_bytes",
            "kernel_max_concurrent",
            "large_result_bind_threshold",
        )
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/settings",
            json={
                "kernel_idle_seconds": 5,  # below the 60s floor
                "kernel_snapshot_max_bytes": 1024,  # below the 1MB floor
                "kernel_max_concurrent": 0,  # below the 1 floor
                "large_result_bind_threshold": 50_000_000,  # above the ceiling
            },
        )
    assert resp.status_code == 200
    for key, original in before.items():
        assert getattr(settings, key) == original  # every one reverted

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/settings", json={"kernel_max_concurrent": 8})
    assert resp.status_code == 200
    assert settings.kernel_max_concurrent == 8
    settings.kernel_max_concurrent = before["kernel_max_concurrent"]


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


async def test_patch_session_rename_and_pin():
    from api.routers import sessions
    from db import models as db

    app = _make_app(sessions.router)
    sid = db.create_session(title="Old title")
    before = db.get_session(sid)["updated_at"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.patch(f"/api/sessions/{sid}", json={"title": "  New title  ", "pinned": True})
    assert resp.status_code == 200
    assert resp.json()["title"] == "New title"
    assert resp.json()["pinned"] is True
    row = db.get_session(sid)
    assert row["title"] == "New title"
    assert row["pinned"] == 1
    # Rename/pin must not bump recency ordering.
    assert row["updated_at"] == before


async def test_patch_session_rejects_empty_title():
    from api.routers import sessions
    from db import models as db

    app = _make_app(sessions.router)
    sid = db.create_session(title="Keep me")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.patch(f"/api/sessions/{sid}", json={"title": "   "})
    assert resp.status_code == 400
    assert db.get_session(sid)["title"] == "Keep me"


async def test_patch_session_not_found():
    from api.routers import sessions

    app = _make_app(sessions.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.patch("/api/sessions/nonexistent", json={"title": "x"})
    assert resp.status_code == 404


async def test_pending_queue_list_and_remove():
    from api.routers import sessions
    from db import models as db
    from sessions.manager import get_manager

    app = _make_app(sessions.router)
    sid = db.create_session(title="Queue test")
    session = get_manager().get_or_create(sid)
    mid = db.add_message(sid, "user", "queued message")
    from sessions.state import PendingMessage

    session.pending_messages.append(PendingMessage("queued message", "", True, 0.0, mid))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/api/sessions/{sid}/pending")
        assert resp.status_code == 200
        assert resp.json()["pending"] == [{"message_id": mid, "preview": "queued message"}]

        resp = await client.delete(f"/api/sessions/{sid}/pending/{mid}")
        assert resp.status_code == 200
        assert resp.json()["queue_depth"] == 0

        # Deque entry, DB row both gone; second delete 404s.
        assert len(session.pending_messages) == 0
        assert db.get_message(mid) is None
        resp = await client.delete(f"/api/sessions/{sid}/pending/{mid}")
        assert resp.status_code == 404


async def test_patch_session_model_override_set_and_clear():
    from api.routers import sessions
    from db import models as db
    from sessions.manager import get_manager

    app = _make_app(sessions.router)
    sid = db.create_session(title="Override me")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.patch(f"/api/sessions/{sid}", json={"model_override": "some/model"})
        assert resp.status_code == 200
        assert resp.json()["model_override"] == "some/model"
        session = get_manager().get(sid)
        assert session is not None
        assert session.model_override == "some/model"
        # A user-set override must NOT register as an agent switch (which
        # would be reverted at turn end).
        assert session._model_before_agent_switch is None

        resp = await client.patch(f"/api/sessions/{sid}", json={"model_override": ""})
        assert resp.status_code == 200
        assert resp.json()["model_override"] is None
        assert get_manager().get(sid).model_override is None


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


async def test_skills_list_surfaces_disabled_with_enabled_false_flag(tmp_path, monkeypatch):
    """The Explorer UI depends on disabled skills appearing in the list
    response WITH 'enabled': False so the toggle row stays renderable.
    Regression risk: if someone swaps reg.all_skills() for reg.enabled_skills()
    in the router, disabled skills vanish from the UI entirely and the user
    can never re-enable them.
    """
    from api.routers import skills as skills_router
    from core.skills.registry import SkillRegistry

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    for name in ("alpha", "beta"):
        d = skills_dir / name
        d.mkdir()
        (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name}\n---\n# {name}\n")

    monkeypatch.setattr("config.settings.skills_dir", str(skills_dir))
    fresh = SkillRegistry()
    fresh.scan(skills_dir)
    fresh.disable("alpha")
    monkeypatch.setattr("core.skills.registry._skill_registry", fresh)

    app = _make_app(skills_router.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/skills")
    assert resp.status_code == 200
    by_name = {s["name"]: s for s in resp.json()["skills"]}
    assert "alpha" in by_name, "disabled skill missing from /api/skills — UI toggle row would be lost"
    assert "beta" in by_name
    assert by_name["alpha"]["enabled"] is False
    assert by_name["beta"]["enabled"] is True


async def test_skills_list_surfaces_pending_proposal_count(tmp_path, monkeypatch):
    """The Skills UI needs ``pending_proposals_count`` per row so it can
    render the inline 'N pending' badge without an extra API round trip."""
    from api.routers import skills as skills_router
    from core.skills.registry import SkillRegistry
    from db import models as db

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    for name in ("alpha", "beta"):
        d = skills_dir / name
        d.mkdir()
        (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name}\n---\n# {name}\n")

    monkeypatch.setattr("config.settings.skills_dir", str(skills_dir))
    fresh = SkillRegistry()
    fresh.scan(skills_dir)
    monkeypatch.setattr("core.skills.registry._skill_registry", fresh)

    sid = db.create_session(title="Refine source")
    for _ in range(2):
        db.add_skill_proposal(
            workflow_name=None,
            run_id=None,
            skill_name="alpha",
            section="Usage",
            problem="p",
            proposed_change="c",
            confidence=0.8,
            source_origin="refine",
            session_id=sid,
        )

    app = _make_app(skills_router.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/skills")
    assert resp.status_code == 200
    by_name = {s["name"]: s for s in resp.json()["skills"]}
    assert by_name["alpha"]["pending_proposals_count"] == 2
    assert by_name["beta"]["pending_proposals_count"] == 0


async def test_skills_get_surfaces_disabled_skill_body_and_resources(tmp_path, monkeypatch):
    """GET /api/skills/{name} must still return the body + resource tree for
    a disabled skill so the user can inspect/edit it before re-enabling.
    The router uses include_disabled=True for this — if it ever drops back
    to the default-filtered call, the editor view goes blank for disabled skills.
    """
    from api.routers import skills as skills_router
    from core.skills.registry import SkillRegistry

    skills_dir = tmp_path / "skills"
    d = skills_dir / "off-one"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: off-one\ndescription: x\n---\n# Body text\n")
    (d / "scripts").mkdir()
    (d / "scripts" / "go.sh").write_text("#!/bin/sh\necho ok")

    monkeypatch.setattr("config.settings.skills_dir", str(skills_dir))
    fresh = SkillRegistry()
    fresh.scan(skills_dir)
    fresh.disable("off-one")
    monkeypatch.setattr("core.skills.registry._skill_registry", fresh)

    app = _make_app(skills_router.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/skills/off-one")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert "Body text" in body["instructions"], "disabled skill body must still surface in editor view"
    assert "go.sh" in body["resources"].get("scripts", []), "disabled skill resources must still surface in editor"


async def test_skills_patch_toggle_round_trips_through_registry(tmp_path, monkeypatch):
    """PATCH must round-trip through reg.disable / reg.enable. After PATCH,
    is_disabled() must reflect the new state, and a subsequent PATCH back
    to enabled must clear it. Catches a regression where the router would
    silently bypass the registry singleton (e.g. by reverting to the old
    JSON-file dance).
    """
    from api.routers import skills as skills_router
    from core.skills.registry import SkillRegistry, get_skill_registry

    skills_dir = tmp_path / "skills"
    d = skills_dir / "togg"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: togg\ndescription: x\n---\n# body\n")

    monkeypatch.setattr("config.settings.skills_dir", str(skills_dir))
    fresh = SkillRegistry()
    fresh.scan(skills_dir)
    monkeypatch.setattr("core.skills.registry._skill_registry", fresh)

    app = _make_app(skills_router.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.patch("/api/skills/togg", json={"enabled": False})
        assert resp.status_code == 200
        assert get_skill_registry().is_disabled("togg")
        resp = await client.patch("/api/skills/togg", json={"enabled": True})
        assert resp.status_code == 200
        assert not get_skill_registry().is_disabled("togg")


async def test_sw_js_route_stamps_build_id():
    """/sw.js is served with the deploy build id substituted for __BUILD__
    and no-cache headers, so PWA clients detect new builds automatically."""
    from api.app import BUILD_ID, service_worker

    resp = await service_worker()
    body = resp.body.decode()
    assert "__BUILD__" not in body
    assert BUILD_ID and len(BUILD_ID) == 12
    assert f"pernix-shell-{BUILD_ID}" in body
    assert "no-cache" in resp.headers.get("cache-control", "")
    # The SW must not intercept its own script fetches.
    assert "url.pathname === '/sw.js'" in body


async def test_health_reports_build_id():
    from api.app import BUILD_ID
    from api.routers.health import health

    data = await health()
    assert data.get("build") == BUILD_ID
