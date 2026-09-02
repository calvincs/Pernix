"""The per-turn read model behind the State timeline.

The timeline used to fetch the whole state log and the whole transcript and
join them in the browser. `db.get_turns` / `GET /api/sessions/{id}/turns` do
that join server-side, one record per turn. These tests build a session with
the shapes a real one carries — a scout report, assistant rows holding
tool_calls, tool rows with the executor's `was_error` stamp, a reflect retry
chain, an eval gate attempt, a compaction marker, a notice, and token_usage
rows — and pin the contract the UI reads.
"""

import json
import time

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from db import models as db

# The three turns the fixture builds. Turn 1 retries once (so its phases run
# scouting→processing→finalizing twice), turn 2 compacts mid-loop, turn 3 is
# still running: it has an opening transition and no closing one.
T1_START = 1_760_000_000_000
SECOND = 1000


def _tick(base, *offsets):
    return [base + o for o in offsets]


def _log(sid, turn, frm, to, reason, ts, **kw):
    return db.add_state_log(sid, turn_id=turn, from_state=frm, to_state=to, reason=reason, timestamp_ms=ts, **kw)


def _iso(ms):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def _msg(sid, role, ms, content="", **kw):
    """Insert a message and force its created_at onto the fixture's clock —
    add_message stamps `now`, and the join is by time window."""
    mid = db.add_message(sid, role, content, **kw)
    from db.database import connect_sessions

    with connect_sessions() as conn:
        conn.execute("UPDATE messages SET created_at = ? WHERE id = ?", (_iso(ms), mid))
    return mid


def _usage(sid, ms, prompt, completion, model="test-model", cost=None):
    db.add_token_usage(
        sid,
        model=model,
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        cost_estimate=cost,
    )
    from db.database import connect_sessions
    from db.models import _turn_usage_bound

    with connect_sessions() as conn:
        row = conn.execute("SELECT MAX(id) AS m FROM token_usage WHERE session_id = ?", (sid,)).fetchone()
        conn.execute("UPDATE token_usage SET created_at = ? WHERE id = ?", (_turn_usage_bound(ms), row["m"]))


def _tool_round(sid, ms, calls, results):
    """One agent round: an assistant row carrying `calls`, then a tool row per
    result. `calls` is [(id, name, args_dict)]; `results` is
    [(id, content, latency_ms, was_error)]."""
    _msg(
        sid,
        "assistant",
        ms,
        "",
        tool_calls=json.dumps([{"id": cid, "name": name, "arguments": json.dumps(args)} for cid, name, args in calls]),
        metadata=json.dumps({"model": "test-model", "latency_ms": 900}),
    )
    for cid, content, latency, was_error in results:
        _msg(
            sid,
            "tool",
            ms + 1,
            content,
            tool_call_id=cid,
            latency_ms=latency,
            metadata=json.dumps({"was_error": was_error, "latency_ms": latency}),
        )


