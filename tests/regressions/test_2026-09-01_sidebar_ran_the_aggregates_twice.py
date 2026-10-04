"""Every sidebar refresh ran the whole-table aggregates twice.

list_sessions_enriched unions space sessions back in past the recency
window, and it issued that second _ENRICHED_SELECT unconditionally — even
on an install with no spaces at all, where it can only return rows the
first pass already had. The query GROUP BYs all of messages and
token_usage and runs a ROW_NUMBER() over every user message before the
outer LIMIT, so this doubled the disk and CPU of a refresh that fires on
every session event.

2026-09-08 — the skip was right and the shape underneath it was not. Even
one pass computed every aggregate in the database before the LIMIT could be
applied: `EXPLAIN QUERY PLAN` showed three MATERIALIZE steps ahead of
`SCAN s`, so a 50-row page cost the same as a 500-row one and both tracked
total history rather than page size. Measured at 1,622 sessions /
116,410 messages, 92,400 of them inside ARCHIVED sessions the page does not
show: 352.9 ms for `limit=50`, of which the ROW_NUMBER() window alone was
103.6 ms — and with a space configured, all of it twice.

The enrichment now runs against a fixed list of candidate ids, so the union
contributes ids to the SAME pass instead of repeating it. What this file
pins is therefore stronger than the count it started with: one enrichment
pass whether or not spaces exist, no aggregate materialized ahead of the
page, and a page whose cost does not follow history it never displays.
"""

import time

from db import models as db
from db.database import connect_sessions

# The enrichment statement is the one that computes per-session aggregates;
# it is recognizable by the column only it produces.
_ENRICHMENT_MARK = "first_message"


class _CountingConn:
    def __init__(self, real):
        self._real = real
        self.enriched = 0
        self.statements = []

    def execute(self, sql, *a):
        self.statements.append(sql)
        if _ENRICHMENT_MARK in sql:
            self.enriched += 1
        return self._real.execute(sql, *a)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _instrumented(monkeypatch):
    holder = {}
    real_connect = db.connect_sessions

    class _Ctx:
        def __enter__(self):
            self._cm = real_connect()
            holder["conn"] = _CountingConn(self._cm.__enter__())
            return holder["conn"]

        def __exit__(self, *a):
            return self._cm.__exit__(*a)

    monkeypatch.setattr(db, "connect_sessions", lambda: _Ctx())
    return holder


def _count_enriched_queries(monkeypatch, **kw):
    holder = _instrumented(monkeypatch)
    db.list_sessions_enriched(limit=kw.pop("limit", 10), **kw)
    return holder["conn"].enriched


def _space_with(n: int, prefix: str = "sp") -> str:
    space_id = db.create_space(label="Alpha", slug="alpha", color="#334455")["id"]
    with connect_sessions() as conn:
        conn.executemany(
            "INSERT INTO sessions (id, title, system_prompt, session_type, space_id, state,"
            " created_at, updated_at) VALUES (?, ?, '', 'normal', ?, 'idle', ?, ?)",
            [
                (
                    f"{prefix}{i:05d}",
                    f"space chat {i}",
                    space_id,
                    "2026-09-01T00:00:00+00:00",
                    "2020-01-{:02d}".format(i % 28 + 1),
                )
                for i in range(n)
            ],
        )
    return space_id


# ---------------------------------------------------------------------------
# (a) one enrichment pass — the original finding, held to a stronger line
# ---------------------------------------------------------------------------


def test_no_spaces_means_one_pass(monkeypatch):
    db.create_session(title="plain")
    assert _count_enriched_queries(monkeypatch) == 1


def test_with_a_space_the_union_joins_the_same_pass(monkeypatch):
    """It used to run the whole enrichment a second time. Now it contributes ids."""
    sid = db.create_session(title="in a space")
    space_id = db.create_space(label="Alpha", slug="alpha", color="#334455")["id"]
    with connect_sessions() as conn:
        conn.execute("UPDATE sessions SET space_id = ? WHERE id = ?", (space_id, sid))
    assert _count_enriched_queries(monkeypatch) == 1


