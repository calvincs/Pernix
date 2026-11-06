"""One-time store-wide memory compaction (2026-11-06 churn cleanup).

Drops archived sections whose body is live in any file, keeps ONE archived
copy of each body that is live nowhere (store-wide), collapses stacked
`@archived` tags, then seeds the move ledger from the last recorded hop of
every live entry. Live sections are never changed: each file's live
(epoch, content) list is checked before it is written, and the whole store's
live set is compared before and after.

Run with the server STOPPED (the store lock is per process):

    python scripts/memory_compact_once.py            # dry run, prints counts
    python scripts/memory_compact_once.py --apply    # rewrite files

Back up data/memories first.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings  # noqa: E402
from core.memory import compact, moves  # noqa: E402
from core.memory.format import parse_entries_from_markdown  # noqa: E402
from core.memory.store import MemoryStore  # noqa: E402


def live_set(store: MemoryStore) -> set[tuple[str, int, str]]:
    out = set()
    for p in sorted(store._dir.glob("*.md")):
        raw = p.read_text(encoding="utf-8", errors="replace")
        for e in parse_entries_from_markdown(p.stem, raw):
            out.add((p.stem, e.epoch, e.content))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--memory-dir", default=settings.memory_dir)
    args = ap.parse_args()

    settings.memory_dir = args.memory_dir  # the index lives beside the files
    from db.database import init_memory_db

    init_memory_db()  # creates memory_moves on an index from before 2026-11-06
    store = MemoryStore(args.memory_dir)
    files = sorted(p.stem for p in store._dir.glob("*.md"))
    before_bytes = sum((store._dir / f"{n}.md").stat().st_size for n in files)
    before_live = live_set(store)
    live_keys = {moves.body_key(c) for _, _, c in before_live}
    print(f"files={len(files)} bytes={before_bytes:,} live_entries={len(before_live):,}")

    if not args.apply:
        seen: set[str] = set()
        totals = {"bytes_after": 0, "dropped_live_twin": 0, "dropped_extra_copy": 0, "kept_tombstones": 0}
        for n in files:
            raw = (store._dir / f"{n}.md").read_text(encoding="utf-8", errors="replace")
            new_raw, st = compact.compact_text(n, raw, live_keys, seen)
            totals["bytes_after"] += len(new_raw.encode())
            for k in ("dropped_live_twin", "dropped_extra_copy", "kept_tombstones"):
                totals[k] += st[k]
        print("DRY RUN", totals)
        profile = store._dir / "user.profile.md"
        if profile.exists():
            raw = profile.read_text(encoding="utf-8", errors="replace")
            new_raw, _ = compact.compact_text("user.profile", raw, live_keys, set())
            print(f"user.profile: {len(raw.encode()):,} -> ~{len(new_raw.encode()):,} bytes")
        return 0

    totals = compact.compact_files(store, files, live_keys=live_keys, seen_dead=set())
    after_live = live_set(store)
    after_bytes = sum((store._dir / f"{n}.md").stat().st_size for n in files if (store._dir / f"{n}.md").exists())
    print("APPLIED", totals)
    print(f"bytes {before_bytes:,} -> {after_bytes:,}")
    if after_live != before_live:
        print(f"LIVE SET CHANGED: before={len(before_live)} after={len(after_live)}", file=sys.stderr)
        return 2
    print(f"live set identical ({len(after_live):,} entries)")

    seeded = moves.seed_from_store(store)
    print(f"move ledger seeded with {seeded:,} hops")
    try:
        from db import models as db

        db.set_snooze_state("move_ledger_seeded", str(seeded))
        db.set_snooze_state("compact_mtimes", "{}")
    except Exception as e:  # the rung seeds itself if this fails
        print(f"note: could not mark the ledger seeded ({e})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
