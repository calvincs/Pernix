"""Pernix — Storage: how much there is, and what can be given back.

The numbers an owner needs to answer "why is this box full?" were spread
across four places and one of them was a shell prompt: the sessions table
(how many, of what kind), the database file (how big, and how much of that
is free pages SQLite will reuse but never hand back), the snapshot directory
(which grows on a schedule and rotated only the files the *current* naming
scheme produced), and the retention sweeps that already run in the
background. This router is the one place that reports all four, so Settings →
Storage can show a ledger instead of a link to `du`.

Everything here touches files or SQLite, so every handler does its work in a
thread — a `VACUUM` on a 168 MB database is seconds of blocking IO, and the
event loop is also serving the SSE stream that the same page is watching.

Paths are reported in full. `db_path` is redacted out of `GET /api/settings`
because it is machine-local configuration that has no business round-tripping
through a settings form; here it is the answer to the question being asked,
and the endpoint sits behind the same auth as everything else.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException

from config import settings

router = APIRouter(tags=["storage"])


# ---------------------------------------------------------------------------
# Readers (all called through asyncio.to_thread)
# ---------------------------------------------------------------------------


def _session_ledger() -> dict:
    """Counts by session type, plus the three cross-cutting flags.

    `archived` is None until the column exists: archiving lands in a later
    change, and a ledger that reported 0 archived sessions on a schema that
    cannot archive would be a lie rather than a zero.
    """
    from db import models as db

    with db.connect_sessions() as conn:
        by_type: dict[str, int] = {}
        for row in conn.execute("SELECT COALESCE(session_type, 'normal') AS t, COUNT(*) AS c FROM sessions GROUP BY t"):
            by_type[str(row["t"])] = int(row["c"])

        def _scalar(sql: str) -> int:
            row = conn.execute(sql).fetchone()
            return int(row[0]) if row else 0

        pinned = _scalar("SELECT COUNT(*) FROM sessions WHERE pinned = 1")
        in_spaces = _scalar("SELECT COUNT(*) FROM sessions WHERE space_id IS NOT NULL")

        columns = {str(r["name"]) for r in conn.execute("PRAGMA table_info(sessions)")}
        archived = (
            _scalar("SELECT COUNT(*) FROM sessions WHERE archived_at IS NOT NULL") if "archived_at" in columns else None
        )

    return {
        "total": sum(by_type.values()),
        "by_type": dict(sorted(by_type.items())),
        "pinned": pinned,
        "in_spaces": in_spaces,
        "archived": archived,
    }


def _database_ledger() -> dict:
    """File sizes from the filesystem, page accounting from SQLite.

    `bytes` is what `ls` reports, not page_count * page_size: in WAL mode the
    two disagree by however much the last checkpoint left behind, and the
    owner is looking at the same disk `ls` is.
    """
    from db import models as db

    path = Path(settings.db_path)
    try:
        db_bytes = path.stat().st_size
    except OSError:
        db_bytes = 0
    try:
        wal_bytes = Path(str(path) + "-wal").stat().st_size
    except OSError:
        wal_bytes = 0

    with db.connect_sessions() as conn:
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0] or 4096)
        freelist = int(conn.execute("PRAGMA freelist_count").fetchone()[0] or 0)

    return {
        "path": str(path.resolve()),
        "bytes": db_bytes,
        "wal_bytes": wal_bytes,
        "page_size": page_size,
        # Free pages: space deleted rows already gave up inside the file.
        # SQLite reuses them for new rows but never returns them to the
        # filesystem — only VACUUM does that, which is what /optimize is for.
        "reclaimable_bytes": freelist * page_size,
    }


def _dir_bytes(root: Path) -> int:
    total = 0
    for entry in root.rglob("*"):
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


def _backups_ledger(root: Path | None = None) -> dict:
    """Every snapshot in one backup directory, under every name it has worn.

    `count` and `beyond_keep` are about database snapshots — the files
    rotation governs. `bytes` is the whole directory, because that is the
    number `du` gives and the memory corpora beside the snapshots are part of
    what is filling the disk.

    Takes a root because there are two of these directories on a box that has
    been redeployed for long enough, and the same ledger describes both.
    """
    from scripts import backup

    root = backup.backups_dir() if root is None else root
    keep = backup.resolve_keep()
    if not root.is_dir():
        return {"dir": str(root), "count": 0, "bytes": 0, "keep": keep, "last_backup_at": None, "beyond_keep": []}

    snapshots = backup.list_snapshots(root)
    last_at = (
        datetime.fromtimestamp(snapshots[0]["mtime"], tz=timezone.utc).isoformat().replace("+00:00", "Z")
        if snapshots
        else None
    )
    return {
        "dir": str(root),
        "count": len(snapshots),
        "bytes": _dir_bytes(root),
        "keep": keep,
        "last_backup_at": last_at,
        "beyond_keep": [
            {"name": s["path"].name, "bytes": s["bytes"], "mtime": s["mtime"], "scheme": s["scheme"]}
            for s in backup.snapshots_beyond_keep(keep, snapshots)
        ],
    }


def _legacy_backups_ledger() -> dict | None:
    """The same ledger for `data/.backups`, or None when there isn't one.

    Backups used to live in a dotted directory, and the rename left a
    boot-time path behind that still writes a database copy and a settings
    copy into the old one on every container start. So it is not a relic to
    be migrated once and forgotten: it grows on the same schedule the deploys
    do, under a retention count nothing was applying to it, and on the box it
    is the larger of the two by more than a gigabyte.

    None rather than a zeroed block when the directory is absent — most
    instances have never had one, and a ledger row for a path that does not
    exist is a question raised for no reason.
    """
    from scripts import backup

    root = backup.legacy_backups_dir()
    if not root.is_dir():
        return None
    return _backups_ledger(root)


def _sweep_counters() -> dict | None:
    """The retention counters the snooze runner already keeps.

    `get_stats()` is a dict copy of in-memory counters — the same read
    `/api/health/detailed` makes on every poll — so this costs nothing. Only
    the prune counters and the cycle stamp come through: the rest of the
    stats block is about memory synthesis, which is not a storage question.
    """
    try:
        from core.snooze import get_snooze

        stats = get_snooze().get_stats()
    except Exception:
        return None
    counters = {k: v for k, v in stats.items() if k.endswith("_pruned")}
    counters["last_cycle"] = stats.get("last_cycle")
    return counters


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/api/storage")
async def get_storage():
    """One ledger: sessions, the database file, the snapshots, the sweeps."""
    sessions, database, backups, legacy, sweeps = await asyncio.gather(
        asyncio.to_thread(_session_ledger),
        asyncio.to_thread(_database_ledger),
        asyncio.to_thread(_backups_ledger),
        asyncio.to_thread(_legacy_backups_ledger),
        asyncio.to_thread(_sweep_counters),
    )
    # `legacy_backups` is always present and usually null: a key that appears
    # only on the boxes with the problem is a key no client remembers to look
    # for. Same shape as `backups` when it is there, so one renderer does both.
    payload = {"sessions": sessions, "database": database, "backups": backups, "legacy_backups": legacy}
    if sweeps is not None:
        payload["sweeps"] = sweeps
    return payload


@router.post("/api/storage/backups/rotate")
async def rotate_backups(body: dict = {}):
    """Apply the retention count to every snapshot, not just today's naming.

    Defaults to a dry run on purpose: the caller has to ask twice before
    anything is deleted, and the first answer is the list the confirmation
    dialog names.

    `dir` picks which of the two directories to sweep — "primary" (the one
    the schedule writes) or "legacy" (`data/.backups`, which deploys still
    write and nothing has ever rotated). Each keeps its own newest `keep`;
    the count is not a budget shared between them. An unknown name is a 400
    rather than a silent fall back to the primary, because the one thing this
    endpoint must never do is delete from a directory the caller did not name.
    """
    from scripts import backup

    dry_run = bool(body.get("dry_run", True))
    which = str(body.get("dir", "primary"))
    if which not in ("primary", "legacy"):
        raise HTTPException(400, detail=f"Unknown backup directory {which!r} — expected 'primary' or 'legacy'.")

    root = backup.legacy_backups_dir() if which == "legacy" else backup.backups_dir()
    if which == "legacy" and not root.is_dir():
        raise HTTPException(404, detail="This instance has no legacy backup directory.")

    keep = backup.resolve_keep()
    result = await asyncio.to_thread(backup.rotate, keep, dry_run, root)
    return {"dry_run": dry_run, "dir": which, **result}


def _vacuum() -> tuple[int, int]:
    """`PRAGMA optimize`, then `VACUUM`, then fold the WAL back in.

    On a connection of our own, not the cached per-thread one from
    `db.connect_sessions()`: VACUUM cannot run inside a transaction and that
    connection may be mid-`with` in an outer frame — the same reasoning
    scripts/backup.py gives for its snapshot connection. The long busy_timeout
    is for WAL mode, where a checkpoint or another reader holds the database
    briefly.

    The checkpoint is the part that makes the number true. In WAL mode VACUUM
    rebuilds the database *through the write-ahead log*, so the main file is
    exactly as large the instant it finishes as it was before; it shrinks at
    the next checkpoint, whenever that happens to be. Without TRUNCATE here,
    every single run would report "reclaimed 0 bytes" while having in fact
    reclaimed everything. A busy checkpoint is not an error — a reader is
    holding the log and the next one will get it — so the size is simply read
    back as it stands.
    """
    path = Path(settings.db_path)
    before = path.stat().st_size if path.exists() else 0
    conn = sqlite3.connect(settings.db_path)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA optimize")
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    after = path.stat().st_size if path.exists() else 0
    return before, after


@router.post("/api/storage/optimize")
async def optimize_database():
    """Rebuild the database file so the free pages go back to the filesystem.

    Refused while a turn is in flight. VACUUM holds a write lock for the
    whole rebuild; on a 168 MB database that is long enough for a running
    agent's next write to time out, and losing a turn is a worse outcome
    than a full disk five minutes later.
    """
    from sessions.manager import get_manager

    if get_manager().has_active_work():
        raise HTTPException(409, detail="A turn is running — compacting would block its writes. Try again when idle.")

    before, after = await asyncio.to_thread(_vacuum)
    return {"bytes_before": before, "bytes_after": after}
