"""Regression tests for the 2026-09-04 trust-loop hardening, W2.

The loop had no ground truth. Reflect graded its own homework, and a turn the
user replied to inside the quiet window had its grade DROPPED — which on the
box left roughly a quarter of interactive turns ungraded, and threw the grade
away exactly when the best evidence for it (what the user said next) had just
arrived. There was also nowhere for the user to simply say "no".

What these tests pin:

* a real turn N+1 no longer costs turn N its grade, and turn N's evidence
  carries the user's next message while its transcript slice stays clamped to
  the turn that was actually snapshotted;
* the deterministic `next_msg_correction` reading of that message, recorded
  whatever the grader concluded;
* `outcome_source` — llm < next_turn < user — on every write path;
* migration v36 on a fresh database and on a v35 one;
* the feedback API contract W3 renders against;
* thumbs correcting the per-entry credit a verdict handed out, idempotently;
* /api/trust answering with zeros over empty tables rather than a 500.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from core.llm.types import ChatResponse, TokenUsage
from db import models as db

# ---------------------------------------------------------------------------
# Fixtures and factories
# ---------------------------------------------------------------------------


def _verdict(**overrides) -> ChatResponse:
    payload = {"verdict": "pass", "reasoning": "Task completed", "failure_cause": "none"}
    payload.update(overrides)
    return ChatResponse(
        content=json.dumps(payload),
        tool_calls=None,
        usage=TokenUsage(10, 5, 15),
        model="test",
        provider="fake",
        finish_reason="stop",
    )


def _turn(title: str = "Ground truth") -> tuple[str, int, int]:
    """A finished turn. Returns (session_id, user_msg_id, last_msg_id)."""
    sid = db.create_session(title=title)
    uid = db.add_message(sid, "user", "Fix the login bug")
    meta = json.dumps({"parent_user_msg_id": uid})
    db.add_message(sid, "assistant", "Fixed it in auth.py", metadata=meta)
    last = db.add_message(sid, "tool", "file written", metadata=meta)
    return sid, uid, last


def _snapshot(sid: str, uid: int, last: int, turn_id: int = 4):
    from sessions.hooks import _DeferredGrade

    return _DeferredGrade(
        session_id=sid,
        ticket=1,
        turn_id=turn_id,
        turn_user_msg_id=uid,
        attempt=1,
        turn_last_msg_id=last,
    )


def _session(sid: str, turn_id: int = 5):
    from sessions.state import AgentSession

    obj = AgentSession(session_id=sid)
    obj._deferred_reflect_seq = 1
    obj._turn_id = turn_id  # a real turn N+1 has started
    return obj


@pytest.fixture
def graded_now(monkeypatch):
    """Grade without waiting, and without pinning the memory-store singleton."""
    monkeypatch.setattr("config.settings.reflect_defer_idle_s", 0)
    monkeypatch.setattr("config.settings.reflect_next_turn_grading", True)
    monkeypatch.setattr("core.memory.store.get_memory_store", lambda: None)


def _evidence(mock_llm_client) -> str:
    """The evidence blob the last reflect call was handed."""
    return mock_llm_client.calls[-1]["messages"][1]["content"]


def _post_mortem(sid: str) -> dict:
    rows = db.list_post_mortems(session_id=sid)
    assert rows, "the turn was never graded"
    return rows[0]


# ---------------------------------------------------------------------------
# Next-turn grading
# ---------------------------------------------------------------------------


async def test_a_reply_no_longer_costs_the_turn_its_grade(mock_llm_client, monkeypatch, graded_now):
    """The old rule dropped this grade as "turn counter advanced". The user's
    reply is the single best piece of evidence the loop ever gets about that
    turn — dropping the grade on arrival threw it away."""
    from sessions.hooks import _deferred_reflect_task

    sid, uid, last = _turn()
    db.add_message(sid, "user", "No, that's not what I asked for — the bug is in the session cookie.")
    mock_llm_client.responses = [_verdict(verdict="retry", failure_cause="agent", reasoning="missed intent")]

    await _deferred_reflect_task(_session(sid), _snapshot(sid, uid, last))

    pm = _post_mortem(sid)
    assert pm["verdict"] == "retry"
    assert pm["outcome_source"] == "next_turn"
    evidence = _evidence(mock_llm_client)
    assert "USER'S NEXT MESSAGE (arrived after this turn" in evidence
    assert "the bug is in the session cookie" in evidence


async def test_the_next_message_is_read_deterministically_too(mock_llm_client, monkeypatch, graded_now):
    """The grader may call this a pass. The regex reading of the same text is
    stored either way, so the share of turns the user pushed back on is
    measurable without trusting the grade that graded them."""
    from sessions.hooks import _deferred_reflect_task

    sid, uid, last = _turn()
    db.add_message(sid, "user", "No, that's wrong.")
    mock_llm_client.responses = [_verdict()]  # grader says pass

    await _deferred_reflect_task(_session(sid), _snapshot(sid, uid, last))

    payload = json.loads(_post_mortem(sid)["payload_json"])
    assert payload["verdict"] == "pass"
    assert payload["next_msg_correction"] is True
    assert payload["outcome_source"] == "next_turn"


async def test_the_evidence_stops_at_the_turn_being_graded(mock_llm_client, monkeypatch, graded_now):
    """The grade now runs while turn N+1 is writing to the same transcript.
    Without the captured message-id window, reflect's "slice back to the last
    scout marker" would walk into that newer turn and grade the wrong work."""
    from sessions.hooks import _deferred_reflect_task

    sid, uid, last = _turn()
    db.add_message(sid, "user", "Try again please")
    nmeta = json.dumps({"parent_user_msg_id": last + 1})
    db.add_message(sid, "assistant", "ZEBRAFISH — the next turn's own answer", metadata=nmeta)
    mock_llm_client.responses = [_verdict()]

    await _deferred_reflect_task(_session(sid), _snapshot(sid, uid, last))

    evidence = _evidence(mock_llm_client)
    assert "Fixed it in auth.py" in evidence, "the graded turn's own work must be in the evidence"
    assert "ZEBRAFISH" not in evidence, "the next turn's transcript leaked into the graded turn"
    assert "AGENT FINAL RESPONSE:\nFixed it in auth.py" in evidence