def test_the_union_is_still_skipped_outright_when_no_space_exists(monkeypatch):
    """The 2026-09-01 optimization itself, at the level it now lives on."""
    db.create_session(title="plain")
    holder = _instrumented(monkeypatch)
    db.list_sessions_enriched(limit=10)
    joined = " ".join(holder["conn"].statements)
    assert "space_id IS NOT NULL" not in joined, "an install with no spaces asked about space membership"


def test_the_archived_page_does_not_union_spaces_back_in(monkeypatch):
    holder = _instrumented(monkeypatch)
    db.list_sessions_enriched(limit=10, archived=True)
    assert "space_id IS NOT NULL" not in " ".join(holder["conn"].statements)


# ---------------------------------------------------------------------------
# (b) bounded work — 2026-09-08
# ---------------------------------------------------------------------------


def test_nothing_is_materialized_before_the_page_is_chosen():
    """The plan, not the statement count. A syntactic change proves nothing."""
    db.create_session(title="one")
    with connect_sessions() as conn:
        page_plan = [
            r[3]
            for r in conn.execute(
                "EXPLAIN QUERY PLAN SELECT s.id FROM sessions s WHERE s.archived_at IS NULL "
                "ORDER BY s.updated_at DESC LIMIT 50 OFFSET 0"
            )
        ]
        enrich_plan = [
            r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + db._ENRICH_SQL.format(placeholders="?,?"), ("a", "b"))
        ]

    assert not any("MATERIALIZE" in p for p in page_plan), page_plan
    assert not any("MATERIALIZE" in p for p in enrich_plan), enrich_plan
    assert not any("SCAN messages" in p for p in enrich_plan), enrich_plan
    assert not any("SCAN token_usage" in p for p in enrich_plan), enrich_plan
    assert any("idx_sessions_recency" in p for p in page_plan), page_plan


def test_the_enrichment_only_ever_names_the_page_it_is_enriching(monkeypatch):
    ids = [db.create_session(title=f"chat {i}") for i in range(30)]
    holder = _instrumented(monkeypatch)
    rows = db.list_sessions_enriched(limit=5)
    enrich = [s for s in holder["conn"].statements if _ENRICHMENT_MARK in s]

    assert len(rows) == 5
    assert len(enrich) == 1
    assert enrich[0].count("?") == 5, "the enrichment was handed more ids than the page holds"
    assert len(ids) == 30  # the other 25 were never enriched


def test_unrelated_and_archived_history_does_not_enter_the_page():
    """Grow only history the page never shows; the page must not notice."""
    wanted = [db.create_session(title=f"chat {i}") for i in range(10)]
    for sid in wanted:
        db.add_message(sid, "user", "the first thing said")

    def page_ms() -> float:
        db.list_sessions_enriched(limit=10)
        t = time.perf_counter()
        for _ in range(5):
            rows = db.list_sessions_enriched(limit=10)
        assert len(rows) == 10
        return (time.perf_counter() - t) / 5 * 1000

    lean = page_ms()

    with connect_sessions() as conn:
        conn.executemany(
            "INSERT INTO sessions (id, title, system_prompt, session_type, archived_at, state,"
            " created_at, updated_at) VALUES (?, ?, '', 'normal', '2026-01-01', 'idle', ?, ?)",
            [(f"old{i:05d}", f"archived {i}", "2020-01-01", "2020-01-01") for i in range(200)],
        )
        conn.executemany(
            "INSERT INTO messages (session_id, role, content, char_count, created_at)"
            " VALUES (?, ?, ?, length(?), '2020-01-01')",
            [
                (
                    f"old{i:05d}",
                    "user" if j % 2 else "assistant",
                    "buried transcript line " * 10,
                    "buried transcript line " * 10,
                )
                for i in range(200)
                for j in range(100)
            ],
        )

    fat = page_ms()

    # 20,000 messages the sidebar never renders. The old shape aggregated
    # every one of them per refresh; the honest bound is a small multiple of
    # the lean page, not "identical" — timing on a shared core is noisy.
    assert fat < max(lean * 4, lean + 20), f"lean {lean:.2f} ms -> fat {fat:.2f} ms"


