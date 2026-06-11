"""Pernix — Tests for the model_mgmt extension (switch_model scope semantics)."""

from core.extensions.model_mgmt import switch_model
from db import models as db
from sessions.manager import get_manager


def _make_session(title: str = "Switch test"):
    sid = db.create_session(title=title)
    return get_manager().get_or_create(sid)


# These tests are intentionally synchronous: with no running event loop,
# switch_model skips the registry-resolution path (best-effort) and applies
# the raw model name, exercising only the scope/override logic under test.


def test_switch_model_turn_scope_default_sets_restore_tracker():
    session = _make_session()
    result = switch_model("some/model-a", _context={"session_id": session.session_id})

    assert session.model_override == "some/model-a"
    # Turn-end restore tracker armed with "" sentinel (no prior override).
    assert session._model_before_agent_switch == ""
    assert session._budget_before_agent_switch == -1
    assert "temporary for this turn" in result


def test_switch_model_session_scope_persists():
    session = _make_session()
    result = switch_model("some/model-b", scope="session", _context={"session_id": session.session_id})

    assert session.model_override == "some/model-b"
    # No restore tracker — the manager's turn-end restore must not revert this.
    assert session._model_before_agent_switch is None
    assert session._budget_before_agent_switch is None
    assert "persists for the rest of this session" in result


def test_switch_model_session_scope_cancels_pending_turn_restore():
    session = _make_session()
    switch_model("some/model-a", _context={"session_id": session.session_id})
    assert session._model_before_agent_switch == ""

    switch_model("some/model-b", scope="session", _context={"session_id": session.session_id})
    assert session.model_override == "some/model-b"
    assert session._model_before_agent_switch is None
    assert session._budget_before_agent_switch is None


def test_switch_model_turn_scope_after_session_scope_restores_to_session_model():
    session = _make_session()
    switch_model("some/model-b", scope="session", _context={"session_id": session.session_id})

    switch_model("some/model-c", scope="turn", _context={"session_id": session.session_id})
    assert session.model_override == "some/model-c"
    # Turn-end restore returns to the session-scoped override, not the default.
    assert session._model_before_agent_switch == "some/model-b"


def test_switch_model_invalid_scope_rejected_without_mutation():
    session = _make_session()
    result = switch_model("some/model-a", scope="forever", _context={"session_id": session.session_id})

    assert "Invalid scope" in result
    assert session.model_override is None
    assert session._model_before_agent_switch is None
