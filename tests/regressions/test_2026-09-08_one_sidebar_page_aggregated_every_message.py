"""One 50-row sidebar page aggregated every message in the database.

`_ENRICHED_SELECT` LEFT JOINed three derived tables into the sessions list:
a GROUP BY over every user/assistant message, a GROUP BY over the whole of
`token_usage`, and a ROW_NUMBER() window partitioned over EVERY user message
there has ever been. SQLite cannot push the outer LIMIT into a shape like
that. `EXPLAIN QUERY PLAN` for `LIMIT 50 OFFSET 0` showed three MATERIALIZE
steps — `MATERIALIZE mc` (SCAN messages), `MATERIALIZE tu` (SCAN
token_usage), `MATERIALIZE (subquery-3)` (SCAN messages + TEMP B-TREE) —
before `SCAN s` was reachable at all.

So the page cost tracked total history rather than page size, and the
sidebar polls it every ten seconds per visible tab. Measured on 1,622
sessions / 116,410 messages, 92,400 of which sit inside ARCHIVED sessions
the page does not display and 1,200 more inside excluded canary sessions:

    limit=25   307.9 ms      limit=50   352.9 ms      limit=500  311.2 ms

— the same work three times, because the limit was never the thing doing
the bounding. Attribution: `mc` 15.2 ms, `tu` 1.2 ms, and the ROW_NUMBER()
window 103.6 ms, the single largest item and paid TWICE per request once
any space existed, because the space union re-ran the whole statement.

Two more things the shape hid. There was no index on the sidebar's own
ordering, so even an id-only page did `SCAN sessions` plus a temp B-tree.
And the space union had no LIMIT of any kind: 900 live space sessions meant
900 extra enriched rows in every poll — 638 KB per refresh in this fixture,
for a sidebar showing 25.

What this does NOT pin is responsiveness. `api/routers/sessions.py` already
dispatches every one of these queries through `asyncio.to_thread`, and the
heartbeat measured 6.5 ms against a 5 ms baseline while the route ran. This
was never an event-loop bug; it was thread-pool occupancy, CPU, and
bandwidth, and it is asserted as such.

Pinned here: candidate ids first and enrichment second, verified against the
query PLAN rather than the statement text; a page whose cost does not follow
history it never shows; one enrichment pass with or without spaces; a
bounded space union that still reports each group's true size; and the same
rows, columns, ordering and paging the sidebar had before.
"""

import asyncio
import time

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from db import models as db
from db.database import connect_sessions

NOW = "2026-09-08T00:00:00+00:00"
HEARTBEAT_S = 0.005


def _client() -> AsyncClient:
    from api.routers import sessions

    app = FastAPI()
    app.include_router(sessions.router)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _bulk(
    prefix: str,
    n: int,
    *,
    messages: int = 0,
    archived: bool = False,
    session_type: str = "normal",
    space_id: str | None = None,
) -> list[str]:
    ids = [f"{prefix}{i:05d}" for i in range(n)]
    with connect_sessions() as conn:
        conn.executemany(
            "INSERT INTO sessions (id, title, system_prompt, session_type, space_id, archived_at,"
            " state, created_at, updated_at) VALUES (?, ?, '', ?, ?, ?, 'idle', ?, ?)",
            [
                (sid, f"{prefix} {i}", session_type, space_id, NOW if archived else None, NOW, NOW)
                for i, sid in enumerate(ids)
            ],
        )
        if messages:
            conn.executemany(
                "INSERT INTO messages (session_id, role, content, char_count, created_at)"
                " VALUES (?, ?, ?, length(?), ?)",
                [
                    (
                        sid,
                        "user" if j % 2 else "assistant",
                        "a line of transcript " * 6,
                        "a line of transcript " * 6,
                        NOW,
                    )
                    for sid in ids
                    for j in range(messages)
                ],
            )
    return ids


# ---------------------------------------------------------------------------
# (a) the plan, not the statement count
# ---------------------------------------------------------------------------


def test_the_page_is_chosen_before_any_aggregate_runs():
    _bulk("live", 5)
    with connect_sessions() as conn:
        plan = [
            r[3]
            for r in conn.execute(
                "EXPLAIN QUERY PLAN SELECT s.id FROM sessions s WHERE s.archived_at IS NULL "
                "ORDER BY s.updated_at DESC LIMIT 50 OFFSET 0"
            )
        ]
    assert not any("MATERIALIZE" in p for p in plan), plan
    assert not any("messages" in p for p in plan), plan
    assert not any("token_usage" in p for p in plan), plan