@pytest.fixture
def three_turns():
    """A session with two finished turns and one still running.

    Turn 1: prompt → scout → two tool calls (one failing) → reflect says
            retry → re-scout → one tool call → reflect passes → complete.
    Turn 2: prompt → scout → tool call → compaction round trip → an eval
            gate attempt → complete, with a notice from the post-hooks.
    Turn 3: prompt → scout → still processing. No closing transition.
    """
    sid = db.create_session(title="Timeline fixture")

    # ---- turn 1 (one reflect retry) ------------------------------------
    a, b, c, d, e, f, g = _tick(
        T1_START, 0, 20 * SECOND, 50 * SECOND, 80 * SECOND, 95 * SECOND, 120 * SECOND, 140 * SECOND
    )
    _msg(sid, "user", a - 2, "do the thing")
    _log(sid, 1, "idle_ready", "scouting", "prompt-arrived", a)
    _msg(
        sid,
        "scout",
        a + SECOND,
        json.dumps(
            {
                "type": "scout.done",
                "reused_prior": False,
                "tools": ["bash", "file_read"],
                "tool_rationale": "shell and a read",
                "approach": "first attempt",
                "memory": "",
                "model": "",
                "scout_model": "scout-model",
                "latency_ms": 19_000,
                "from_cache": False,
                "from_fallback": False,
            }
        ),
    )
    _log(sid, 1, "scouting", "processing", "scout-done", b)
    _tool_round(
        sid,
        b + 5 * SECOND,
        [("call_a", "bash", {"command": "ls -la"}), ("call_b", "file_read", {"path": "notes.md"})],
        [("call_a", "total 0", 41, False), ("call_b", "Error: File not found: notes.md", 7, True)],
    )
    _usage(sid, b + 5 * SECOND, 1000, 20)
    _log(sid, 1, "processing", "finalizing", "loop-complete", c, termination_reason="complete")
    _msg(
        sid,
        "reflect",
        c + 2 * SECOND,
        json.dumps(
            {
                "verdict": "retry",
                "reasoning": "the read failed and was not retried",
                "diagnostic": "gave up early",
                "what_worked": "the shell call",
            }
        ),
    )
    _log(sid, 1, "finalizing", "scouting", "reflect-retry", d, retry_index=1, reflect_count=1)
    _msg(
        sid,
        "scout",
        d + SECOND,
        json.dumps({"type": "scout.done", "reused_prior": True, "tools": ["file_read"], "approach": "second attempt"}),
    )
    _log(sid, 1, "scouting", "processing", "scout-done", e, retry_index=1, reflect_count=1)
    _tool_round(
        sid,
        e + 2 * SECOND,
        [("call_c", "file_read", {"path": "README.md"})],
        [("call_c", "# readme", 12, False)],
    )
    _usage(sid, e + 2 * SECOND, 2000, 30)
    _log(
        sid,
        1,
        "processing",
        "finalizing",
        "loop-complete",
        f,
        retry_index=1,
        reflect_count=1,
        termination_reason="complete",
    )
    _msg(
        sid,
        "reflect",
        f + SECOND,
        json.dumps({"verdict": "pass", "reasoning": "delivered", "diagnostic": "", "what_worked": "the retry"}),
    )
    _log(sid, 1, "finalizing", "idle_ready", "turn-complete", g, retry_index=1, reflect_count=1)

    # ---- turn 2 (compaction + eval gate) --------------------------------
    base2 = T1_START + 300 * SECOND
    h, i, j, k, m, n = _tick(base2, 0, 15 * SECOND, 40 * SECOND, 60 * SECOND, 90 * SECOND, 100 * SECOND)
    _msg(sid, "user", h - 2, "and again")
    _log(sid, 2, "idle_ready", "scouting", "prompt-arrived", h)
    _msg(sid, "scout", h + SECOND, "{not json at all")
    _log(sid, 2, "scouting", "processing", "scout-done", i)
    _tool_round(sid, i + 2 * SECOND, [("call_d", "bash", {"command": "pwd"})], [("call_d", "/app", 5, False)])
    _usage(sid, i + 2 * SECOND, 4000, 60, cost=0.25)
    _log(sid, 2, "processing", "compacting", "compact-proactive", j, compaction_count=1)
    _msg(
        sid,
        "compaction",
        j + SECOND,
        '```json\n{"goal": "keep going", "progress": ["read a file"]}\n```\n\nA prose recap the model adds after the fence.',
        metadata=json.dumps({"compacted_up_to": 42, "original_count": 190}),
    )
    _log(sid, 2, "compacting", "processing", "compact-done", k, compaction_count=1)
    _usage(sid, k + SECOND, 500, 10, cost=0.05)
    _log(sid, 2, "processing", "finalizing", "loop-complete", m, compaction_count=1, termination_reason="complete")
    _msg(
        sid,
        "eval",
        m + SECOND,
        json.dumps(
            {
                "kind": "gate",
                "attempt": 1,
                "gates": [
                    {
                        "kind": "gate",
                        "name": "tests",
                        "command": "pytest -q",
                        "passed": False,
                        "exit_code": 1,
                        "output_tail": "1 failed",
                        "reused": False,
                        "error": "",
                    }
                ],
            }
        ),
    )
    _msg(sid, "notice", m + 2 * SECOND, "💭 [contradiction] two answers disagree")
    _log(sid, 2, "finalizing", "idle_ready", "turn-complete", n, compaction_count=1, eval_count=1)

    # ---- turn 3 (still running) -----------------------------------------
    base3 = int(time.time() * 1000) - 30 * SECOND
    _msg(sid, "user", base3 - 2, "one more")
    _log(sid, 3, "idle_ready", "scouting", "prompt-arrived", base3)
    _msg(sid, "scout", base3 + SECOND, json.dumps({"type": "scout.done", "approach": "live", "tools": []}))
    _log(sid, 3, "scouting", "processing", "scout-done", base3 + 10 * SECOND)
    _tool_round(sid, base3 + 12 * SECOND, [("call_e", "grep", {"pattern": "x"})], [("call_e", "match", 3, False)])
    _usage(sid, base3 + 12 * SECOND, 700, 5)
    return sid


