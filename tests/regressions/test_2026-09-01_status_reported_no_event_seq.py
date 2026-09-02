"""A reaped session's status looked like a server restart to the client.

GET /api/sessions/{id}/status omitted event_seq when the session was no
longer in memory. The browser read the missing value as 0, which is what a
restarted server's reset counter looks like, so returning to a
backgrounded tab whose idle session had simply been reaped triggered a
transcript reload, a scroll jump to the bottom, and a "reconnected" notice.

Status now states in_memory and a null event_seq explicitly.
"""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from db import models as db


def _client_app():
    from api.routers import sessions

    app = FastAPI()
    app.include_router(sessions.router)
    return app


@pytest.fixture
def app(monkeypatch):
    from sessions.manager import SessionManager

    fresh = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", fresh)
    return _client_app(), fresh


async def test_a_session_only_in_the_db_reports_no_seq(app):
    application, _mgr = app
    sid = db.create_session(title="reaped")
    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://t") as c:
        resp = await c.get(f"/api/sessions/{sid}/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["in_memory"] is False
    assert body["event_seq"] is None, "null, not absent — absent reads as 0 == 'server restarted'"


async def test_a_resident_session_says_so(app):
    application, mgr = app
    sid = mgr.create_session(title="live")
    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://t") as c:
        resp = await c.get(f"/api/sessions/{sid}/status")
    body = resp.json()
    assert body["in_memory"] is True


async def test_an_unknown_session_is_still_a_404(app):
    application, _mgr = app
    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://t") as c:
        resp = await c.get("/api/sessions/does-not-exist/status")
    assert resp.status_code == 404