async def test_a_worker_resume_is_not_the_user_replying(mock_llm_client, monkeypatch, graded_now):
    """The harness talking to itself is neither a trigger nor evidence — the
    2026-09-03 field case (session 3dc5a307d751) in its new home."""
    from sessions.hooks import _deferred_reflect_task, _real_turn_started
    from sessions.manager import _WORKER_RESUME_PREFIX

    sid, uid, last = _turn()
    db.add_message(sid, "user", f"{_WORKER_RESUME_PREFIX} — 2 total]")
    mock_llm_client.responses = [_verdict()]

    session_obj = _session(sid, turn_id=5)
    session_obj._synthetic_turn_ids.add(5)
    snap = _snapshot(sid, uid, last, turn_id=4)
    assert _real_turn_started(session_obj, snap) is False

    await _deferred_reflect_task(session_obj, snap)

    pm = _post_mortem(sid)
    assert pm["outcome_source"] == "llm", "a resume injection is not the user's next message"
    assert "USER'S NEXT MESSAGE" not in _evidence(mock_llm_client)


async def test_a_turn_nobody_answered_is_graded_by_the_llm_alone(mock_llm_client, monkeypatch, graded_now):
    from sessions.hooks import _deferred_reflect_task

    sid, uid, last = _turn()
    mock_llm_client.responses = [_verdict()]

    await _deferred_reflect_task(_session(sid, turn_id=4), _snapshot(sid, uid, last))

    pm = _post_mortem(sid)
    assert pm["outcome_source"] == "llm"
    assert "next_msg_correction" not in json.loads(pm["payload_json"])


async def test_the_legacy_rule_still_drops_the_grade_when_the_flag_is_off(mock_llm_client, monkeypatch, graded_now):
    from sessions.hooks import _deferred_reflect_task

    monkeypatch.setattr("config.settings.reflect_next_turn_grading", False)
    sid, uid, last = _turn()
    db.add_message(sid, "user", "and now the logout bug")

    await _deferred_reflect_task(_session(sid), _snapshot(sid, uid, last))

    assert db.list_post_mortems(session_id=sid) == []
    assert mock_llm_client.call_count == 0
    assert any("superseded by a newer turn" in m["content"] for m in db.get_messages(sid) if m["role"] == "notice")