def _by_id(page):
    return {t["turn_id"]: t for t in page["turns"]}


# ---------------------------------------------------------------------------
# Shape and ordering
# ---------------------------------------------------------------------------


def test_turns_are_newest_first_and_counted(three_turns):
    page = db.get_turns(three_turns)
    assert page["session_id"] == three_turns
    assert page["count"] == 3
    assert page["has_more"] is False
    assert [t["turn_id"] for t in page["turns"]] == [3, 2, 1]


def test_paging_walks_backward_without_overlap(three_turns):
    first = db.get_turns(three_turns, limit=2)
    assert [t["turn_id"] for t in first["turns"]] == [3, 2]
    assert first["has_more"] is True

    second = db.get_turns(three_turns, before_turn=2, limit=2)
    assert [t["turn_id"] for t in second["turns"]] == [1]
    assert second["has_more"] is False

    # The page behind the oldest turn is empty, not an error.
    assert db.get_turns(three_turns, before_turn=1)["turns"] == []


def test_limit_is_clamped_to_a_hundred(three_turns):
    assert db.get_turns(three_turns, limit=5000)["count"] == 3
    assert db.get_turns(three_turns, limit=0)["count"] == 1
    assert db.get_turns(three_turns, limit=-4)["count"] == 1


def test_unknown_session_is_an_empty_page(three_turns):
    page = db.get_turns("no-such-session")
    assert page == {"session_id": "no-such-session", "count": 0, "has_more": False, "turns": []}


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------


def test_phases_are_in_order_and_sum_to_the_turn(three_turns):
    turn = _by_id(db.get_turns(three_turns))[1]
    assert [p["state"] for p in turn["phases"]] == [
        "scouting",
        "processing",
        "finalizing",
        "scouting",
        "processing",
        "finalizing",
    ]
    assert sum(p["elapsed_ms"] for p in turn["phases"]) == turn["elapsed_ms"] == 140 * SECOND
    first = turn["phases"][0]
    assert first["reason_in"] == "prompt-arrived"
    assert first["reason_out"] == "scout-done"
    assert first["elapsed_ms"] == 20 * SECOND
    assert all(p["ended_at"] is not None for p in turn["phases"])
    # The idle time before the prompt is NOT part of the turn: the opening
    # row's own elapsed_ms measures the previous idle stretch.
    assert turn["started_at"] < turn["ended_at"]


def test_compaction_round_trip_is_its_own_phase(three_turns):
    turn = _by_id(db.get_turns(three_turns))[2]
    assert [p["state"] for p in turn["phases"]] == ["scouting", "processing", "compacting", "processing", "finalizing"]
    assert sum(p["elapsed_ms"] for p in turn["phases"]) == turn["elapsed_ms"]
    assert turn["compaction_count"] == 1


