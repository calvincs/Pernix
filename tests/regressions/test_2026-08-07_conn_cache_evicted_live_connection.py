"""Regression: cache eviction could close a connection an outer frame held.

Shipped defect (found in the 2026-08-07 architecture review,
db/database.py _connect): when a thread's connection cache reached
_CONN_CACHE_MAX, eviction closed *every* cached connection and cleared the
dict — including one an outer stack frame was mid-`with` on. The next
statement on that connection raises ProgrammingError and its in-flight
transaction is gone. The code's own comment conceded it was safe only by
arithmetic coincidence ("tests rotate tmp DB paths; prod uses 2 paths"): the
sessions DB plus the memory DB plus two tmp paths is exactly the cap.

Fix: connections are opened through _TrackedConnection, which counts `with`
checkouts, and eviction skips anything currently checked out. The cap became a
soft target — briefly exceeding it costs a file handle, closing a live
connection costs a transaction.
"""

from __future__ import annotations

import sqlite3

from db import database
from db.database import _CONN_CACHE_MAX, _connect, connect_sessions


def test_eviction_does_not_close_a_checked_out_connection(tmp_path):
    with connect_sessions() as conn:
        conn.execute("INSERT INTO sessions (id, title) VALUES ('held', 'held')")

        # Force eviction while `conn` is checked out by opening more distinct
        # paths than the cache holds.
        for i in range(_CONN_CACHE_MAX + 2):
            other = _connect(str(tmp_path / f"rotate-{i}.db"))
            other.execute("CREATE TABLE IF NOT EXISTS t (a)")

        # Pre-fix this raised ProgrammingError: closed database.
        row = conn.execute("SELECT title FROM sessions WHERE id = 'held'").fetchone()
        assert row["title"] == "held"

    # And the outer block's write survived to commit.
    with connect_sessions() as verify:
        assert verify.execute("SELECT COUNT(*) FROM sessions WHERE id = 'held'").fetchone()[0] == 1


def test_idle_connections_are_still_evicted(tmp_path):
    """The cap must keep doing its job — the fix narrows eviction, it does not
    disable it."""
    before = len(database._conn_local.conns)
    opened = [_connect(str(tmp_path / f"idle-{i}.db")) for i in range(_CONN_CACHE_MAX + 3)]
    assert len(database._conn_local.conns) <= max(before, _CONN_CACHE_MAX) + 1
    # At least one of the early connections was actually closed, not merely
    # dropped from the dict.
    closed = 0
    for conn in opened[:-1]:
        try:
            conn.total_changes
        except sqlite3.ProgrammingError:
            closed += 1
    assert closed > 0


def test_checkout_counter_returns_to_zero():
    conn = connect_sessions()
    assert conn._checkouts == 0
    with conn:
        assert conn._checkouts == 1
        with conn:  # sqlite3 connections are re-entrant as context managers
            assert conn._checkouts == 2
        assert conn._checkouts == 1
    assert conn._checkouts == 0

    # Exceptions must not leak a checkout, or the connection becomes
    # permanently unevictable.
    try:
        with conn:
            raise ValueError("boom")
    except ValueError:
        pass
    assert conn._checkouts == 0
