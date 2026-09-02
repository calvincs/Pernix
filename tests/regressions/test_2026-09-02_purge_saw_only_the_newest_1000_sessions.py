"""Regression — 2026-09-02, box (1,032 sessions, 32 of them invisible).

POST /api/sessions/purge read `list_sessions(1000)` — the 1,000 most
recently updated rows — and filtered those by cutoff. So the 32 OLDEST
sessions, the best candidates a bulk purge has, were the ones it could not
see, and the blind spot grew by one per new session. Same bug class as the
2026-08-21 pruners; the fix is the same shape, one query over the whole
table.

It also deleted by age alone: canary, worker, cron, rlm and snooze sessions
each carry their own horizon in core/retention.py and were swept out from
under it, and `pinned` — the user saying "keep this" — did nothing at all.

Pinned here: the scan reaches past any window, the three exclusion rules
hold and are reported under `skipped`, keep_min counts candidates rather
than rows, and a dry run reports exactly what the real run then does.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from db import models as db
from db.database import connect_sessions


def _ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _backdate(sid: str, days: float) -> None:
    with connect_sessions() as conn:
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (_ago(days), sid))


def _pin(sid: str) -> None:
    with connect_sessions() as conn:
        conn.execute("UPDATE sessions SET pinned = 1 WHERE id = ?", (sid,))


def _crowd(n: int = 1005) -> None:
    """More than 1,000 fresher sessions, so a windowed scan cannot see past them."""
    with connect_sessions() as conn:
        now = datetime.now(timezone.utc).isoformat()
        conn.executemany(
            """INSERT INTO sessions (id, title, system_prompt, session_type,
               state, created_at, updated_at) VALUES (?, ?, '', 'normal', 'idle', ?, ?)""",
            [(f"filler{i:08d}", f"filler {i}", now, now) for i in range(n)],
        )


def _client() -> AsyncClient:
    from api.routers import sessions

    app = FastAPI()
    app.include_router(sessions.router)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _purge(**body) -> dict:
    async with _client() as c:
        resp = await c.post("/api/sessions/purge", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# (a) the window
# ---------------------------------------------------------------------------


async def test_oldest_session_is_a_candidate_from_behind_1000_newer_ones():
    oldest = db.create_session(title="the oldest chat")
    _backdate(oldest, 400)
    _crowd()

    result = await _purge(keep_days=7, keep_min=0, dry_run=True)

    ids = {s["id"] for s in result["sample"]}
    assert result["candidates"] == 1 and result["would_delete"] == 1
    assert oldest in ids, "the oldest session is exactly the one a window hides"


async def test_the_real_run_deletes_the_session_behind_the_window():
    oldest = db.create_session(title="the oldest chat")
    _backdate(oldest, 400)
    _crowd()

    result = await _purge(keep_days=7, keep_min=0)

    assert result["purged"] == 1
    assert db.get_session(oldest) is None


# ---------------------------------------------------------------------------
# (b), (c), (d) — the three exclusion rules
# ---------------------------------------------------------------------------


async def test_pinned_sessions_are_kept_and_counted():
    pinned = db.create_session(title="pinned and old")
    plain = db.create_session(title="plain and old")
    for sid in (pinned, plain):
        _backdate(sid, 30)
    _pin(pinned)

    result = await _purge(keep_days=7, keep_min=0)

    assert result["candidates"] == 1 and result["purged"] == 1
    assert result["skipped"]["pinned"] == 1
    assert db.get_session(pinned) is not None
    assert db.get_session(plain) is None


async def test_typed_sessions_are_kept_and_counted_under_other_types():
    """Each of these has its own retention horizon in core/retention.py."""
    typed = {
        t: db.create_session(title=f"{t} run", session_type=t) for t in ("canary", "worker", "cron", "rlm", "snooze")
    }
    plain = db.create_session(title="ordinary chat")
    for sid in (*typed.values(), plain):
        _backdate(sid, 30)

    result = await _purge(keep_days=7, keep_min=0)

    assert result["candidates"] == 1 and result["purged"] == 1
    assert result["skipped"]["other_types"] == 5
    assert all(db.get_session(sid) is not None for sid in typed.values())
    assert db.get_session(plain) is None


async def test_space_sessions_are_kept_and_counted():
    space = db.create_space("Research", "#8ab4f8", "research")["id"]
    in_space = db.create_session(title="space chat", space_id=space)
    plain = db.create_session(title="ordinary chat")
    for sid in (in_space, plain):
        _backdate(sid, 30)

    result = await _purge(keep_days=7, keep_min=0)

    assert result["candidates"] == 1 and result["purged"] == 1
    assert result["skipped"]["in_space"] == 1
    assert db.get_session(in_space) is not None
    assert db.get_session(plain) is None


async def test_a_session_is_counted_under_the_first_rule_that_spares_it():
    """One session can break every rule; the buckets still partition."""
    space = db.create_space("Research", "#8ab4f8", "research")["id"]
    sid = db.create_session(title="pinned worker in a space", session_type="worker", space_id=space)
    _pin(sid)
    _backdate(sid, 30)

    found = db.list_purge_candidates(_ago(7))

    assert found["candidates"] == []
    assert found["skipped"] == {"other_types": 1, "pinned": 0, "in_space": 0}


# ---------------------------------------------------------------------------
# (e) dry run, (f) keep_min
# ---------------------------------------------------------------------------


async def test_dry_run_deletes_nothing_and_reports_what_the_real_run_does():
    ids = [db.create_session(title=f"chat {i}") for i in range(6)]
    for i, sid in enumerate(ids):
        _backdate(sid, 30 + i)

    async with _client() as c:
        dry = (await c.post("/api/sessions/purge", json={"keep_days": 7, "keep_min": 2, "dry_run": True})).json()
        assert all(db.get_session(sid) is not None for sid in ids), "a dry run deletes nothing"
        real = (await c.post("/api/sessions/purge", json={"keep_days": 7, "keep_min": 2})).json()

    assert dry["dry_run"] is True and dry["purged"] == 0
    assert real["dry_run"] is False
    assert dry["candidates"] == real["candidates"] == 6
    assert dry["would_delete"] == real["would_delete"] == real["purged"] == 4
    assert [s["id"] for s in dry["sample"]] == [s["id"] for s in real["sample"]]


async def test_keep_min_keeps_the_newest_candidates():
    ids = [db.create_session(title=f"chat {i}") for i in range(6)]
    for i, sid in enumerate(ids):
        _backdate(sid, 30 + i)  # ids[0] newest, ids[5] oldest
    pinned = db.create_session(title="pinned")
    _pin(pinned)
    _backdate(pinned, 1)  # inside the cutoff — not a candidate at all

    result = await _purge(keep_days=7, keep_min=2)

    assert result["candidates"] == 6 and result["purged"] == 4
    assert all(db.get_session(sid) is not None for sid in ids[:2])
    assert all(db.get_session(sid) is None for sid in ids[2:])
    assert db.get_session(pinned) is not None


async def test_sample_is_the_first_ten_of_the_delete_set():
    ids = [db.create_session(title=f"chat {i}") for i in range(15)]
    for i, sid in enumerate(ids):
        _backdate(sid, 30 + i)

    result = await _purge(keep_days=7, keep_min=1, dry_run=True)

    assert result["would_delete"] == 14
    assert len(result["sample"]) == 10
    assert [s["id"] for s in result["sample"]] == ids[1:11]
    assert set(result["sample"][0]) == {"id", "title", "updated_at", "message_count"}


async def test_sample_carries_the_message_count():
    sid = db.create_session(title="chatty")
    db.add_message(sid, "user", "hello")
    db.add_message(sid, "assistant", "hi")
    _backdate(sid, 30)

    result = await _purge(keep_days=7, keep_min=0, dry_run=True)

    assert result["sample"][0]["message_count"] == 2


# ---------------------------------------------------------------------------
# Input validation — the knobs decide what gets deleted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"keep_days": -1},
        {"keep_min": -1},
        {"keep_days": "soon"},
        {"keep_min": None},
        {"keep_days": True},
    ],
)
async def test_bad_knobs_are_rejected_rather_than_coerced(body):
    sid = db.create_session(title="old chat")
    _backdate(sid, 90)

    async with _client() as c:
        resp = await c.post("/api/sessions/purge", json=body)

    assert resp.status_code == 400
    assert db.get_session(sid) is not None


async def test_keep_days_zero_means_everything_already_idle():
    sid = db.create_session(title="from a minute ago")
    _backdate(sid, 0.001)

    result = await _purge(keep_days=0, keep_min=0, dry_run=True)

    assert result["keep_days"] == 0
    assert [s["id"] for s in result["sample"]] == [sid]
