"""Leaving whole session types out of the list, in SQL, before the LIMIT.

The sidebar's legend has always hidden types in the browser, which cannot
help with the problem it looks like it solves: the row was already on the
page it was being hidden from. On the owner's box the 500 most recently
updated sessions are 277 canary self-checks, 47 workers and 33 cron runs, so
106 of 310 chats made page one and the rest sat behind "Load older
sessions" — with "Self-check" switched off in the legend the whole time.

Pinned here: the filter runs before the LIMIT so the page refills with what
is left; `total` and `has_more` count the same narrowed population, so the
paging control agrees with the list above it; an excluded type is absent
from every page rather than only the first; the never-roll-off space union
takes the same clause instead of walking the rows back in behind it; the
archived listing honours it too; and `type_counts` deliberately does NOT,
because the legend has to keep naming what it is hiding.
"""

from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from db import models as db
from db.database import connect_sessions

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mk(title: str, session_type: str = "normal", space_id: str | None = None) -> str:
    return db.create_session(title=title, session_type=session_type, space_id=space_id)


def _backdate(sid: str, days: float) -> None:
    when = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with connect_sessions() as conn:
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (when, sid))


def _untype(sid: str) -> None:
    """A row written before session_type had a default: NULL, not 'normal'."""
    with connect_sessions() as conn:
        conn.execute("UPDATE sessions SET session_type = NULL WHERE id = ?", (sid,))


def _sessions_client() -> AsyncClient:
    from api.routers import sessions

    app = FastAPI()
    app.include_router(sessions.router)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ---------------------------------------------------------------------------
# The clause
# ---------------------------------------------------------------------------


def test_the_filtered_listing_leaves_the_named_types_out():
    chats = [_mk(f"chat {i}") for i in range(3)]
    _mk("canary 1", "canary")
    _mk("canary 2", "canary")
    _mk("nightly", "cron")

    kept = {r["id"] for r in db.list_sessions_enriched(limit=50, exclude_types=["canary", "cron"])}
    assert kept == set(chats)
    assert len(db.list_sessions_enriched(limit=50)) == 6


def test_excluding_normal_also_excludes_rows_written_before_the_column_had_a_default():
    old = _mk("from before the default")
    _untype(old)
    worker = _mk("a worker", "worker")

    kept = [r["id"] for r in db.list_sessions_enriched(limit=50, exclude_types=["normal"])]
    assert kept == [worker], "an untyped session IS an ordinary chat"


def test_unknown_type_names_are_ignored_rather_than_rejected():
    chat = _mk("chat")
    canary = _mk("canary", "canary")

    # A client that has learned a type this build never heard of must degrade
    # to showing it, not error on every list request.
    kept = {r["id"] for r in db.list_sessions_enriched(limit=50, exclude_types=["hologram", "canary"])}
    assert kept == {chat}
    assert {r["id"] for r in db.list_sessions_enriched(limit=50, exclude_types=["hologram"])} == {chat, canary}


def test_the_filter_runs_before_the_limit():
    """The whole point: the page refills with what is left.

    Five chats, then twenty canaries on top of them — the shape of the box,
    in miniature. A five-row page holds nothing but canaries, and filtering
    the page after it was cut would leave the user with an empty list rather
    than their five chats.
    """
    chats = [_mk(f"chat {i}") for i in range(5)]
    for i in range(20):
        _mk(f"canary {i}", "canary")

    unfiltered = db.list_sessions_enriched(limit=5)
    assert not {r["id"] for r in unfiltered} & set(chats), "the newest five are all canaries"

    filtered = db.list_sessions_enriched(limit=5, exclude_types=["canary"])
    assert {r["id"] for r in filtered} == set(chats)


def test_the_space_union_takes_the_same_clause():
    """Space sessions are unioned back in past the recency cut — an excluded
    type must not walk in through that door."""
    space = db.create_space("Research", "#8ab4f8", "research")["id"]
    chat = _mk("space chat", space_id=space)
    cron = _mk("space cron", "cron", space_id=space)
    _backdate(chat, 400)
    _backdate(cron, 400)
    for i in range(30):
        _mk(f"filler {i}")

    rows = {r["id"] for r in db.list_sessions_enriched(limit=5)}
    assert chat in rows and cron in rows, "both are unioned back in"

    rows = {r["id"] for r in db.list_sessions_enriched(limit=5, exclude_types=["cron"])}
    assert chat in rows and cron not in rows


def test_count_sessions_counts_the_narrowed_population():
    for i in range(4):
        _mk(f"chat {i}")
    for i in range(6):
        _mk(f"canary {i}", "canary")
    gone = _mk("archived canary", "canary")
    db.set_session_meta(gone, archived=True)

    assert db.count_sessions() == 10
    assert db.count_sessions(exclude_types=["canary"]) == 4
    assert db.count_sessions(archived=True, exclude_types=["canary"]) == 0
    assert db.count_sessions(archived=True) == 1
    assert db.count_sessions(archived=None, exclude_types=["canary"]) == 4


