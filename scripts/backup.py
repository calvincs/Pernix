#!/usr/bin/env python3
"""Pernix backup — a consistent snapshot of the data that cannot be regenerated.

Usage:
    python scripts/backup.py               # snapshot, then rotate to backup_keep_count
    python scripts/backup.py --keep 30     # override the retained-snapshot count
    python scripts/backup.py --json        # machine-readable result

Called on demand here and from maintenance.py's 24h tier — one implementation,
two callers.

WHAT IS CAPTURED
  * ``data/sessions.db`` via ``VACUUM INTO``. SQLite takes the snapshot itself
    from inside a read transaction, so the copy is transactionally consistent
    even while the server is writing. ``cp sessions.db`` is NOT equivalent: in
    WAL mode the newest committed rows live in ``sessions.db-wal`` until a
    checkpoint folds them back, so a plain copy is stale at best and torn at
    worst (copying the -wal separately races the checkpointer).
  * ``data/memories/**/*.md`` — the memory corpus. Markdown is the source of
    truth for memory; everything else about memory is derived from it.

WHAT IS DELIBERATELY NOT CAPTURED
  * ``data/memories/_index.db`` — the FTS5 + vector index. It is a projection
    of the markdown and the memory store's health check rebuilds it, so
    backing it up would only add bytes and a second thing to keep in sync.
  * ``data/workspace/`` — agent scratch space, reproducible by definition, and
    large (it can contain a venv).
  * ``data/settings.json``, ``.env``, ``data/certs/`` — configuration rather
    than accumulated state, and the files most likely to hold secrets. A
    rotating plaintext copy of the auth token and provider API keys next to
    the database is a liability, not a safety net. Back those up deliberately,
    to wherever you keep secrets.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure repo root is on sys.path when invoked directly.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from config import settings  # noqa: E402

logger = logging.getLogger("pernix.backup")

# Retained-snapshot bounds. 0 means "don't take scheduled backups"; the upper
# bound stops a fat-fingered setting from filling the disk on an unattended box.
KEEP_MIN = 0
KEEP_MAX = 90

_DB_PREFIX = "sessions"
_MEMORIES_PREFIX = "memories"


def backups_dir() -> Path:
    """Where snapshots live — beside the database, so a relocated ``db_path``
    (tests, an external volume) carries its backups with it."""
    return Path(settings.db_path).resolve().parent / "backups"


def resolve_keep(keep: int | None = None) -> int:
    """Clamp the retention count into KEEP_MIN..KEEP_MAX."""
    raw = settings.backup_keep_count if keep is None else keep
    try:
        raw = int(raw)
    except (TypeError, ValueError):
        raw = 0
    return max(KEEP_MIN, min(raw, KEEP_MAX))


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _unique(path: Path) -> Path:
    """Second-resolution timestamps collide when two backups land in the same
    second (a manual run racing the scheduled one). VACUUM INTO refuses to
    overwrite, so disambiguate rather than fail.

    The separator is '_' on purpose: rotation sorts by name, and '_' (0x5f)
    sorts after '.' (0x2e), so `sessions-<ts>_001.db` orders *after*
    `sessions-<ts>.db`. With '-' it sorted before, and rotation deleted the
    snapshot it had just taken.

    The counter continues past the highest name already present rather than
    filling the lowest gap, so the name stays monotonic even after rotation
    has removed earlier entries from the same second — otherwise a reused
    lower name would sort as the oldest and be rotated out immediately.
    """
    used: set[int] = set()
    for sibling in path.parent.glob(f"{path.stem}*"):
        name = sibling.name
        if name == path.name:
            used.add(0)
            continue
        tail = name[len(path.stem) :]
        if path.suffix and tail.endswith(path.suffix):
            tail = tail[: -len(path.suffix)]
        if tail.startswith("_") and tail[1:].isdigit():
            used.add(int(tail[1:]))
    if not used:
        return path
    # Zero-padded so `_002` still sorts after `_001` — rotation is name-ordered.
    return path.with_name(f"{path.stem}_{max(used) + 1:03d}{path.suffix}")


def _snapshot_db(dest: Path) -> Path:
    """VACUUM INTO the sessions DB. Uses a dedicated short-lived connection,
    not the per-thread cache: VACUUM cannot run inside a transaction, and the
    cached connection may be mid-`with` in an outer frame."""
    dest = _unique(dest)
    conn = sqlite3.connect(settings.db_path)
    try:
        conn.execute("PRAGMA busy_timeout=30000")  # a checkpoint may hold the DB briefly
        conn.execute("VACUUM INTO ?", (str(dest),))
    finally:
        conn.close()
    return dest


def _snapshot_memories(dest: Path) -> tuple[Path | None, int]:
    """Copy the markdown corpus, preserving its directory shape. Returns the
    destination (None if there was nothing to copy) and the file count."""
    src = Path(settings.memory_dir)
    if not src.is_dir():
        return None, 0
    files = sorted(p for p in src.rglob("*.md") if p.is_file())
    if not files:
        return None, 0
    for path in files:
        target = dest / path.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    return dest, len(files)


def _rotate(root: Path, pattern: str, keep: int) -> list[str]:
    """Keep the newest ``keep`` entries matching ``pattern``, delete the rest.

    Names are ``<prefix>-YYYYMMDD-HHMMSS``, so lexicographic order is
    chronological order — no stat() calls, and no dependence on mtimes that a
    restore or an rsync would have rewritten. Families rotate independently so
    a retained DB snapshot always has its same-generation memory corpus.
    """
    entries = sorted(root.glob(pattern))
    removed: list[str] = []
    for stale in entries[: max(0, len(entries) - keep)]:
        try:
            if stale.is_dir():
                shutil.rmtree(stale)
            else:
                stale.unlink()
            removed.append(stale.name)
        except OSError as e:
            logger.warning("Could not rotate out %s: %s", stale, e)
    return removed


def hours_since_last_backup() -> float | None:
    """Age of the newest DB snapshot in hours, by name-encoded timestamp.

    None when no snapshot exists (or none parses) — callers treat that as
    overdue. Reads names, not mtimes, for the same reason _rotate does: a
    restore or an rsync rewrites mtimes but not the generation stamp.
    """
    newest: datetime | None = None
    for path in backups_dir().glob(f"{_DB_PREFIX}-*.db"):
        stamp = path.stem[len(_DB_PREFIX) + 1 :].split("_", 1)[0]  # drop any _NNN collision counter
        try:
            taken = datetime.strptime(stamp, "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if newest is None or taken > newest:
            newest = taken
    if newest is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - newest).total_seconds() / 3600.0)


def run_backup(keep: int | None = None) -> dict:
    """Take one snapshot and rotate old ones. Returns a summary dict.

    Raises on failure — callers decide whether a failed backup is fatal
    (a CLI run) or a logged warning (the maintenance tier).
    """
    resolved = resolve_keep(keep)
    if resolved == 0:
        return {"skipped": "backup_keep_count is 0", "keep": 0}

    root = backups_dir()
    root.mkdir(parents=True, exist_ok=True)

    db_path = _snapshot_db(root / f"{_DB_PREFIX}-{_timestamp()}.db")
    # Name the corpus from the DB snapshot's *actual* stem, not from a second
    # call to _timestamp(): if the name was disambiguated, or the clock ticked
    # between the two, the pair would otherwise carry different generations
    # and a restore could mix a DB with someone else's memories.
    generation = db_path.stem[len(_DB_PREFIX) + 1 :]
    mem_path, mem_files = _snapshot_memories(root / f"{_MEMORIES_PREFIX}-{generation}")

    removed = _rotate(root, f"{_DB_PREFIX}-*.db", resolved)
    removed += _rotate(root, f"{_MEMORIES_PREFIX}-*", resolved)

    return {
        "dir": str(root),
        "db": str(db_path),
        "db_bytes": db_path.stat().st_size,
        "memories": str(mem_path) if mem_path else None,
        "memory_files": mem_files,
        "rotated_out": removed,
        "keep": resolved,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pernix backup — VACUUM INTO snapshot + memory corpus copy")
    parser.add_argument(
        "--keep",
        type=int,
        default=None,
        help=f"Snapshots to retain (default: settings.backup_keep_count; clamped to {KEEP_MIN}..{KEEP_MAX})",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of plaintext")
    args = parser.parse_args(argv)

    # A manual run is an explicit request, so honour --keep 0 as "keep only
    # this one" rather than as the scheduler's "disabled" meaning.
    keep = args.keep if args.keep is None else max(1, args.keep)
    result = run_backup(keep=keep)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Backup directory: {result['dir']}")
        print(f"  database:  {Path(result['db']).name}  ({result['db_bytes']:,} bytes)")
        if result["memories"]:
            plural = "" if result["memory_files"] == 1 else "s"
            print(f"  memories:  {Path(result['memories']).name}/  ({result['memory_files']} markdown file{plural})")
        else:
            print("  memories:  (none found — nothing to copy)")
        if result["rotated_out"]:
            print(f"  rotated out: {', '.join(result['rotated_out'])}")
        print(f"  retaining {result['keep']} snapshot(s) per artifact")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