# ---------------------------------------------------------------------------
# The running turn
# ---------------------------------------------------------------------------


def test_running_turn_is_open_ended(three_turns):
    turn = _by_id(db.get_turns(three_turns))[3]
    assert turn["running"] is True
    assert turn["ended_at"] is None
    assert turn["termination_reason"] is None
    assert turn["phases"][-1]["state"] == "processing"
    assert turn["phases"][-1]["ended_at"] is None
    assert turn["phases"][-1]["reason_out"] is None
    # Still ticking: elapsed reaches to now, and the phases still sum to it.
    assert turn["elapsed_ms"] >= 30 * SECOND
    assert sum(p["elapsed_ms"] for p in turn["phases"]) == turn["elapsed_ms"]
    assert turn["tool_calls"], "a running turn still reports the work done so far"


def test_finished_turns_are_not_running(three_turns):
    for turn in db.get_turns(three_turns)["turns"][1:]:
        assert turn["running"] is False
        assert turn["ended_at"] is not None


def test_a_turn_left_unclosed_by_a_crash_is_not_running(three_turns):
    """Only the newest turn can still be live. An older turn with no closing
    transition was abandoned — say so instead of letting it tick forever."""
    _log(three_turns, 4, "idle_ready", "scouting", "prompt-arrived", int(time.time() * 1000))
    turn = _by_id(db.get_turns(three_turns))[3]
    assert turn["running"] is False
    assert turn["ended_at"] is not None
    assert "turn-never-closed" in turn["invariant_violations"]


# ---------------------------------------------------------------------------
# Tool calls
# ---------------------------------------------------------------------------


def test_tool_calls_carry_name_args_latency_and_error(three_turns):
    turn = _by_id(db.get_turns(three_turns))[1]
    assert [c["name"] for c in turn["tool_calls"]] == ["bash", "file_read", "file_read"]
    assert [c["latency_ms"] for c in turn["tool_calls"]] == [41, 7, 12]
    assert [c["was_error"] for c in turn["tool_calls"]] == [False, True, False]
    assert [c["call_id"] for c in turn["tool_calls"]] == ["call_a", "call_b", "call_c"]
    assert turn["tool_calls"][0]["args_summary"] == "command: ls -la"
    assert turn["tool_calls"][1]["args_summary"] == "path: notes.md"
    assert all(c["started_at"] for c in turn["tool_calls"])
    assert all(isinstance(c["message_id"], int) for c in turn["tool_calls"])


def test_args_summary_is_capped(three_turns):
    """A file_write's arguments are the whole file; the digest is a header,
    not a payload."""
    _tool_round(
        three_turns,
        T1_START + 30 * SECOND,
        [("call_big", "file_write", {"path": "big.txt", "content": "x" * 5000})],
        [("call_big", "written", 9, False)],
    )
    turn = _by_id(db.get_turns(three_turns))[1]
    big = [c for c in turn["tool_calls"] if c["name"] == "file_write"][0]
    assert big["args_summary"].startswith("path: big.txt")
    assert len(big["args_summary"]) <= 160


def test_tool_row_without_a_stamp_falls_back_to_the_content(three_turns):
    """Rows written before metadata.was_error existed still have to read as
    failures — that is what the timeline modal has always shown."""
    _msg(
        three_turns,
        "tool",
        T1_START + 30 * SECOND,
        "Error: something went wrong",
        tool_call_id="call_legacy",
        latency_ms=3,
    )
    turn = _by_id(db.get_turns(three_turns))[1]
    legacy = [c for c in turn["tool_calls"] if c["call_id"] == "call_legacy"][0]
    assert legacy["was_error"] is True
    assert legacy["name"] == "tool"  # no assistant row claims it


# ---------------------------------------------------------------------------
# Scout, reflect, eval, compaction, notices
# ---------------------------------------------------------------------------


