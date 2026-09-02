"""Retry and clear rewrote the transcript with no state gate.

Compaction has always refused unless the session is idle. Retry and clear
did not: mid-turn they deleted the running turn's own user message and
everything after it, so the agent's next round compiled a history with no
root for its tool calls — and manager.prompt then QUEUED the re-prompt
behind the live turn instead of replacing it, so the user got the work
twice.
"""

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from api.routers.sessions import require_idle
from db import models as db
from sessions import state_v2 as sv2
from sessions.manager import SessionManager


@pytest.fixture
def mgr(monkeypatch):
    fresh = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", fresh)
    return fresh


def _app():
    from api.routers import chat, sessions

    app = FastAPI()
    app.include_router(chat.router)
    app.include_router(sessions.router)
    return app


def test_a_processing_session_refuses_a_rewrite(mgr):
    sid = mgr.create_session(title="busy")
    sv2._set_state(mgr.get(sid), sv2.SessionStateV2.PROCESSING)
    with pytest.raises(HTTPException) as exc:
        require_idle(sid, "retry")
    assert exc.value.status_code == 409


def test_an_idle_session_is_allowed(mgr):
    sid = mgr.create_session(title="idle")
    require_idle(sid, "retry")  # must not raise


def test_a_session_awaiting_the_user_is_allowed(mgr):
    sid = mgr.create_session(title="asked")
    sv2._set_state(mgr.get(sid), sv2.SessionStateV2.AWAITING_USER)
    require_idle(sid, "clear it")


def test_a_non_resident_session_is_allowed(mgr):
    sid = db.create_session(title="on disk only")
    require_idle(sid, "clear it")  # no turn can be running


async def test_clear_endpoint_409s_mid_turn(mgr):
    sid = mgr.create_session(title="busy")
    db.add_message(sid, "user", "the work in progress")
    sv2._set_state(mgr.get(sid), sv2.SessionStateV2.PROCESSING)

    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://t") as c:
        resp = await c.post(f"/api/sessions/{sid}/clear")
    assert resp.status_code == 409
    assert len(db.get_messages(sid)) == 1, "the live turn's transcript must be intact"


async def test_retry_endpoint_409s_mid_turn(mgr):
    sid = mgr.create_session(title="busy")
    db.add_message(sid, "user", "the ask")
    db.add_message(sid, "assistant", "half an answer")
    sv2._set_state(mgr.get(sid), sv2.SessionStateV2.PROCESSING)

    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://t") as c:
        resp = await c.post(f"/api/retry/{sid}")
    assert resp.status_code == 409
    assert len(db.get_messages(sid)) == 2
