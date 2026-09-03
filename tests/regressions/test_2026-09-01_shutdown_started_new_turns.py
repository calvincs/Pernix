"""Lifespan shutdown cancelled agent tasks; the worker cascade then resumed
parents and started fresh turns against a closing LLM client."""

import asyncio

import pytest

from db import models as db
from sessions import state_v2 as sv2
from sessions.manager import SessionManager


@pytest.fixture
def mgr(monkeypatch):
    fresh = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", fresh)
    return fresh


async def test_shutdown_flag_stops_pending_dispatch(mgr, monkeypatch):
    sid = mgr.create_session(title="S")
    session = mgr.get(sid)
    session.pending_messages.append(object())
    started = []
    monkeypatch.setattr(mgr, "_spawn_detached", lambda *a, **k: started.append(a))
    mgr.shutting_down = True
    await mgr._process_pending(session)
    assert started == []
