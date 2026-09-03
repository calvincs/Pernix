"""Every sidebar refresh ran the whole-table aggregates twice.

list_sessions_enriched unions space sessions back in past the recency
window, and it issued that second _ENRICHED_SELECT unconditionally — even
on an install with no spaces at all, where it can only return rows the
first pass already had. The query GROUP BYs all of messages and
token_usage and runs a ROW_NUMBER() over every user message before the
outer LIMIT, so this doubled the disk and CPU of a refresh that fires on
every session event.
"""

from db import models as db
from db.database import connect_sessions


class _CountingConn:
    def __init__(self, real):
        self._real = real
        self.enriched = 0

    def execute(self, sql, *a):
        if "ROW_NUMBER()" in sql or "GROUP BY" in sql:
            self.enriched += 1
        return self._real.execute(sql, *a)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _count_enriched_queries(monkeypatch):
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
    db.list_sessions_enriched(limit=10)
    return holder["conn"].enriched


def test_no_spaces_means_one_pass(monkeypatch):
    db.create_session(title="plain")
    assert _count_enriched_queries(monkeypatch) == 1


def test_with_a_space_the_union_still_runs(monkeypatch):
    sid = db.create_session(title="in a space")
    space_id = db.create_space(label="Alpha", slug="alpha", color="#334455")["id"]
    with connect_sessions() as conn:
        conn.execute("UPDATE sessions SET space_id = ? WHERE id = ?", (space_id, sid))
    assert _count_enriched_queries(monkeypatch) == 2


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