def test_count_sessions_by_type_partitions_the_live_population():
    for i in range(3):
        _mk(f"chat {i}")
    _mk("w", "worker")
    _mk("c", "canary")
    _mk("c2", "canary")
    _untype(_mk("untyped"))
    db.set_session_meta(_mk("filed away", "canary"), archived=True)

    counts = db.count_sessions_by_type()
    assert counts == {"normal": 4, "worker": 1, "cron": 0, "rlm": 0, "snooze": 0, "canary": 2}
    assert sum(counts.values()) == db.count_sessions(), "the live list, partitioned"


def test_an_unknown_type_is_reported_under_its_own_name():
    _mk("chat")
    with connect_sessions() as conn:
        conn.execute("UPDATE sessions SET session_type = 'hologram' WHERE title = 'chat'")
    counts = db.count_sessions_by_type()
    assert counts["hologram"] == 1 and counts["normal"] == 0


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------


async def test_exclude_types_is_a_comma_list_and_the_counts_follow_it():
    chats = [_mk(f"chat {i}") for i in range(3)]
    for i in range(7):
        _mk(f"canary {i}", "canary")
    _mk("nightly", "cron")

    async with _sessions_client() as c:
        all_of_it = (await c.get("/api/sessions?limit=100")).json()
        filtered = (await c.get("/api/sessions?limit=100&exclude_types=canary,cron")).json()

    assert all_of_it["total"] == 11
    assert {s["id"] for s in filtered["items"]} == set(chats)
    assert filtered["total"] == 3, "total counts the population being listed"
    assert filtered["has_more"] is False
    assert filtered["excluded_types"] == ["canary", "cron"]


async def test_unknown_and_blank_names_are_dropped_from_the_echo():
    _mk("chat")
    async with _sessions_client() as c:
        r = (await c.get("/api/sessions?exclude_types=,canary,,hologram,")).json()
    assert r["excluded_types"] == ["canary"]
    assert r["count"] == 1


async def test_has_more_and_the_pages_behind_it_honour_the_exclusion():
    """An excluded type is absent from every page, not only the first."""
    chats = [_mk(f"chat {i}") for i in range(12)]
    for i in range(40):
        _mk(f"canary {i}", "canary")

    seen: list[str] = []
    async with _sessions_client() as c:
        offset = 0
        while True:
            page = (await c.get(f"/api/sessions?limit=5&offset={offset}&exclude_types=canary")).json()
            assert page["total"] == 12
            assert all(s["session_type"] != "canary" for s in page["items"])
            seen += [s["id"] for s in page["items"]]
            if not page["has_more"]:
                break
            offset += 5
            assert offset < 100, "paging did not terminate"

    assert set(seen) == set(chats)
    assert len(seen) == 12, "no row is served twice"


async def test_the_archived_listing_honours_it_too():
    live = _mk("live chat")
    filed_chat = _mk("filed chat")
    filed_canary = _mk("filed canary", "canary")
    for sid in (filed_chat, filed_canary):
        db.set_session_meta(sid, archived=True)

    async with _sessions_client() as c:
        page = (await c.get("/api/sessions?archived=1&exclude_types=canary")).json()

    assert [s["id"] for s in page["items"]] == [filed_chat]
    assert page["total"] == 1
    # The legend's "Archived (N)" still names the whole archive: a count that
    # moved with the filter would tell the user rows had left the archive.
    assert page["archived_count"] == 2
    assert live not in {s["id"] for s in page["items"]}


async def test_type_counts_covers_the_whole_unfiltered_live_population():
    for i in range(2):
        _mk(f"chat {i}")
    for i in range(5):
        _mk(f"canary {i}", "canary")
    db.set_session_meta(_mk("filed", "canary"), archived=True)

    async with _sessions_client() as c:
        filtered = (await c.get("/api/sessions?exclude_types=canary")).json()

    # The legend has to keep naming what it is hiding — otherwise switching a
    # type off erases the control that switches it back on.
    assert filtered["type_counts"]["canary"] == 5
    assert filtered["type_counts"]["normal"] == 2
    assert filtered["type_counts"]["worker"] == 0
    assert filtered["total"] == 2
    assert not any(s["session_type"] == "canary" for s in filtered["items"])


async def test_an_excluded_child_type_leaves_its_parent_standing():
    """Workers nest under their parent. Excluding the type removes the rows,
    not the sessions they hang off."""
    parent = _mk("fan-out parent")
    db.create_session(title="worker a", session_type="worker", parent_session_id=parent)
    db.create_session(title="worker b", session_type="worker", parent_session_id=parent)

    async with _sessions_client() as c:
        page = (await c.get("/api/sessions?exclude_types=worker")).json()

    assert [s["id"] for s in page["items"]] == [parent]
    assert page["type_counts"]["worker"] == 2