def test_scout_report_is_the_one_the_turn_opened_with(three_turns):
    turn = _by_id(db.get_turns(three_turns))[1]
    assert turn["scout"]["approach"] == "first attempt"
    assert turn["scout"]["tools"] == ["bash", "file_read"]
    assert turn["scout"]["scout_model"] == "scout-model"
    assert turn["scout"]["from_fallback"] is False


def test_reflect_is_the_retry_chain_in_order(three_turns):
    turn = _by_id(db.get_turns(three_turns))[1]
    assert [r["attempt"] for r in turn["reflect"]] == [1, 2]
    assert [r["verdict"] for r in turn["reflect"]] == ["retry", "pass"]
    assert turn["reflect"][0]["diagnostic"] == "gave up early"
    assert turn["reflect"][1]["what_worked"] == "the retry"
    assert turn["retry_index"] == 1
    assert turn["reflect_count"] == 1


def test_eval_gates_are_reported_per_attempt(three_turns):
    turn = _by_id(db.get_turns(three_turns))[2]
    assert len(turn["eval"]) == 1
    attempt = turn["eval"][0]
    assert attempt["attempt"] == 1
    assert attempt["gates"] == [
        {"name": "tests", "command": "pytest -q", "passed": False, "exit_code": 1, "output_tail": "1 failed"}
    ]
    assert turn["eval_count"] == 1


def test_compaction_summary_is_parsed_out_of_its_fence(three_turns):
    turn = _by_id(db.get_turns(three_turns))[2]
    assert len(turn["compactions"]) == 1
    comp = turn["compactions"][0]
    assert comp["summary"] == {"goal": "keep going", "progress": ["read a file"]}
    assert comp["compacted_up_to"] == 42
    assert comp["original_count"] == 190
    assert comp["at"]


def test_notices_ride_along_with_their_text_and_time(three_turns):
    turn = _by_id(db.get_turns(three_turns))[2]
    assert [n["text"] for n in turn["notices"]] == ["💭 [contradiction] two answers disagree"]
    assert turn["notices"][0]["at"]


def test_malformed_json_becomes_raw_not_a_crash(three_turns):
    """A half-written scout body must degrade to a `raw` head, never take the
    endpoint down with it."""
    turn = _by_id(db.get_turns(three_turns))[2]
    assert turn["scout"] == {"raw": "{not json at all"}


def test_malformed_reflect_and_eval_also_degrade(three_turns):
    _msg(three_turns, "reflect", T1_START + 130 * SECOND, "not json either")
    _msg(three_turns, "eval", T1_START + 131 * SECOND, "]][[")
    turn = _by_id(db.get_turns(three_turns))[1]
    assert turn["reflect"][-1] == {"attempt": 3, "raw": "not json either"}
    assert turn["eval"][-1]["gates"] == []
    assert turn["eval"][-1]["raw"] == "]][["


# ---------------------------------------------------------------------------
# Tokens and model
# ---------------------------------------------------------------------------


def test_tokens_are_summed_per_turn(three_turns):
    turns = _by_id(db.get_turns(three_turns))
    assert turns[1]["tokens"] == {
        "prompt": 3000,
        "completion": 50,
        "total": 3050,
        "calls": 2,
        "cost_estimate": None,
        "models": ["test-model"],
    }
    assert turns[2]["tokens"]["calls"] == 2
    assert turns[2]["tokens"]["total"] == 4570


def test_cost_is_null_when_nothing_priced_itself(three_turns):
    """An unpriced local model must report no cost, not a cost of zero."""
    turns = _by_id(db.get_turns(three_turns))
    assert turns[1]["tokens"]["cost_estimate"] is None
    assert turns[3]["tokens"]["cost_estimate"] is None
    # Turn 2's rows do carry a price, so it sums.
    assert turns[2]["tokens"]["cost_estimate"] == pytest.approx(0.30)


def test_model_is_the_one_the_turn_mostly_ran_on(three_turns):
    turns = _by_id(db.get_turns(three_turns))
    assert turns[1]["model"] == "test-model"


