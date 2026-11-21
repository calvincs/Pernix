"""The move breaker counted imported history as moves and raised a false alarm.

Box, 2026-11-21: a "Memory maintenance is churning: ~355 entries frozen by the
move breaker" notice fired every day. Nothing was churning. When the ledger
was first deployed it was filled with "seed" rows: past moves read from tags
in the memory files. The breaker and the notice counted those rows the same as
moves a sweep made. Most of the frozen entries had no move under the ledger at
all, and no entry had more than two.

Seed rows exist so the cooldown and reversal rules hold from the first cycle.
They still do. They no longer count toward the breaker or the notice.
"""

from __future__ import annotations

import time

import pytest

from core.memory import moves
from core.memory.store import MemoryStore

_DAY = 86400


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from db.database import init_memory_db

    init_memory_db()
    return MemoryStore(str(tmp_path / "memories"))


def _insert(conn, body, hops, reason, age_days):
    """Ledger rows for `body`: one per (src, dst) hop, `age_days` old."""
    at = int(time.time()) - age_days * _DAY
    conn.executemany(
        "INSERT INTO memory_moves (body_key, src, dst, at, reason) VALUES (?, ?, ?, ?, ?)",
        [(moves.body_key(body), s, d, at + i, reason) for i, (s, d) in enumerate(hops)],
    )
    conn.commit()


_HOPS = [
    ("pernix.a_notes", "pernix.b_notes"),
    ("pernix.b_notes", "pernix.c_notes"),
    ("pernix.c_notes", "pernix.d_notes"),
]


def test_seed_rows_do_not_trip_the_breaker(store):
    conn = store._connect()
    try:
        _insert(conn, "Imported body", _HOPS, "seed", age_days=10)
        h = moves.load_history(conn)[moves.body_key("Imported body")]
    finally:
        conn.close()
    assert h.recent == 0
    assert moves.refusal(h, "pernix.d_notes", "pernix.e_notes") == ""


def test_seed_rows_still_hold_the_cooldown_and_reversal_rules(store, monkeypatch):
    conn = store._connect()
    try:
        _insert(conn, "Fresh import", _HOPS[:1], "seed", age_days=1)
        _insert(conn, "Old import", _HOPS[:1], "seed", age_days=10)
        hist = moves.load_history(conn, [moves.body_key("Fresh import"), moves.body_key("Old import")])
    finally:
        conn.close()
    assert moves.refusal(hist[moves.body_key("Fresh import")], "pernix.b_notes", "pernix.c_notes") == "cooldown"
    assert moves.refusal(hist[moves.body_key("Old import")], "pernix.b_notes", "pernix.a_notes") == "reversal"


def test_real_moves_trip_the_breaker_with_or_without_seed_rows(store):
    conn = store._connect()
    try:
        _insert(conn, "Moved body", _HOPS[:1], "seed", age_days=20)
        _insert(conn, "Moved body", _HOPS, "split", age_days=10)
        h = moves.load_history(conn)[moves.body_key("Moved body")]
    finally:
        conn.close()
    assert h.recent == 3
    assert moves.refusal(h, "pernix.d_notes", "pernix.e_notes") == "breaker"


def test_the_churn_count_ignores_seed_rows(store):
    conn = store._connect()
    try:
        _insert(conn, "Imported body", _HOPS, "seed", age_days=10)
        assert moves.stats(conn)["bodies_at_breaker"] == 0
        _insert(conn, "Moved body", _HOPS, "reroute", age_days=10)
        assert moves.stats(conn)["bodies_at_breaker"] == 1
    finally:
        conn.close()