# ---------------------------------------------------------------------------
# (c) semantics the speed work must not have cost
# ---------------------------------------------------------------------------


def test_space_sessions_are_still_returned_past_the_window():
    space_id = db.create_space(label="Beta", slug="beta", color="#334455")["id"]
    old = db.create_session(title="stale space session")
    with connect_sessions() as conn:
        conn.execute(
            "UPDATE sessions SET space_id = ?, updated_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (space_id, old),
        )
    for i in range(5):
        db.create_session(title=f"newer {i}")

    ids = {r["id"] for r in db.list_sessions_enriched(limit=3)}
    assert old in ids, "a space session must never fall out of the sidebar"


def test_the_page_is_still_newest_first_and_offset_still_pages():
    ids = []
    for i in range(12):
        sid = db.create_session(title=f"chat {i}")
        with connect_sessions() as conn:
            conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (f"2026-09-{i+1:02d}", sid))
        ids.append(sid)
    newest_first = list(reversed(ids))

    first = [r["id"] for r in db.list_sessions_enriched(limit=5, offset=0)]
    second = [r["id"] for r in db.list_sessions_enriched(limit=5, offset=5)]

    assert first == newest_first[:5]
    assert second == newest_first[5:10]
    assert not set(first) & set(second)


def test_the_enriched_columns_are_unchanged():
    sid = db.create_session(title="chatty")
    db.add_message(sid, "user", "the first thing said")
    db.add_message(sid, "assistant", "a reply")
    db.add_message(sid, "tool", "tool output that is not a message count")
    db.add_token_usage(sid, "m", 100, 50, total_tokens=150, cost_estimate=0.25)
    db.add_token_usage(sid, "m", 10, 5, total_tokens=15, cost_estimate=0.75)

    row = db.list_sessions_enriched(limit=5)[0]

    assert row["id"] == sid
    assert row["message_count"] == 2, "user + assistant only, as before"
    assert row["total_tokens"] == 165
    assert abs(row["total_cost"] - 1.0) < 1e-9
    assert row["first_message"] == "the first thing said"
    assert "title" in row and "state_v2" in row, "s.* still travels whole"


def test_a_session_with_no_messages_reads_as_zero_not_null():
    sid = db.create_session(title="brand new")
    row = db.list_sessions_enriched(limit=5)[0]
    assert row["id"] == sid
    assert row["message_count"] == 0
    assert row["total_tokens"] == 0
    assert row["total_cost"] == 0
    assert row["first_message"] is None


# ---------------------------------------------------------------------------
# (d) the space union is bounded, and says so
# ---------------------------------------------------------------------------


def test_the_space_union_is_bounded_rather_than_unlimited():
    """It had no LIMIT at all: 900 live space sessions were 900 rows a poll."""
    _space_with(db.SPACE_UNION_FLOOR + 120)
    rows = db.list_sessions_enriched(limit=25)
    assert len(rows) <= db.SPACE_UNION_FLOOR + 25


def test_a_bigger_page_reaches_further_into_the_space():
    """The bound follows the caller's own paging control, not a fixed ceiling."""
    n = db.SPACE_UNION_FLOOR + 300
    _space_with(n)
    small = db.list_sessions_enriched(limit=25)
    big = db.list_sessions_enriched(limit=n + 50)
    assert len(big) > len(small)
    assert len(big) == n


def test_a_capped_group_still_knows_how_big_it_is():
    """Discoverability, not concealment: the count travels even when rows do not."""
    space_id = _space_with(db.SPACE_UNION_FLOOR + 40)
    counts = db.count_live_sessions_by_space()
    assert counts[space_id] == db.SPACE_UNION_FLOOR + 40


def test_an_archived_space_session_is_counted_nowhere_live():
    space_id = _space_with(3)
    with connect_sessions() as conn:
        conn.execute("UPDATE sessions SET archived_at = '2026-09-01' WHERE id = 'sp00000'")
    assert db.count_live_sessions_by_space()[space_id] == 2
