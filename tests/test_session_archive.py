"""Archive, not delete — the session state between "in my sidebar" and "gone".

Until v34 the only way to clear a chat off the list was to delete it, so a
sidebar with a year in it forced a choice between clutter and losing the
transcript. Archiving is the third answer: the session leaves the list and
its space group, keeps every message, stays searchable, and opens read-only
with a Restore control. Delete stays a separate, explicit act.

Pinned here: the column arrives on a v33 database; the list excludes
archived rows by default and returns exactly them on request; PATCH
round-trips both ways WITHOUT bumping updated_at (recency ordering is what
the time buckets and the idle horizon are computed from); an archived
session refuses messages with a reason that says how to undo it; search
still finds it and says it is archived; and the two sweeps see the whole
table rather than a window over the newest rows — the bug class that hid
the oldest sessions from every pruner twice already.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from db import models as db
from db.database import connect_sessions

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _backdate(sid: str, days: float) -> None:
    with connect_sessions() as conn:
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (_ago(days), sid))


def _pin(sid: str) -> None:
    with connect_sessions() as conn:
        conn.execute("UPDATE sessions SET pinned = 1 WHERE id = ?", (sid,))


def _archive_at(sid: str, days_ago: float) -> None:
    with connect_sessions() as conn:
        conn.execute("UPDATE sessions SET archived_at = ? WHERE id = ?", (_ago(days_ago), sid))


def _updated_at(sid: str) -> str:
    return db.get_session(sid)["updated_at"]


def _crowd(n: int = 1005) -> None:
    """More than 1,000 fresher sessions, so a windowed scan cannot see past
    them — the shape that hid the oldest rows from the pruners (2026-08-21)
    and then from the purge (2026-09-02)."""
    with connect_sessions() as conn:
        now = datetime.now(timezone.utc).isoformat()
        conn.executemany(
            """INSERT INTO sessions (id, title, system_prompt, session_type,
               state, created_at, updated_at) VALUES (?, ?, '', 'normal', 'idle', ?, ?)""",
            [(f"filler{i:08d}", f"filler {i}", now, now) for i in range(n)],
        )


def _client(*routers) -> AsyncClient:
    app = FastAPI()
    for r in routers:
        app.include_router(r)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _sessions_client() -> AsyncClient:
    from api.routers import sessions

    return _client(sessions.router)


# ---------------------------------------------------------------------------
# The column
# ---------------------------------------------------------------------------


def test_migration_v34_adds_archived_at_to_a_v33_database(tmp_path, monkeypatch):
    """A box already carrying spaces gets the column, not a fresh schema."""
    from db import database

    monkeypatch.setattr("config.settings.db_path", str(tmp_path / "v33.db"))
    monkeypatch.setattr(database, "MIGRATIONS", [m for m in database.MIGRATIONS if m[0] <= 33])
    database.init_sessions_db()
    with connect_sessions() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        assert "space_id" in cols and "archived_at" not in cols
        assert int(conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]) == 33

    monkeypatch.undo()
    monkeypatch.setattr("config.settings.db_path", str(tmp_path / "v33.db"))
    database.init_sessions_db()

    with connect_sessions() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        assert "archived_at" in cols
        assert int(conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]) >= 34
        # The queries that filter on it all do so on the archived side.
        idx = {r["name"] for r in conn.execute("PRAGMA index_list(sessions)")}
        assert "idx_sessions_archived" in idx


def test_a_new_session_is_not_archived():
    sid = db.create_session(title="fresh")
    assert db.get_session(sid)["archived_at"] is None


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


async def test_list_excludes_archived_by_default_and_archived_1_returns_only_them():
    live = db.create_session(title="still talking")
    gone = db.create_session(title="finished months ago")
    db.set_session_meta(gone, archived=True)

    async with _sessions_client() as c:
        default = (await c.get("/api/sessions")).json()
        archived = (await c.get("/api/sessions?archived=1")).json()

    assert [s["id"] for s in default["items"]] == [live]
    assert default["total"] == 1, "total counts the population being listed"
    assert [s["id"] for s in archived["items"]] == [gone]
    assert archived["total"] == 1 and archived["archived"] is True


async def test_archived_count_rides_on_both_answers():
    db.create_session(title="live")
    for i in range(3):
        db.set_session_meta(db.create_session(title=f"old {i}"), archived=True)

    async with _sessions_client() as c:
        default = (await c.get("/api/sessions")).json()
        archived = (await c.get("/api/sessions?archived=1")).json()

    # The sidebar needs the number for "Archived (N)" without a second call.
    assert default["archived_count"] == 3 and default["total"] == 1
    assert archived["archived_count"] == 3 and archived["total"] == 3


def test_an_archived_space_session_leaves_its_space_group():
    """The never-roll-off union is exactly what archiving has to defeat."""
    space = db.create_space("Research", "#8ab4f8", "research")["id"]
    sid = db.create_session(title="space chat", space_id=space)
    _backdate(sid, 400)
    _crowd(60)

    assert sid in {r["id"] for r in db.list_sessions_enriched(limit=10)}
    db.set_session_meta(sid, archived=True)
    assert sid not in {r["id"] for r in db.list_sessions_enriched(limit=10)}
    assert sid in {r["id"] for r in db.list_sessions_enriched(limit=10, archived=True)}
    assert db.list_spaces()[0]["session_count"] == 0


def test_count_sessions_counts_the_population_it_is_asked_for():
    live = [db.create_session(title=f"live {i}") for i in range(2)]
    gone = [db.create_session(title=f"gone {i}") for i in range(3)]
    for sid in gone:
        db.set_session_meta(sid, archived=True)

    assert db.count_sessions() == len(live)
    assert db.count_sessions(archived=True) == len(gone)
    assert db.count_sessions(archived=None) == len(live) + len(gone)


# ---------------------------------------------------------------------------
# PATCH archived
# ---------------------------------------------------------------------------


async def test_patch_archived_round_trips_without_touching_updated_at():
    sid = db.create_session(title="a chat from March")
    _backdate(sid, 120)
    before = _updated_at(sid)

    async with _sessions_client() as c:
        on = (await c.patch(f"/api/sessions/{sid}", json={"archived": True})).json()
        assert on["archived"] is True
        assert db.get_session(sid)["archived_at"] is not None
        assert _updated_at(sid) == before, "archiving must not reshuffle the sidebar"

        off = (await c.patch(f"/api/sessions/{sid}", json={"archived": False})).json()

    assert off["archived"] is False
    assert db.get_session(sid)["archived_at"] is None
    assert _updated_at(sid) == before, "a restored session goes back where it was"


async def test_patch_archived_on_a_missing_session_is_a_404():
    async with _sessions_client() as c:
        resp = await c.patch("/api/sessions/nope", json={"archived": True})
    assert resp.status_code == 404


async def test_session_detail_carries_archived_at_and_the_read_only_verdict():
    from sessions.policy import ARCHIVED_REASON

    sid = db.create_session(title="filed away")
    db.set_session_meta(sid, archived=True)

    async with _sessions_client() as c:
        detail = (await c.get(f"/api/sessions/{sid}")).json()

    assert detail["archived_at"]
    assert detail["read_only"] is True
    assert detail["read_only_reason"] == ARCHIVED_REASON
    assert "estore" in detail["read_only_reason"], "the reason has to say how to undo it"


def test_read_only_reason_is_archived_before_it_is_a_type():
    """Archiving is not a session type — any type can be archived."""
    from sessions.policy import ARCHIVED_REASON, read_only_reason

    assert read_only_reason({"session_type": "normal", "archived_at": None}) is None
    assert read_only_reason({"session_type": "normal", "archived_at": _ago(1)}) == ARCHIVED_REASON
    assert read_only_reason({"session_type": "snooze", "archived_at": _ago(1)}) == ARCHIVED_REASON


async def test_an_archived_session_rejects_a_message_with_the_reason():
    from api.routers import chat as chat_router
    from sessions.policy import ARCHIVED_REASON

    sid = db.create_session(title="closed thread")
    db.set_session_meta(sid, archived=True)

    async with _client(chat_router.router) as c:
        resp = await c.post("/api/chat", json={"session_id": sid, "message": "one more thing"})
        assert resp.status_code == 400 and resp.json()["detail"] == ARCHIVED_REASON
        inject = await c.post("/api/chat/inject", json={"session_id": sid, "message": "hi"})
        assert inject.status_code == 400

    db.set_session_meta(sid, archived=False)
    assert db.get_session(sid)["archived_at"] is None


# ---------------------------------------------------------------------------
# Search — the surface archiving deliberately does NOT remove a session from
# ---------------------------------------------------------------------------


async def test_search_still_finds_an_archived_session_and_says_so():
    live = db.create_session(title="live thread")
    gone = db.create_session(title="archived thread")
    for sid in (live, gone):
        db.add_message(sid, "user", "the reconciliation ledger drifted past tolerance")
    db.set_session_meta(gone, archived=True)

    async with _sessions_client() as c:
        results = (await c.get("/api/sessions/search?q=reconciliation")).json()["results"]

    by_id = {r["session_id"]: r for r in results}
    assert set(by_id) == {live, gone}, "archiving hides a chat from the list, not from search"
    assert by_id[gone]["archived"] is True
    assert by_id[live]["archived"] is False


# ---------------------------------------------------------------------------
# archive_idle_sessions
# ---------------------------------------------------------------------------


def test_archive_idle_selects_exactly_idle_normal_unpinned_from_behind_1000_rows():
    from core.retention import archive_idle_sessions

    stale = db.create_session(title="idle since spring")
    fresh = db.create_session(title="yesterday")
    pinned = db.create_session(title="pinned and idle")
    typed = {t: db.create_session(title=f"{t} run", session_type=t) for t in ("canary", "worker", "cron", "rlm")}
    for sid in (stale, pinned, *typed.values()):
        _backdate(sid, 90)
    _backdate(fresh, 1)
    _pin(pinned)
    _crowd()  # the fillers are fresh; a windowed scan would see only them

    result = archive_idle_sessions(30)

    assert result["count"] == 1 and result["ids"] == [stale]
    assert db.get_session(stale)["archived_at"] is not None
    assert db.get_session(pinned)["archived_at"] is None, "pinning is the user saying keep this"
    assert db.get_session(fresh)["archived_at"] is None
    assert all(db.get_session(sid)["archived_at"] is None for sid in typed.values())


def test_archive_idle_includes_space_sessions():
    """v33 spares space sessions from every DELETE sweep because losing a
    transcript is irreversible. Archiving loses nothing, so they are in."""
    from core.retention import archive_idle_sessions

    space = db.create_space("YouTube", "#e5534b", "youtube")["id"]
    sid = db.create_session(title="an old video idea", space_id=space)
    _backdate(sid, 60)

    assert archive_idle_sessions(30)["ids"] == [sid]
    assert db.get_session(sid)["archived_at"] is not None
    assert db.get_session(sid)["space_id"] == space, "it keeps its membership, it just leaves the group"


def test_archive_idle_can_be_scoped_to_one_space():
    from core.retention import archive_idle_sessions

    space = db.create_space("YouTube", "#e5534b", "youtube")["id"]
    inside = db.create_session(title="in the space", space_id=space)
    outside = db.create_session(title="not in it")
    for sid in (inside, outside):
        _backdate(sid, 60)

    result = archive_idle_sessions(30, space_id=space)

    assert result["ids"] == [inside]
    assert db.get_session(outside)["archived_at"] is None


def test_archive_idle_dry_run_changes_nothing_and_reports_what_the_real_run_does():
    from core.retention import archive_idle_sessions

    ids = [db.create_session(title=f"chat {i}") for i in range(12)]
    for i, sid in enumerate(ids):
        _backdate(sid, 40 + i)

    dry = archive_idle_sessions(30, dry_run=True)
    assert all(db.get_session(sid)["archived_at"] is None for sid in ids), "a dry run writes nothing"

    real = archive_idle_sessions(30)

    assert dry["dry_run"] is True and real["dry_run"] is False
    assert dry["count"] == real["count"] == 12
    assert dry["ids"] == real["ids"]
    assert len(dry["sample"]) == 10, "the sample is the first ten"
    assert set(dry["sample"][0]) == {"id", "title", "updated_at", "space_id"}


def test_archive_idle_never_re_archives_and_is_off_at_zero(monkeypatch):
    from core.retention import archive_idle_sessions

    sid = db.create_session(title="already filed")
    _backdate(sid, 90)
    archive_idle_sessions(30)
    stamp = db.get_session(sid)["archived_at"]

    assert archive_idle_sessions(30)["count"] == 0
    assert db.get_session(sid)["archived_at"] == stamp

    other = db.create_session(title="idle too")
    _backdate(other, 90)
    monkeypatch.setattr("config.settings.session_archive_idle_days", 0)
    assert archive_idle_sessions()["count"] == 0
    assert db.get_session(other)["archived_at"] is None


def test_archive_idle_reads_the_setting_when_no_days_are_given(monkeypatch):
    from core.retention import archive_idle_sessions

    monkeypatch.setattr("config.settings.session_archive_idle_days", 30)
    sid = db.create_session(title="idle since spring")
    _backdate(sid, 45)

    assert archive_idle_sessions()["ids"] == [sid]


async def test_the_archive_idle_endpoint_promises_what_it_then_does():
    space = db.create_space("YouTube", "#e5534b", "youtube")["id"]
    inside = db.create_session(title="old video idea", space_id=space)
    outside = db.create_session(title="unrelated chat")
    for sid in (inside, outside):
        _backdate(sid, 60)

    async with _sessions_client() as c:
        dry = (await c.post("/api/sessions/archive-idle", json={"days": 30, "space_id": space, "dry_run": True})).json()
        assert db.get_session(inside)["archived_at"] is None
        real = (await c.post("/api/sessions/archive-idle", json={"days": 30, "space_id": space})).json()
        missing = await c.post("/api/sessions/archive-idle", json={"space_id": "nope"})
        bad = await c.post("/api/sessions/archive-idle", json={"days": -1})

    assert dry["count"] == real["count"] == 1 and dry["ids"] == real["ids"] == [inside]
    assert db.get_session(inside)["archived_at"] is not None
    assert db.get_session(outside)["archived_at"] is None
    assert missing.status_code == 404
    assert bad.status_code == 400


# ---------------------------------------------------------------------------
# prune_archived_sessions
# ---------------------------------------------------------------------------


def test_prune_archived_deletes_only_rows_past_the_archive_horizon():
    from core.retention import prune_archived_sessions

    old = db.create_session(title="archived a year ago")
    recent = db.create_session(title="archived last week")
    live = db.create_session(title="never archived")
    _backdate(live, 400)  # age alone must not reach it
    _archive_at(old, 400)
    _archive_at(recent, 7)
    _crowd()

    result = prune_archived_sessions(90)

    assert result["count"] == 1 and result["ids"] == [old]
    assert db.get_session(old) is None
    assert db.get_session(recent) is not None
    assert db.get_session(live) is not None


def test_prune_archived_is_a_no_op_at_zero(monkeypatch):
    from core.retention import prune_archived_sessions

    sid = db.create_session(title="archived long ago")
    _archive_at(sid, 3650)

    monkeypatch.setattr("config.settings.session_delete_archived_days", 0)
    assert prune_archived_sessions() == {"count": 0, "ids": [], "sample": [], "days": 0, "dry_run": False}
    assert prune_archived_sessions(0)["count"] == 0
    assert db.get_session(sid) is not None, "0 means never — the archive is the point"


def test_prune_archived_dry_run_deletes_nothing():
    from core.retention import prune_archived_sessions

    ids = [db.create_session(title=f"filed {i}") for i in range(3)]
    for sid in ids:
        _archive_at(sid, 400)

    dry = prune_archived_sessions(90, dry_run=True)

    assert dry["count"] == 3 and sorted(dry["ids"]) == sorted(ids)
    assert all(db.get_session(sid) is not None for sid in ids)


def test_prune_archived_reaches_every_session_type():
    """Once a row carries archived_at it is in the archive, and the archive
    has one horizon rather than six."""
    from core.retention import prune_archived_sessions

    typed = [db.create_session(title=f"{t}", session_type=t) for t in ("normal", "cron", "worker", "snooze")]
    for sid in typed:
        _archive_at(sid, 200)

    assert prune_archived_sessions(90)["count"] == 4
    assert all(db.get_session(sid) is None for sid in typed)


# ---------------------------------------------------------------------------
# Snooze stops working on a session the user has filed away
# ---------------------------------------------------------------------------


def _make_distillable(title: str) -> str:
    sid = db.create_session(title=title)
    for i in range(3):
        db.add_message(sid, "user", f"question {i} " + "x" * 120)
        db.add_message(sid, "assistant", f"answer {i} " + "y" * 120)
    _backdate(sid, 1)
    return sid


def test_get_unreviewed_sessions_ignores_archived():
    live = _make_distillable("still open")
    filed = _make_distillable("filed away")
    db.set_session_meta(filed, archived=True)

    ids = {s["id"] for s in db.get_unreviewed_sessions(min_age_minutes=1, limit=10)}

    assert live in ids
    assert filed not in ids, "an archived chat must not spend a distillation call"


def test_get_unrefined_sessions_ignores_archived():
    live = _make_distillable("still open")
    filed = _make_distillable("filed away")
    db.set_session_meta(filed, archived=True)

    ids = {s["id"] for s in db.get_unrefined_sessions(min_idle_minutes=1, limit=10)}

    assert live in ids and filed not in ids


# ---------------------------------------------------------------------------
# The knobs
# ---------------------------------------------------------------------------


def test_the_two_knobs_have_bounds_that_allow_zero():
    from api.routers.health import _SETTING_BOUNDS

    assert _SETTING_BOUNDS["session_archive_idle_days"][0] == 0
    assert _SETTING_BOUNDS["session_delete_archived_days"][0] == 0


def test_the_archived_index_is_partial_so_it_costs_the_live_list_nothing():
    with connect_sessions() as conn:
        try:
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_sessions_archived'"
            ).fetchone()
        except sqlite3.OperationalError:  # pragma: no cover - schema always present in tests
            sql = None
    assert sql and "WHERE archived_at IS NOT NULL" in sql[0]