def test_the_sidebars_ordering_has_an_index_of_its_own():
    """Without it the id page was still SCAN sessions plus a temp B-tree.

    Only the LIVE page becomes fully index-ordered. The archived page keeps
    reaching its rows through the narrower partial `idx_sessions_archived`
    and still sorts them, which is the right trade: it is opened by hand,
    not polled every ten seconds, and the population behind it only shrinks.
    What both must avoid is reading the whole sessions table.
    """
    _bulk("live", 5)
    _bulk("arch", 5, archived=True)
    with connect_sessions() as conn:
        live = [
            r[3]
            for r in conn.execute(
                "EXPLAIN QUERY PLAN SELECT s.id FROM sessions s WHERE s.archived_at IS NULL "
                "ORDER BY s.updated_at DESC LIMIT 50"
            )
        ]
        arch = [
            r[3]
            for r in conn.execute(
                "EXPLAIN QUERY PLAN SELECT s.id FROM sessions s WHERE s.archived_at IS NOT NULL "
                "ORDER BY s.updated_at DESC LIMIT 50"
            )
        ]
    assert any("idx_sessions_recency" in p for p in live), live
    assert not any("USE TEMP B-TREE" in p for p in live), live
    assert not any("SCAN s" in p for p in live), live
    assert not any("SCAN s" in p for p in arch), arch


def test_the_enrichment_reaches_each_session_through_an_index():
    with connect_sessions() as conn:
        plan = [
            r[3]
            for r in conn.execute("EXPLAIN QUERY PLAN " + db._ENRICH_SQL.format(placeholders="?,?,?"), ("a", "b", "c"))
        ]
    assert not any("MATERIALIZE" in p for p in plan), plan
    assert not any("SCAN messages" in p for p in plan), plan
    assert not any("SCAN token_usage" in p for p in plan), plan
    assert any("idx_messages_session_role" in p for p in plan), plan
    assert any("idx_token_usage_session" in p for p in plan), plan


# ---------------------------------------------------------------------------
# (b) bounded work — the page does not pay for history it never shows
# ---------------------------------------------------------------------------


def _page_ms(limit: int = 25, **kw) -> float:
    db.list_sessions_enriched(limit=limit, **kw)
    t = time.perf_counter()
    for _ in range(5):
        db.list_sessions_enriched(limit=limit, **kw)
    return (time.perf_counter() - t) / 5 * 1000


@pytest.mark.slow
def test_archived_history_does_not_enter_the_live_page():
    _bulk("live", 25, messages=20)
    lean = _page_ms()
    _bulk("arch", 150, messages=200, archived=True)  # 30,000 messages, none of them shown
    fat = _page_ms()
    assert fat < max(lean * 4, lean + 25), f"lean {lean:.2f} ms -> fat {fat:.2f} ms with 30k archived messages"


@pytest.mark.slow
def test_excluded_types_do_not_enter_the_page_either():
    _bulk("live", 25, messages=20)
    lean = _page_ms(exclude_types=["canary"])
    _bulk("cana", 150, messages=120, session_type="canary")
    fat = _page_ms(exclude_types=["canary"])
    assert fat < max(lean * 4, lean + 25), f"lean {lean:.2f} ms -> fat {fat:.2f} ms with 18k canary messages"


@pytest.mark.slow
def test_a_small_page_costs_less_than_a_large_one():
    """The old shape made `limit=25` and `limit=500` cost the same.

    Twenty times the rows, so the gap is a property of the query rather than
    of the clock: a 1.5x margin is well inside it and well outside noise.
    """
    _bulk("live", 500, messages=6)
    small = _page_ms(25)
    large = _page_ms(500)
    assert large > small * 1.5, f"limit=25 {small:.2f} ms vs limit=500 {large:.2f} ms — the limit bounds nothing"


# ---------------------------------------------------------------------------
# (c) the space union: one pass, bounded, and counted
# ---------------------------------------------------------------------------