def test_model_is_null_when_no_assistant_row_recorded_one(three_turns):
    sid = db.create_session(title="No model stamp")
    _log(sid, 1, "idle_ready", "scouting", "prompt-arrived", T1_START)
    _log(sid, 1, "scouting", "processing", "scout-done", T1_START + SECOND)
    _msg(sid, "assistant", T1_START + 2 * SECOND, "answer")
    _log(sid, 1, "finalizing", "idle_ready", "turn-complete", T1_START + 3 * SECOND)
    turn = db.get_turns(sid)["turns"][0]
    assert turn["model"] is None
    assert turn["tokens"] == {
        "prompt": 0,
        "completion": 0,
        "total": 0,
        "calls": 0,
        "cost_estimate": None,
        "models": [],
    }


# ---------------------------------------------------------------------------
# Turn chaining
# ---------------------------------------------------------------------------


def test_answer_received_chains_to_the_turn_that_asked(three_turns):
    """ask_user ends a turn; the answer opens a new one that names its
    parent."""
    sid = db.create_session(title="Question")
    _log(sid, 1, "idle_ready", "scouting", "prompt-arrived", T1_START)
    _log(sid, 1, "scouting", "processing", "scout-done", T1_START + SECOND)
    _log(sid, 1, "processing", "awaiting_user", "ask-user", T1_START + 2 * SECOND)
    _log(sid, 2, "awaiting_user", "scouting", "answer-received", T1_START + 60 * SECOND, parent_turn_id=1)
    _log(sid, 2, "scouting", "processing", "scout-done", T1_START + 61 * SECOND)
    _log(sid, 2, "processing", "finalizing", "loop-complete", T1_START + 70 * SECOND, termination_reason="complete")
    _log(sid, 2, "finalizing", "idle_ready", "turn-complete", T1_START + 71 * SECOND)

    turns = _by_id(db.get_turns(sid))
    assert turns[2]["parent_turn_id"] == 1
    assert turns[1]["parent_turn_id"] is None
    # A turn parked in awaiting_user is finished, not running: the answer
    # arrives as turn 2.
    assert turns[1]["running"] is False
    assert turns[1]["ended_at"] is not None
    assert turns[1]["elapsed_ms"] == 2 * SECOND


def test_termination_reason_is_the_last_one_recorded(three_turns):
    turns = _by_id(db.get_turns(three_turns))
    assert turns[1]["termination_reason"] == "complete"
    assert turns[3]["termination_reason"] is None


# ---------------------------------------------------------------------------
# The HTTP route
# ---------------------------------------------------------------------------


def _app():
    from api.routers import sessions

    app = FastAPI()
    app.include_router(sessions.router)
    return app


async def test_endpoint_returns_the_page(three_turns):
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        resp = await client.get(f"/api/sessions/{three_turns}/turns?limit=2")
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == three_turns
    assert body["count"] == 2
    assert body["has_more"] is True
    assert [t["turn_id"] for t in body["turns"]] == [3, 2]


async def test_endpoint_pages_backward(three_turns):
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        resp = await client.get(f"/api/sessions/{three_turns}/turns?before_turn=2&limit=20")
    body = resp.json()
    assert [t["turn_id"] for t in body["turns"]] == [1]
    assert body["has_more"] is False


async def test_endpoint_clamps_the_limit(three_turns):
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        resp = await client.get(f"/api/sessions/{three_turns}/turns?limit=999")
    assert resp.status_code == 200
    assert resp.json()["count"] == 3


async def test_endpoint_404s_on_an_unknown_session():
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        resp = await client.get("/api/sessions/nope/turns")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


async def test_state_log_endpoint_is_untouched(three_turns):
    """The new view is additive: the log the timeline pages today still
    answers exactly as it did."""
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        resp = await client.get(f"/api/sessions/{three_turns}/state-log")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == len(body["entries"]) > 0
    assert "from_state" in body["entries"][0]
