"""A failed COMMIT left the transaction open on a reused connection.

Connections are cached per thread. CPython's sqlite3 __exit__ calls
commit() and, if that raises, propagates without rolling back — so the
transaction stayed open and the NEXT `with conn:` block on that thread
inherited it. That block's own commit or rollback then decided the fate of
the earlier block's writes: an unrelated helper's error could silently
discard a message already reported as saved.

Rare in WAL (a COMMIT seldom blocks), which is why it was rated plausible
rather than observed — but the recovery costs nothing and the failure is
silent, so _TrackedConnection.__exit__ now rolls back before re-raising.
"""

import sqlite3

import pytest

from db.database import _TrackedConnection


class _FailingExit(sqlite3.Connection):
    """Stands in for sqlite3's own __exit__ when its COMMIT returns BUSY.

    Injected through the MRO rather than by patching: sqlite3.Connection is
    an immutable C type, its __exit__ commits in C (so a Python override of
    commit() is never consulted), and a real lock makes the INSERT fail
    before any transaction is opened. Placing this class between
    _TrackedConnection and Connection makes _TrackedConnection's
    `super().__exit__(...)` land here.
    """

    def __exit__(self, *exc_info):
        raise sqlite3.OperationalError("database is locked")


class _CommitFails(_TrackedConnection, _FailingExit):
    pass


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "t.db"), factory=_CommitFails)
    c.execute("CREATE TABLE t (v TEXT)")
    c.commit()
    yield c
    c.close()


def test_a_failed_commit_leaves_no_open_transaction(conn):
    with pytest.raises(sqlite3.OperationalError):
        with conn:
            conn.execute("INSERT INTO t VALUES ('first block')")
    assert not conn.in_transaction, "the next block on this cached connection would inherit it"
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0, "and its writes must not survive"


def test_the_checkout_counter_still_balances_after_a_failure(conn):
    with pytest.raises(sqlite3.OperationalError):
        with conn:
            pass
    assert conn._checkouts == 0, "a leaked checkout would pin the connection against eviction"


def test_an_ordinary_error_inside_the_block_still_rolls_back(tmp_path):
    c = sqlite3.connect(str(tmp_path / "u.db"), factory=_TrackedConnection)
    c.execute("CREATE TABLE t (v TEXT)")
    c.commit()
    try:
        with pytest.raises(ValueError):
            with c:
                c.execute("INSERT INTO t VALUES ('doomed')")
                raise ValueError("boom")
        assert c.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
        assert c._checkouts == 0
    finally:
        c.close()