def test_the_union_no_longer_doubles_the_enrichment():
    space = db.create_space(label="Research", slug="research", color="#8ab4f8")["id"]
    _bulk("sp", 30, messages=4, space_id=space)
    statements: list[str] = []
    real = db.connect_sessions

    class _Ctx:
        def __enter__(self):
            self._cm = real()
            conn = self._cm.__enter__()
            outer = self

            class _Spy:
                def execute(self, sql, *a):
                    statements.append(sql)
                    return conn.execute(sql, *a)

                def __getattr__(self, n):
                    return getattr(conn, n)

            return _Spy()

        def __exit__(self, *a):
            return self._cm.__exit__(*a)

    db.connect_sessions, saved = (lambda: _Ctx()), db.connect_sessions
    try:
        db.list_sessions_enriched(limit=5)
    finally:
        db.connect_sessions = saved

    assert sum(1 for s in statements if "first_message" in s) == 1
    assert sum(1 for s in statements if "space_id IS NOT NULL" in s) == 1, "the union is an id query, and only one"


def test_the_space_union_has_a_limit_at_all():
    space = db.create_space(label="Research", slug="research", color="#8ab4f8")["id"]
    _bulk("sp", db.SPACE_UNION_FLOOR + 200, space_id=space)
    rows = db.list_sessions_enriched(limit=25)
    assert len(rows) <= db.SPACE_UNION_FLOOR + 25, f"{len(rows)} rows came back for a 25-row page"


async def test_the_response_carries_each_spaces_true_size():
    """Bounded is not hidden: a paged group still reports how big it is."""
    space = db.create_space(label="Research", slug="research", color="#8ab4f8")["id"]
    _bulk("sp", db.SPACE_UNION_FLOOR + 40, space_id=space)

    async with _client() as c:
        body = (await c.get("/api/sessions?limit=25")).json()

    assert body["space_counts"][space] == db.SPACE_UNION_FLOOR + 40
    assert len(body["items"]) < body["space_counts"][space] + 25


async def test_the_archived_answer_has_no_space_counts_because_it_has_no_union():
    space = db.create_space(label="Research", slug="research", color="#8ab4f8")["id"]
    _bulk("sp", 3, space_id=space)

    async with _client() as c:
        body = (await c.get("/api/sessions?limit=25&archived=1")).json()

    assert body["space_counts"] == {}


# ---------------------------------------------------------------------------
# (d) the impact correction: this was never an event-loop bug
# ---------------------------------------------------------------------------


def test_every_sidebar_query_runs_on_a_worker_thread(monkeypatch):
    """`asyncio.to_thread` was already right here and must stay right.

    Asserted by thread identity, not by a stopwatch. A heartbeat gap under
    a parallel test runner measures GIL contention as readily as it measures
    a blocked loop — the audit's own run saw 9.16 ms against a 5.15 ms
    baseline and correctly called this NOT an event-loop bug. What would be
    a real regression is one of these calls losing its thread hop.
    """
    import threading

    _bulk("live", 20, messages=4)
    main = threading.current_thread()
    seen: dict[str, object] = {}

    for name in (
        "list_sessions_enriched",
        "list_spaces",
        "count_sessions_by_type",
        "count_sessions",
        "count_live_sessions_by_space",
    ):
        real = getattr(db, name)

        def wrapper(*a, _real=real, _name=name, **kw):
            seen[_name] = threading.current_thread()
            return _real(*a, **kw)

        monkeypatch.setattr(db, name, wrapper)

    async def go():
        async with _client() as c:
            return await c.get("/api/sessions?limit=20")

    resp = asyncio.run(go())

    assert resp.status_code == 200
    assert seen, "no sidebar query was observed at all"
    off_loop = {n: t for n, t in seen.items() if t is not main}
    assert set(seen) == set(off_loop), f"ran on the event loop thread: {set(seen) - set(off_loop)}"


# ---------------------------------------------------------------------------
# (e) the sidebar still behaves like a sidebar
# ---------------------------------------------------------------------------


async def test_paging_ordering_and_totals_survive_the_rewrite():
    ids = []
    for i in range(12):
        sid = db.create_session(title=f"chat {i}")
        with connect_sessions() as conn:
            conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (f"2026-09-{i+1:02d}", sid))
        ids.append(sid)
    newest_first = list(reversed(ids))

    async with _client() as c:
        first = (await c.get("/api/sessions?limit=5&offset=0")).json()
        second = (await c.get("/api/sessions?limit=5&offset=5")).json()
        third = (await c.get("/api/sessions?limit=5&offset=10")).json()

    assert [s["id"] for s in first["items"]] == newest_first[:5]
    assert [s["id"] for s in second["items"]] == newest_first[5:10]
    assert [s["id"] for s in third["items"]] == newest_first[10:]
    assert first["total"] == 12 and first["has_more"] is True
    assert third["has_more"] is False