async def test_one_deferred_grade_at_a_time_per_session(monkeypatch, graded_now):
    """ "Every real turn gets graded" is only affordable if a rapid-fire burst
    queues rather than fans out. The session's lock is that bound."""
    from sessions.hooks import _deferred_reflect_task

    sid, uid, last = _turn()
    session_obj = _session(sid)
    events: list = []
    inside: list = []

    async def _fake_run(session_obj, snap, next_user_message=""):
        assert not inside, "two deferred grades ran into the same session at once"
        inside.append(snap.turn_id)
        events.append(("enter", snap.turn_id))
        await asyncio.sleep(0)  # a real grade awaits; so must the fake
        events.append(("exit", snap.turn_id))
        inside.pop()

    monkeypatch.setattr("sessions.hooks._run_deferred_reflect", _fake_run)

    await asyncio.gather(
        _deferred_reflect_task(session_obj, _snapshot(sid, uid, last, turn_id=1)),
        _deferred_reflect_task(session_obj, _snapshot(sid, uid, last, turn_id=2)),
    )

    # Both turns were graded, and neither grade started before the other
    # finished. Which of the two goes first is scheduling, not policy.
    assert sorted(turn for kind, turn in events if kind == "enter") == [1, 2]
    assert [kind for kind, _ in events] == ["enter", "exit", "enter", "exit"]
    assert events[0][1] == events[1][1] and events[2][1] == events[3][1]


# ---------------------------------------------------------------------------
# The deterministic pre-check
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "No, I wanted the other one",
        "that's wrong",
        "That’s wrong — check the log",  # curly apostrophe
        "This is not what I asked for",
        "you didn't run the tests",
        "you didn’t read the file",
        "try again with the real data",
        "it's still broken",
        "the second column is wrong",
        "That total is incorrect",
        "please redo the summary",
        "TRY AGAIN",
    ],
)
def test_next_msg_correction_catches_a_push_back(message):
    from core.reflect import next_msg_correction

    assert next_msg_correction(message) is True


@pytest.mark.parametrize(
    "message",
    [
        "thanks, that's perfect",
        "now also add a test for the logout path",
        "great — next let's look at the deploy script",
        "no worries, I'll take it from here",
        "can you explain why the token expires?",
        "Nowhere in the docs does it say that",  # 'no' without the comma
        "the wrongdoing report is unrelated",  # substring, not a word
        "",
    ],
)
def test_next_msg_correction_leaves_moving_on_alone(message):
    from core.reflect import next_msg_correction

    assert next_msg_correction(message) is False


def test_next_msg_correction_reads_what_the_verifier_reads():
    """The flag describes the same text the evidence carries, so a post-mortem
    can never claim a push-back the grader was never shown."""
    from core.reflect import NEXT_MSG_CHAR_CAP, next_msg_correction

    assert next_msg_correction("ok " * NEXT_MSG_CHAR_CAP + "wrong") is False


# ---------------------------------------------------------------------------
# Migration v36
# ---------------------------------------------------------------------------


