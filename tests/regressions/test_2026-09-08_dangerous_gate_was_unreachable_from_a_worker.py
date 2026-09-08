"""Agent Mesh build, 2026-09-07: the `research` worker kind grants browse_web
(safety_level="dangerous") while its EXCLUSIVE tool_allowlist omits ask_user.
The schema builder intersects that allowlist after the builtin force-add, so
ask_user was gone and the ask_user -> approve_dangerous_tool handshake was
impossible. Four research workers took 11 approval refusals on a tool the
harness itself had handed them, with no move that could ever clear the gate.

A session that cannot reach a human must be treated as unattended, exactly as
cron and canary sessions already are.
"""

from __future__ import annotations

import types

import pytest

from core.tools import executor as ex


class _FakeManager:
    def __init__(self, sessions: dict):
        self._sessions = sessions

    def get(self, sid):
        return self._sessions.get(sid)


def _session(**kw):
    base = {
        "session_type": "normal",
        "parent_session_id": None,
        "tool_allowlist": None,
    }
    base.update(kw)
    return types.SimpleNamespace(**base)


@pytest.fixture
def patch_manager(monkeypatch):
    def _install(sessions: dict):
        mgr = _FakeManager(sessions)
        monkeypatch.setattr("sessions.manager.get_manager", lambda: mgr)

    return _install


def test_worker_whose_allowlist_drops_ask_user_is_unattended(patch_manager):
    """The exact Agent Mesh shape: a research worker of a normal session."""
    research_allowlist = frozenset({"file_read", "file_write", "glob", "grep", "search_web", "browse_web", "http_get"})
    patch_manager(
        {
            "w1": _session(
                session_type="worker",
                parent_session_id="p1",
                tool_allowlist=research_allowlist,
            ),
            "p1": _session(session_type="normal"),
        }
    )
    assert ex._is_unattended_session("w1") is True


def test_a_session_that_keeps_ask_user_is_still_gated(patch_manager):
    """An allowlist that retains ask_user can complete the handshake — the gate
    must stay on for it."""
    patch_manager(
        {
            "w2": _session(
                session_type="worker",
                parent_session_id="p1",
                tool_allowlist=frozenset({"file_read", "browse_web", "ask_user"}),
            ),
            "p1": _session(session_type="normal"),
        }
    )
    assert ex._is_unattended_session("w2") is False


def test_unrestricted_normal_session_is_still_gated(patch_manager):
    """No allowlist at all means the full surface, ask_user included."""
    patch_manager({"s1": _session(session_type="normal")})
    assert ex._is_unattended_session("s1") is False


def test_cron_and_its_workers_stay_exempt(patch_manager):
    patch_manager(
        {
            "c1": _session(session_type="cron"),
            "w3": _session(session_type="worker", parent_session_id="c1"),
        }
    )
    assert ex._is_unattended_session("c1") is True
    assert ex._is_unattended_session("w3") is True


def test_research_worker_kind_still_ships_a_gated_tool_without_ask_user():
    """Guards the premise: if the kind ever gains ask_user, or loses its
    dangerous tools, this regression's shape has changed and the test above
    should be revisited rather than silently passing on a different world."""
    from core.extensions.orchestration.kinds import resolve_kind

    kind = resolve_kind("research")
    assert kind is not None
    assert "browse_web" in kind.tool_allowlist
    assert "ask_user" not in kind.tool_allowlist