async def test_an_older_page_is_the_page_behind_the_first_one():
    """A loaded older page must not re-deliver rows already on screen."""
    _bulk("live", 40)
    async with _client() as c:
        page1 = (await c.get("/api/sessions?limit=20&offset=0")).json()
        page2 = (await c.get("/api/sessions?limit=20&offset=20")).json()
    a = {s["id"] for s in page1["items"]}
    b = {s["id"] for s in page2["items"]}
    assert len(a) == len(b) == 20
    assert not (a & b)


async def test_overlapping_refreshes_return_the_same_page():
    """Three polls in flight at once — the sidebar's real traffic pattern."""
    _bulk("live", 60, messages=4)
    async with _client() as c:
        results = await asyncio.gather(*[c.get("/api/sessions?limit=30") for _ in range(3)])
    bodies = [r.json() for r in results]
    assert all(r.status_code == 200 for r in results)
    ids = [[s["id"] for s in b["items"]] for b in bodies]
    assert ids[0] == ids[1] == ids[2]
    assert all(b["total"] == 60 for b in bodies)


async def test_the_row_the_sidebar_renders_is_unchanged():
    sid = db.create_session(title="chatty")
    db.add_message(sid, "user", "the first thing said")
    db.add_message(sid, "assistant", "a reply")
    db.add_token_usage(sid, "m", 100, 50, total_tokens=150, cost_estimate=0.5)

    async with _client() as c:
        row = (await c.get("/api/sessions?limit=5")).json()["items"][0]

    for field in (
        "id",
        "title",
        "subtitle",
        "session_type",
        "space_id",
        "archived_at",
        "pinned",
        "state_v2",
        "created_at",
        "updated_at",
        "message_count",
        "total_tokens",
        "total_cost",
        "first_message",
        "read_only",
        "read_only_reason",
    ):
        assert field in row, f"the sidebar lost {field}"
    assert row["message_count"] == 2
    assert row["total_tokens"] == 150
    assert row["first_message"] == "the first thing said"


# ---------------------------------------------------------------------------
# (f) the index has to reach an existing database, not just a fresh one
# ---------------------------------------------------------------------------


def test_the_recency_migration_is_present_and_the_list_stays_ordered():
    """The runner skips any version <= the database's current one, so a
    MIGRATIONS list that is not ascending silently drops an entry on every
    existing database, forever, without failing. Several streams add
    migrations in one batch; this pins the invariant rather than an absolute
    number, which is merge-order dependent and says nothing on its own."""
    from db.database import MIGRATIONS

    versions = [m[0] for m in MIGRATIONS]
    assert versions == sorted(versions), f"MIGRATIONS out of order: {versions}"
    assert len(versions) == len(set(versions)), f"duplicate migration version: {versions}"
    recency = [m for m in MIGRATIONS if "idx_sessions_recency" in " ".join(m[2])]
    assert len(recency) == 1, "the sidebar recency index migration is missing"


def test_the_index_reaches_a_database_that_predates_it(tmp_path, monkeypatch):
    """A live box gets the index by upgrade, not by a fresh schema."""
    from db.database import MIGRATIONS, connect_sessions, init_sessions_db

    recency_version = next(m[0] for m in MIGRATIONS if "idx_sessions_recency" in " ".join(m[2]))

    monkeypatch.setattr("config.settings.db_path", str(tmp_path / "old.db"))
    monkeypatch.setattr("db.database.MIGRATIONS", [m for m in MIGRATIONS if m[0] < recency_version])
    init_sessions_db()
    with connect_sessions() as conn:
        assert "idx_sessions_recency" not in {r["name"] for r in conn.execute("PRAGMA index_list(sessions)")}

    monkeypatch.undo()
    monkeypatch.setattr("config.settings.db_path", str(tmp_path / "old.db"))
    init_sessions_db()
    with connect_sessions() as conn:
        assert "idx_sessions_recency" in {r["name"] for r in conn.execute("PRAGMA index_list(sessions)")}
        version = int(conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0])
        assert version >= recency_version
