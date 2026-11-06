"""One-time: retire live duplicate copies, compact, and shrink the index.

2026-11-06 follow-up to memory_compact_once.py. On the box 4,088 extra live
copies sat across 2,234 bodies (old moves that copied without retiring the
source), and _index.db was 166 MB for 9 MB of live text: 12k orphan
vectors and an unmerged FTS5 index.

  1. keep one live copy per (body, type); archive the rest, hits summed in;
  2. compact store-wide (drop tombstones whose body is live);
  3. prune orphan vectors, merge FTS5 segments ('optimize'), VACUUM.

Run with the server STOPPED. Dry run by default; --apply to change files.
Invariant checked: the set of distinct live (body, type) is unchanged.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings  # noqa: E402
from core.memory import compact  # noqa: E402
from core.memory.moves import body_key  # noqa: E402
from core.memory.store import MemoryStore  # noqa: E402


def distinct_live(store) -> set[tuple[str, str]]:
    conn = store._connect()
    try:
        return {(body_key(c), t or "note") for c, t in conn.execute("SELECT content, entry_type FROM memory_fts")}
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--memory-dir", default=settings.memory_dir)
    args = ap.parse_args()
    settings.memory_dir = args.memory_dir
    from db.database import init_memory_db

    init_memory_db()
    store = MemoryStore(args.memory_dir)
    idx = Path(args.memory_dir) / "_index.db"
    groups = compact.live_twin_groups(store)
    extra = sum(len(g) - 1 for g in groups)
    before = distinct_live(store)
    print(f"live twin groups={len(groups)} extra copies={extra} index={idx.stat().st_size / 1e6:.1f} MB")
    if not args.apply:
        return 0

    res = compact.dedup_live_twins(store)
    print("dedup", res)
    after = distinct_live(store)
    if after != before:
        print(f"DISTINCT LIVE SET CHANGED: {len(before)} -> {len(after)}", file=sys.stderr)
        return 2
    names = sorted(p.stem for p in store._dir.glob("*.md"))
    print("compact", compact.compact_files(store, names, seen_dead=set()))
    conn = store._connect()
    try:
        print("orphan vectors pruned", store._prune_orphan_vectors(conn))
        conn.execute("INSERT INTO memory_fts(memory_fts) VALUES('optimize')")
        conn.commit()
    finally:
        conn.close()
    import sqlite3

    raw = sqlite3.connect(str(idx))
    raw.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    raw.execute("VACUUM")
    raw.close()
    survey = store._survey()
    print(
        f"index={os.path.getsize(idx) / 1e6:.1f} MB store_mb={survey['store_mb']} in_sync={survey['in_sync']} "
        f"indexed={survey['indexed_entries']} live twins left={len(compact.live_twin_groups(store))}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