def _columns(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_migration_v36_on_a_fresh_database():
    from db.database import _get_schema_version, connect_sessions

    conn = connect_sessions()
    try:
        assert _get_schema_version(conn) >= 36
        assert _columns(conn, "post_mortems") >= {"user_signal", "outcome_source"}
        assert _columns(conn, "message_feedback") == {"id", "session_id", "message_id", "signal", "note", "created_at"}
        # A thumb is a state, not an event log.
        conn.execute(
            "INSERT INTO message_feedback (session_id, message_id, signal, note, created_at)"
            " VALUES ('s', '1', 'up', '', 'now')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO message_feedback (session_id, message_id, signal, note, created_at)"
                " VALUES ('s', '1', 'down', '', 'now')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO message_feedback (session_id, message_id, signal, note, created_at)"
                " VALUES ('s', '2', 'sideways', '', 'now')"
            )
    finally:
        conn.close()


def test_migration_v36_upgrades_a_v35_database(tmp_path, monkeypatch):
    """Built at v35 for real, then upgraded — including the backfill that says
    every outcome recorded before today rested on the grader alone."""
    from db import database as dbm

    path = str(tmp_path / "v35.db")
    full = dbm.MIGRATIONS
    conn = dbm._connect(path)
    try:
        conn.executescript(dbm._SESSIONS_SCHEMA)
        dbm._set_schema_version(conn, 1)
        conn.commit()

        monkeypatch.setattr(dbm, "MIGRATIONS", [m for m in full if m[0] <= 35])
        dbm._run_migrations(conn)
        assert dbm._get_schema_version(conn) == 35
        assert "outcome_source" not in _columns(conn, "post_mortems")

        conn.execute("INSERT INTO sessions (id, title) VALUES ('s', 'Legacy')")
        conn.execute("""INSERT INTO post_mortems (id, session_id, created_at, attempt, verdict,
                   failure_cause, confidence, payload_json)
               VALUES ('legacy', 's', '2026-09-01T00:00:00+00:00', 1, 'pass', 'none', 0.9, '{}')""")
        conn.commit()

        monkeypatch.setattr(dbm, "MIGRATIONS", full)
        dbm._run_migrations(conn)

        assert dbm._get_schema_version(conn) == max(m[0] for m in full)
        assert _columns(conn, "post_mortems") >= {"user_signal", "outcome_source"}
        row = conn.execute("SELECT outcome_source, user_signal FROM post_mortems WHERE id = 'legacy'").fetchone()
        assert row["outcome_source"] == "llm", "existing rows must be backfilled, not left NULL"
        assert row["user_signal"] is None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The feedback API (the contract W3 renders against)
# ---------------------------------------------------------------------------


def _feedback_app() -> FastAPI:
    from api.routers import sessions as sessions_router

    app = FastAPI()
    app.include_router(sessions_router.router)
    return app


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_feedback_upserts_and_deletes():
    sid, uid, last = _turn()
    aid = [m["id"] for m in db.get_messages(sid) if m["role"] == "assistant"][0]
    async with await _client(_feedback_app()) as c:
        url = f"/api/sessions/{sid}/messages/{aid}/feedback"

        r = await c.post(url, json={"signal": "down", "note": "missed the point"})
        assert r.status_code == 200
        assert r.json() == {"message_id": str(aid), "signal": "down", "note": "missed the point"}

        # A thumb is a state: the other thumb replaces it, it does not append.
        r = await c.post(url, json={"signal": "up"})
        assert r.json() == {"message_id": str(aid), "signal": "up", "note": ""}
        listed = (await c.get(f"/api/sessions/{sid}/feedback")).json()["items"]
        assert len(listed) == 1
        assert listed[0]["signal"] == "up"
        assert set(listed[0]) == {"message_id", "signal", "note", "created_at"}

        r = await c.post(url, json={"signal": None})
        assert r.json() == {"message_id": str(aid), "signal": None, "note": ""}
        assert (await c.get(f"/api/sessions/{sid}/feedback")).json() == {"items": []}


async def test_feedback_rejects_what_it_cannot_be_a_verdict_on():
    sid, uid, last = _turn()
    other = db.create_session(title="Elsewhere")
    async with await _client(_feedback_app()) as c:
        # A tool result and the user's own message have no turn outcome to argue with.
        assert (await c.post(f"/api/sessions/{sid}/messages/{last}/feedback", json={"signal": "up"})).status_code == 400
        assert (await c.post(f"/api/sessions/{sid}/messages/{uid}/feedback", json={"signal": "up"})).status_code == 400
        # Wrong session, missing message, unparseable id, invalid signal.
        aid = [m["id"] for m in db.get_messages(sid) if m["role"] == "assistant"][0]
        assert (
            await c.post(f"/api/sessions/{other}/messages/{aid}/feedback", json={"signal": "up"})
        ).status_code == 404
        assert (await c.post(f"/api/sessions/{sid}/messages/99999/feedback", json={"signal": "up"})).status_code == 404
        assert (await c.post(f"/api/sessions/{sid}/messages/abc/feedback", json={"signal": "up"})).status_code == 404
        assert (
            await c.post(f"/api/sessions/{sid}/messages/{aid}/feedback", json={"signal": "sideways"})
        ).status_code == 400
        assert (await c.get("/api/sessions/nope/feedback")).status_code == 404


# ---------------------------------------------------------------------------
# Thumbs correcting the credit a verdict handed out
# ---------------------------------------------------------------------------


def _graded_turn_citing(entry_id: str, verdict: str, hint_id: str = "") -> tuple[str, int, str]:
    """A graded turn whose outcome was attributed to `entry_id`."""
    sid, uid, last = _turn()
    aid = [m["id"] for m in db.get_messages(sid) if m["role"] == "assistant"][0]
    payload = {
        "verdict": verdict,
        "outcome_source": "llm",
        "turn_user_msg_id": uid,
        "cited_policies": [entry_id],
    }
    if hint_id:
        payload["scout_summary"] = {"used_hints": [hint_id]}
    pm_id = db.add_post_mortem(
        session_id=sid,
        attempt=1,
        verdict=verdict,
        failure_cause="none" if verdict == "pass" else "agent",
        confidence=0.8,
        reflect_model="m",
        reflect_latency_ms=1,
        scout_viability=None,
        execution_mode=None,
        payload_json=json.dumps(payload),
    )
    # The counters these tests pre-seed are the credit synthesis handed out
    # for this grade; a thumb corrects credit only once it exists.
    db.mark_post_mortems_synthesized([pm_id])
    return sid, aid, entry_id


def _counters(entry_id: str) -> tuple[int, int, int]:
    row = db.get_signal("adaptive_entry", entry_id) or {}
    return int(row.get("successes") or 0), int(row.get("failures") or 0), int(row.get("reinforcements") or 0)


def test_thumbs_up_on_a_non_pass_gives_the_credit_back():
    from core.feedback import apply_user_signal

    db.upsert_signal("adaptive_entry", "pol-1", delta_failures=1)
    sid, aid, entry = _graded_turn_citing("pol-1", "retry", hint_id="hint-1")

    apply_user_signal(sid, aid, "up")

    assert _counters("pol-1") == (1, 0, 1), "the failure it was blamed for is taken back"
    assert _counters("hint-1") == (1, 0, 0), "a use is not re-counted; only the outcome moves"
    pm = db.list_post_mortems(session_id=sid)[0]
    assert pm["user_signal"] == "up"
    assert pm["outcome_source"] == "user"


def test_thumbs_down_on_a_pass_takes_the_credit_away():
    from core.feedback import apply_user_signal

    db.upsert_signal("adaptive_entry", "pol-2", delta_successes=1)
    sid, aid, entry = _graded_turn_citing("pol-2", "pass")

    apply_user_signal(sid, aid, "down")

    assert _counters("pol-2") == (0, 1, 1)


def test_an_agreeing_thumb_moves_nothing():
    """The forward attribution already recorded agreement; applying it again
    would count one observation twice."""
    from core.feedback import apply_user_signal

    db.upsert_signal("adaptive_entry", "pol-3", delta_successes=1)
    sid, aid, entry = _graded_turn_citing("pol-3", "pass")

    report = apply_user_signal(sid, aid, "up")

    assert report["applied"] == {}
    assert _counters("pol-3") == (1, 0, 1)
    assert db.list_post_mortems(session_id=sid)[0]["outcome_source"] == "user"


def test_the_same_thumb_twice_is_not_two_corrections():
    from core.feedback import apply_user_signal

    db.upsert_signal("adaptive_entry", "pol-4", delta_successes=1)
    sid, aid, entry = _graded_turn_citing("pol-4", "pass")

    apply_user_signal(sid, aid, "down")
    apply_user_signal(sid, aid, "down")

    assert _counters("pol-4") == (0, 1, 1)


def test_flipping_and_withdrawing_a_thumb_reverses_exactly_what_it_applied():
    from core.feedback import apply_user_signal

    db.upsert_signal("adaptive_entry", "pol-5", delta_successes=1)
    sid, aid, entry = _graded_turn_citing("pol-5", "pass")
    before = _counters("pol-5")

    apply_user_signal(sid, aid, "down")
    assert _counters("pol-5") == (0, 1, 1)

    apply_user_signal(sid, aid, "up")  # agrees with the pass — nothing to correct
    assert _counters("pol-5") == before

    apply_user_signal(sid, aid, None)
    assert _counters("pol-5") == before
    pm = db.list_post_mortems(session_id=sid)[0]
    assert pm["user_signal"] is None
    assert pm["outcome_source"] == "llm", "withdrawing a thumb restores the grade's own source"


def test_a_correction_never_drives_a_counter_negative():
    """scout_signals counters are cumulative evidence. A take-back of a
    failure that was never recorded would corrupt every ratio read from it."""
    from core.feedback import apply_user_signal

    sid, aid, entry = _graded_turn_citing("pol-6", "retry")  # no prior signal row at all

    apply_user_signal(sid, aid, "up")

    successes, failures, _ = _counters("pol-6")
    assert (successes, failures) == (1, 0)


def test_a_thumb_on_an_ungraded_turn_is_still_recorded():
    from core.feedback import apply_user_signal

    sid, uid, last = _turn()
    aid = [m["id"] for m in db.get_messages(sid) if m["role"] == "assistant"][0]

    report = apply_user_signal(sid, aid, "down")

    assert report["post_mortem_id"] is None
    assert report["applied"] == {}


# ---------------------------------------------------------------------------
# /api/trust
# ---------------------------------------------------------------------------


async def _trust() -> dict:
    from api.routers import trust

    app = FastAPI()
    app.include_router(trust.router)
    async with await _client(app) as c:
        resp = await c.get("/api/trust")
    assert resp.status_code == 200
    return resp.json()


async def test_trust_answers_with_zeros_over_empty_tables():
    """Half of what this reports is written by sibling workstreams. A
    dashboard that 500s until the last of them lands cannot be used to watch
    them land."""
    assert await _trust() == {
        "grader": {"agreement": 0.0, "n": 0, "holdout": None},
        "outcomes": {"by_source": {"llm": 0, "next_turn": 0, "user": 0}, "graded_7d": 0, "user_turns_7d": 0},
        "entries": {"by_status": {}, "unfounded": 0},
        "canaries": {"contaminated_14d": 0, "runs_14d": 0, "fails_14d": 0},
        "trials": [],
    }


async def test_trust_reports_the_outcome_mix_and_the_graders_report_card():
    from core.feedback import apply_user_signal

    sid, aid, entry = _graded_turn_citing("pol-7", "pass")
    apply_user_signal(sid, aid, "down")  # the user disagrees with the grader
    db.add_post_mortem(
        session_id=sid,
        attempt=1,
        verdict="pass",
        failure_cause="none",
        confidence=0.9,
        reflect_model="m",
        reflect_latency_ms=1,
        scout_viability=None,
        execution_mode=None,
        payload_json="{}",
        outcome_source="next_turn",
    )

    body = await _trust()

    assert body["outcomes"]["by_source"] == {"llm": 0, "next_turn": 1, "user": 1}
    assert body["outcomes"]["graded_7d"] == 2
    assert body["outcomes"]["user_turns_7d"] == 1
    assert body["grader"] == {"agreement": 0.0, "n": 1, "holdout": None}


async def test_trust_reads_the_grader_holdout_when_one_exists():
    db.set_snooze_state("trust.grader_holdout", json.dumps({"accuracy": 0.9, "n": 10, "model": "qwen"}))

    assert (await _trust())["grader"]["holdout"] == {"accuracy": 0.9, "n": 10, "model": "qwen"}


async def test_trust_survives_a_holdout_value_that_is_not_json():
    db.set_snooze_state("trust.grader_holdout", "not json at all")

    assert (await _trust())["grader"]["holdout"] is None


# ---------------------------------------------------------------------------
# Outcome precedence is one rule, not three (found live on 2026-09-04: a
# thumb on turn 2's answer landed on turn 1's grade, because turn 1's
# next-turn grade was written inside turn 2's time window)
# ---------------------------------------------------------------------------


def test_a_grade_written_during_the_next_turn_is_not_the_next_turns_grade():
    """Turn 1 is graded once turn 2's message arrives, so its post-mortem is
    created inside turn 2's window. The window fallback exists for rows
    written before turn anchors existed; an anchored row is never a match
    for a different turn."""
    sid, uid1, _ = _turn()
    uid2 = db.add_message(sid, "user", "No, that's wrong. Try again.")
    meta2 = json.dumps({"parent_user_msg_id": uid2})
    aid2 = db.add_message(sid, "assistant", "Sorry, here it is.", metadata=meta2)
    db.add_post_mortem(
        session_id=sid,
        attempt=1,
        verdict="retry",
        failure_cause="agent",
        confidence=0.7,
        reflect_model="m",
        reflect_latency_ms=1,
        scout_viability=None,
        execution_mode=None,
        payload_json=json.dumps({"turn_user_msg_id": uid1, "outcome_source": "next_turn"}),
        outcome_source="next_turn",
    )

    assert db.latest_post_mortem_for_turn(sid, uid1)["verdict"] == "retry"
    assert db.latest_post_mortem_for_turn(sid, uid2) is None, "turn 2 has no grade yet"
    assert db.set_post_mortem_user_signal(sid, aid2, "down") is None
    assert db.latest_post_mortem_for_turn(sid, uid1)["user_signal"] is None, "turn 1's grade is untouched"


def test_attribution_takes_the_thumb_over_the_grade():
    """Synthesis is where credit is handed out, so the thumb has to win
    there — not only in a correction applied afterwards."""
    from core.synthesis import attribute

    base = {
        "confidence": 0.9,
        "scout_viability": None,
        "execution_mode": None,
        "payload_json": json.dumps({"cited_policies": ["pol-x"], "scout_summary": {"used_hints": ["hint-x"]}}),
    }
    down_on_pass = attribute({**base, "verdict": "pass", "failure_cause": "none", "user_signal": "down"})
    assert any(a.subject == "pol-x" and a.delta_failures == 1 for a in down_on_pass)
    assert not any(a.subject == "pol-x" and a.delta_successes for a in down_on_pass)

    up_on_low_confidence_retry = attribute(
        {**base, "verdict": "retry", "failure_cause": "agent", "confidence": 0.2, "user_signal": "up"}
    )
    assert any(
        a.subject == "pol-x" and a.delta_successes == 1 for a in up_on_low_confidence_retry
    ), "the confidence floor guards the model's guess, not the user's"

    untouched = attribute({**base, "verdict": "pass", "failure_cause": "none", "user_signal": None})
    assert any(a.subject == "pol-x" and a.delta_successes == 1 for a in untouched)


def test_a_thumb_before_synthesis_waits_for_synthesis():
    """Correcting credit that was never handed out would count the thumb
    twice once synthesis reads user_signal itself."""
    from core.feedback import apply_user_signal

    sid, uid, _ = _turn()
    aid = [m["id"] for m in db.get_messages(sid) if m["role"] == "assistant"][0]
    db.add_post_mortem(
        session_id=sid,
        attempt=1,
        verdict="pass",
        failure_cause="none",
        confidence=0.8,
        reflect_model="m",
        reflect_latency_ms=1,
        scout_viability=None,
        execution_mode=None,
        payload_json=json.dumps({"turn_user_msg_id": uid, "cited_policies": ["pol-y"]}),
    )

    report = apply_user_signal(sid, aid, "down")

    assert report["applied"] == {}
    assert db.get_signal("adaptive_entry", "pol-y") in (None, {}) or not (
        db.get_signal("adaptive_entry", "pol-y") or {}
    ).get("failures")
    pm = db.list_post_mortems(session_id=sid)[0]
    assert pm["user_signal"] == "down" and pm["outcome_source"] == "user", "the stamp is what synthesis will read"


async def test_a_thumb_that_landed_before_the_grade_rides_on_it(mock_llm_client, monkeypatch, graded_now):
    """The usual order: the user reacts right away, the grade arrives with
    the next message or after the idle wait. The grade must pick the thumb
    up, or most thumbs would never reach a post-mortem."""
    from sessions.hooks import _deferred_reflect_task

    sid, uid, last = _turn()
    aid = [m["id"] for m in db.get_messages(sid) if m["role"] == "assistant"][0]
    db.upsert_message_feedback(sid, aid, "down", "not it")
    db.add_message(sid, "user", "Moving on — what about the logout flow?")
    mock_llm_client.responses = [_verdict()]  # grader says pass

    await _deferred_reflect_task(_session(sid), _snapshot(sid, uid, last))

    pm = _post_mortem(sid)
    assert pm["verdict"] == "pass"
    assert pm["user_signal"] == "down"
    assert pm["outcome_source"] == "user"
