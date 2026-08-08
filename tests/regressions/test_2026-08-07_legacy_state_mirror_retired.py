"""Regression: the v1 session-state layer must stay deleted.

The v1→v2 migration sat at "Stage 1 complete" for a long time: v2 was
authoritative, but every transition still mirrored itself into a 5-value
enum on the session object *and* burned a second UPDATE writing that
value to the `sessions.state` column. The mirror was lossy — CANCELLING,
FINALIZING, AWAITING_USER and AWAITING_WORKERS all collapsed to "idle" —
so any consumer that read it saw a session as idle mid-cancel. Callers
grew defensive workarounds around exactly that.

These tests pin the finished state: one state field, one DB column, one
UPDATE per transition.
"""

from __future__ import annotations

import sessions.state as state_mod
from db import models as db
from sessions import state_v2 as sv2
from sessions.state import AgentSession


def test_legacy_enum_and_mirror_field_are_gone():
    assert not hasattr(state_mod, "SessionState"), "the pre-v2 5-state enum is back"
    session = AgentSession(session_id="legacy-mirror-1")
    assert not hasattr(session, "state"), "AgentSession regrew a legacy state mirror field"
    assert not hasattr(session, "_force_state_for_tests"), "the test-only force-state hatch is back"


def test_bridge_tables_and_legacy_writer_are_gone():
    assert not hasattr(sv2, "_LEGACY_TO_V2")
    assert not hasattr(sv2, "_V2_TO_LEGACY")
    assert not hasattr(db, "set_session_state"), "the legacy-column writer is back"


def test_transition_writes_state_v2_only(monkeypatch):
    """One UPDATE per transition, and it carries state_v2 — never `state`.

    The extra legacy UPDATE was pure waste on the hot path: every turn
    pays 4+ transitions, each one previously doing two writes to the same
    row for the same event.
    """
    sid = db.create_session(title="legacy mirror")
    session = AgentSession(session_id=sid)

    writes: list[dict] = []
    real_update = db.update_session

    def spy(session_id, **fields):
        writes.append(fields)
        return real_update(session_id, **fields)

    monkeypatch.setattr(db, "update_session", spy)

    sv2.transition(session, sv2.SessionStateV2.SCOUTING, "prompt-arrived")

    assert writes == [{"state_v2": "scouting"}]
    assert db.get_session(sid)["state_v2"] == "scouting"


def test_lossy_states_persist_distinctly():
    """The four states the old mirror flattened to "idle" each survive a
    round-trip through the DB, which is the whole point of the column the
    migration replaced it with."""
    lossy = (
        sv2.SessionStateV2.CANCELLING,
        sv2.SessionStateV2.FINALIZING,
        sv2.SessionStateV2.AWAITING_USER,
        sv2.SessionStateV2.AWAITING_WORKERS,
    )
    for target in lossy:
        sid = db.create_session(title=f"lossy {target.value}")
        session = AgentSession(session_id=sid)
        sv2._set_state(session, target)
        assert db.get_session(sid)["state_v2"] == target.value
        assert db.get_sessions_in_state_v2(target.value)
