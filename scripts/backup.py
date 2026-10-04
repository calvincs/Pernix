#!/usr/bin/env python3
"""Pernix backup — a consistent snapshot of the data that cannot be regenerated.

Usage:
    python scripts/backup.py               # snapshot, then rotate to backup_keep_count
    python scripts/backup.py --keep 30     # override the retained-snapshot count
    python scripts/backup.py --dry-run     # list what rotation would remove; snapshot nothing
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

WHAT "COMPLETE" MEANS, AND WHERE THE LINE IS
  A generation is built in a hidden staging directory and published by ONE
  atomic rename at the end: ``sessions-<stamp>.db`` appears under its
  recognized name only after the snapshot has passed an integrity check, the
  corpus has finished copying, and the manifest is already in place. That
  rename is the completion boundary, and every reader agrees on it because
  they all key on the same name — freshness (:func:`hours_since_last_backup`),
  listing (:func:`list_snapshots`), whatever a restore picks, and rotation.

  It is the boundary because of what happened without it. The snapshot used to
  be written straight to its final name and the corpus copied afterwards, so a
  disk-full during the copy left a recognized generation with two of six
  memory files; freshness read it as 0.0001 hours old, the ``age >= 24.0``
  retry gate stayed shut for a day, and a restore would have picked it. Worse,
  a failure INSIDE ``VACUUM INTO`` left a ZERO-BYTE file wearing the current
  scheme's name — a backup of nothing, ranked newest, on the exact condition
  where backups matter most. And with ``keep=2``, a broken generation between
  two good ones evicted the older good one: one usable backup, two reported.

  What the boundary does NOT promise is a single point in time across both
  stores. The database snapshot is internally consistent — SQLite takes it
  from inside a read transaction — and the corpus copy is a walk of the
  markdown tree that happens after it. A memory written between the two is in
  the corpus and not in the database. That was true before and is true now;
  publication makes a generation whole, not instantaneous. Closing that gap
  would need the memory store to hold still for the duration, which is a
  larger coordination than a backup should demand.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import threading
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
# One manifest per generation, named so it can never be mistaken for a
# snapshot by `snapshot_scheme` (which anchors on "sessions").
_MANIFEST_PREFIX = "backup"
# Staging is a dotted directory so `list_snapshots`'s non-recursive scan and
# `hours_since_last_backup`'s non-recursive glob cannot see into it. A
# half-written snapshot in here wears its final NAME and is still invisible.
_STAGING_PREFIX = ".staging-"
# How long an abandoned staging directory is left alone before it is swept.
# Generous on purpose: the sweeper cannot tell a crashed run from a run
# another process is in the middle of, and deleting the latter's work would
# be a worse bug than the disk it holds.
_STAGING_GRACE_S = 6 * 3600

# How long the snapshot waits for a database-exclusive operation to finish
# before deferring. A backup that defers is retried at the next hourly check;
# a backup that runs into a `VACUUM` is two rebuilds of the same file.
DB_GATE_WAIT_S = 30.0

# One backup at a time in this process. Two runs would race on the same
# generation stamp, on the staging sweep, and on rotation. Cross-process
# collisions (the CLI beside the scheduled tier) still resolve safely: the
# stamp is disambiguated by `_unique` and each run publishes its own
# generation atomically.
_RUN_LOCK = threading.Lock()


class BackupIncomplete(RuntimeError):
    """A generation failed validation and was NOT published.

    Raised rather than returned so a caller cannot mistake a broken run for a
    successful one — the whole class of bug this replaces was a failed backup
    that looked, to every reader, exactly like a fresh one.
    """


def backups_dir() -> Path:
    """Where snapshots live — beside the database, so a relocated ``db_path``
    (tests, an external volume) carries its backups with it."""
    return Path(settings.db_path).resolve().parent / "backups"


def legacy_backups_dir() -> Path:
    """The directory backups lived in before the rename: ``data/.backups``.

    Nothing here ever writes to it, and yet it is not history. A boot-time
    path that predates the rename still drops a ``sessions-<stamp>.db`` and a
    ``settings-<stamp>.json`` into it every time the container starts, so on a
    box that has been redeployed for a year it holds more than the live
    directory does — 2.7 GB against 1.3 GB — under a retention count that has
    never once been applied to it.

    Only meaningful when it exists: an instance that has never run an older
    version has no such directory, and callers report nothing rather than an
    empty ledger for a path that will never appear.
    """
    return backups_dir().parent / ".backups"


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

    The separator is '_' on purpose: '_' (0x5f) sorts after '.' (0x2e), so
    `sessions-<ts>_001.db` orders *after* `sessions-<ts>.db`. With '-' it
    sorted before, and the name-ordered rotation of the day deleted the
    snapshot it had just taken. Snapshot rotation goes by mtime now, which is
    immune to that on its own, but the name is still the tiebreaker within a
    filesystem tick — and the monotonic reading is what a human scanning the
    directory expects.

    The counter continues past the highest name already present rather than
    filling the lowest gap, so the name stays monotonic even after rotation
    has removed earlier entries from the same second.
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
    # Zero-padded so `_002` still sorts after `_001` on any name-ordered read.
    return path.with_name(f"{path.stem}_{max(used) + 1:03d}{path.suffix}")


def _snapshot_db(dest: Path) -> Path:
    """VACUUM INTO the sessions DB. Uses a dedicated short-lived connection,
    not the per-thread cache: VACUUM cannot run inside a transaction, and the
    cached connection may be mid-`with` in an outer frame.

    Announced to the maintenance gate for its duration. `VACUUM INTO` reads
    the entire file from inside a transaction, which on a large database is
    long enough that an operator's Optimize arriving in the middle of it
    would be two whole-file operations on one file, neither knowing about the
    other (3.2.2 audit S07).
    """
    from db.exclusive import writing

    dest = _unique(dest)
    with writing("backup:snapshot", wait=DB_GATE_WAIT_S):
        conn = sqlite3.connect(settings.db_path)
        try:
            conn.execute("PRAGMA busy_timeout=30000")  # a checkpoint may hold the DB briefly
            conn.execute("VACUUM INTO ?", (str(dest),))
        finally:
            conn.close()
    return dest


# The 16 bytes every SQLite file starts with. A truncated or zero-length
# snapshot fails here before anything more expensive is attempted.
_SQLITE_MAGIC = b"SQLite format 3\x00"


def _verify_snapshot(path: Path) -> dict:
    """Prove the staged snapshot is a database before publishing it.

    Three checks, cheapest first: it is not empty, it starts with SQLite's
    magic, and `PRAGMA quick_check` agrees. The first two are what catch the
    verified disk-full case — a `VACUUM INTO` killed by RLIMIT_FSIZE leaves a
    zero-byte file — and the third is what catches the subtler truncations
    that still parse as a header.

    Returns the facts it established, which go into the manifest: an
    integrity claim nobody recorded is an integrity claim nobody can audit.
    """
    size = path.stat().st_size if path.exists() else 0
    if size == 0:
        raise BackupIncomplete(f"snapshot {path.name} is empty — the database was not copied")
    with path.open("rb") as fh:
        if fh.read(len(_SQLITE_MAGIC)) != _SQLITE_MAGIC:
            raise BackupIncomplete(f"snapshot {path.name} is not a SQLite database")

    conn = sqlite3.connect(path)
    try:
        integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
        if integrity != "ok":
            raise BackupIncomplete(f"snapshot {path.name} failed quick_check: {integrity}")
        row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
        schema_version = str(row[0]) if row else None
    except sqlite3.DatabaseError as e:
        raise BackupIncomplete(f"snapshot {path.name} is not readable: {e}") from e
    finally:
        conn.close()
    return {"db_bytes": size, "integrity": integrity, "schema_version": schema_version}


def _snapshot_memories(dest: Path) -> tuple[Path | None, int, int]:
    """Copy the markdown corpus, preserving its directory shape.

    Returns the destination (None if there was nothing to copy), the number
    of files copied, and the number that vanished between the walk and the
    copy. That last number is not a failure: memory dedup deletes files, and
    a corpus that lost one during the walk is still the corpus. Anything
    else — no space, no permission — raises, and the generation is discarded
    rather than published two-sixths full.
    """
    src = Path(settings.memory_dir)
    if not src.is_dir():
        return None, 0, 0
    files = sorted(p for p in src.rglob("*.md") if p.is_file())
    if not files:
        return None, 0, 0
    copied = 0
    vanished = 0
    for path in files:
        target = dest / path.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(path, target)
        except FileNotFoundError:
            vanished += 1
            continue
        copied += 1
    if copied == 0:
        return None, 0, vanished
    return dest, copied, vanished


# Every name this script — or a version of it that came before — has given a
# database snapshot. Rotation used to glob for exactly the one it writes
# today, so each rename orphaned a generation of files permanently: on a box
# that had run all three, `backup_keep_count = 7` was holding 40 snapshots and
# 2.7 GB, and no amount of waiting was going to reclaim it.
#
# The stamp each pattern captures is not parsed. It is here so the regexes
# stay anchored to a timestamp shape and cannot swallow an unrelated file that
# merely starts with "sessions".
_SNAPSHOT_SCHEMES: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Current: sessions-20260102-030405.db, plus _001 same-second collisions.
    ("stamped", re.compile(r"^sessions-\d{8}-\d{6}(?:_\d+)?\.db$")),
    # Older, in two spellings of the same ISO instant: the extended form
    # sessions.2026-01-02T03:04:05.123456+00:00.db, and the compact
    # basic form sessions.20260102T030405Z.db — which is what the oldest
    # snapshots on a long-lived box actually wear, because a filename is a
    # bad place for colons and an early version said so with strftime.
    ("iso", re.compile(r"^sessions\.(?:\d{4}-\d{2}-\d{2}[T_][0-9:.\-+T]*|\d{8}T\d{6}Z)\.db$")),
    # Older still: sessions.db.20260102-030405
    ("suffixed", re.compile(r"^sessions\.db\.\d{8}-\d{6}$")),
)


def snapshot_scheme(name: str) -> str | None:
    """Which naming scheme ``name`` belongs to, or None if it is not a snapshot.

    Anything else in the directory — ``settings-*.json`` dropped there by
    hand, the ``memories-*`` corpus directories, a hand-made
    ``sessions.db.bak-*``, the live database and its ``-wal``/``-shm``
    siblings — returns None and is never a rotation candidate.
    """
    for scheme, pattern in _SNAPSHOT_SCHEMES:
        if pattern.match(name):
            return scheme
    return None


def generation_of(name: str) -> str | None:
    """The generation stamp a current-scheme snapshot or corpus belongs to.

    ``sessions-20260102-030405_001.db`` and ``memories-20260102-030405_001``
    both answer ``20260102-030405_001``, which is what pairs them. Only the
    current scheme has generations: the older names predate the corpus copy
    entirely, so there is nothing on the other side to pair them with.
    """
    if name.startswith(f"{_DB_PREFIX}-") and name.endswith(".db") and snapshot_scheme(name) == "stamped":
        return name[len(_DB_PREFIX) + 1 : -3]
    if name.startswith(f"{_MEMORIES_PREFIX}-"):
        return name[len(_MEMORIES_PREFIX) + 1 :]
    if name.startswith(f"{_MANIFEST_PREFIX}-") and name.endswith(".json"):
        return name[len(_MANIFEST_PREFIX) + 1 : -5]
    return None


def manifest_path(root: Path, generation: str) -> Path:
    """Where a generation's manifest lives once it has been published."""
    return root / f"{_MANIFEST_PREFIX}-{generation}.json"


def read_manifest(root: Path, generation: str) -> dict | None:
    """A published generation's manifest, or None for a legacy one.

    None is the answer for every snapshot written before manifests existed,
    and it is not the same answer as "incomplete" — see :func:`_completeness`.
    """
    path = manifest_path(root, generation)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _completeness(root: Path, path: Path, size: int) -> bool:
    """Is this snapshot a finished generation?

    Three cases, in the order they occur on a real box:

    * A manifest exists — this generation was published by a version that
      writes them, and the manifest is the record. Believe it.
    * No manifest, and the file is empty — a `VACUUM INTO` that died of a
      full disk under an older version. It wears a recognized name and is
      a backup of nothing.
    * No manifest, non-empty — a legacy generation. Older valid backups must
      not vanish from an operator's listing because a newer scheme arrived,
      so this is complete, on the only evidence that scheme ever recorded.
    """
    generation = generation_of(path.name)
    if generation is not None:
        manifest = read_manifest(root, generation)
        if manifest is not None:
            return bool(manifest.get("complete"))
    return size > 0


def list_snapshots(root: Path | str | None = None) -> list[dict]:
    """Every database snapshot in ``root``, newest first.

    Ordered by mtime rather than by name: the three schemes sort against each
    other lexicographically in an order that has nothing to do with time
    ("sessions-2026…" sorts after "sessions.2026…" whatever the dates say), so
    a name-ordered rotation across all three would delete by naming era
    instead of by age. The name breaks mtime ties so the order is stable when
    two snapshots land in the same filesystem tick.

    ``complete`` is the completion boundary as every other reader sees it.
    Incomplete entries are still listed — a zero-byte snapshot an operator
    cannot see is a zero-byte snapshot they cannot delete — but nothing
    downstream counts one as a backup.
    """
    root = backups_dir() if root is None else Path(root)
    if not root.is_dir():
        return []
    found: list[dict] = []
    for path in root.iterdir():
        if not path.is_file():
            continue
        scheme = snapshot_scheme(path.name)
        if scheme is None:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        found.append(
            {
                "path": path,
                "scheme": scheme,
                "mtime": stat.st_mtime,
                "bytes": stat.st_size,
                "generation": generation_of(path.name),
                "complete": _completeness(root, path, stat.st_size),
            }
        )
    found.sort(key=lambda s: (s["mtime"], s["path"].name), reverse=True)
    return found


def snapshots_beyond_keep(keep: int, snapshots: list[dict] | None = None) -> list[dict]:
    """The snapshots rotation would remove: everything past the newest ``keep``.

    ``keep == 0`` removes nothing. In ``settings.backup_keep_count`` a zero
    means "stop taking scheduled backups", not "delete the ones I have" — the
    reading that would wipe an operator's entire history the moment they
    turned the schedule off.

    Only completed generations consume a slot. With ``keep=2`` and a broken
    generation sitting between two good ones, counting the broken one evicted
    the older good one and left the operator holding a single usable backup
    while the ledger said two. An incomplete generation is always removable
    and never protective.
    """
    if keep <= 0:
        return []
    if snapshots is None:
        snapshots = list_snapshots()
    stale: list[dict] = []
    kept = 0
    for entry in snapshots:
        if not entry.get("complete", True):
            stale.append(entry)
            continue
        if kept < keep:
            kept += 1
            continue
        stale.append(entry)
    return stale


def rotate(keep: int, dry_run: bool = False, root: Path | str | None = None) -> dict:
    """Keep the newest ``keep`` snapshots across every scheme; drop the rest.

    Returns what was (or would be) removed, the bytes it frees, and how many
    snapshots are left standing.

    ``root`` defaults to :func:`backups_dir`. It is a parameter because there
    is a second directory on a long-lived box — :func:`legacy_backups_dir` —
    and each is retained on its own: ``keep`` means "the newest ``keep`` in
    this directory", never a budget shared across both. Pooling them would
    make deleting a live snapshot the consequence of a deploy having written
    a legacy one.
    """
    snapshots = list_snapshots(root)
    stale = snapshots_beyond_keep(keep, snapshots)
    removed: list[str] = []
    freed = 0
    for entry in stale:
        if dry_run:
            removed.append(entry["path"].name)
            freed += entry["bytes"]
            continue
        try:
            entry["path"].unlink()
        except OSError as e:
            logger.warning("Could not rotate out %s: %s", entry["path"], e)
            continue
        removed.append(entry["path"].name)
        freed += entry["bytes"]
    return {"removed": removed, "bytes_freed": freed, "kept": len(snapshots) - len(removed)}


def _rotate_companions(root: Path, newest_generation: str | None) -> list[str]:
    """Delete the corpora and manifests whose DB snapshot is gone.

    Replaces a `keep`-counted sweep of ``memories-*``. That sweep held the
    newest N corpora by lexicographic name while :func:`rotate` held the
    newest N snapshots by mtime, over sets a partial run could make unequal —
    two independent rules, and a comment claiming they agreed. They agree now
    because there is only one rule: a corpus is retained exactly as long as
    the snapshot it was taken with, so a retained snapshot always has its
    same-generation memories and nothing else keeps a slot warm.

    ``newest_generation`` is the run that just published, and anything at or
    after it is left alone: another process may be mid-publish, and its
    corpus lands before its snapshot does.
    """
    live = {entry["generation"] for entry in list_snapshots(root) if entry["generation"]}
    removed: list[str] = []
    companions = sorted(root.glob(f"{_MEMORIES_PREFIX}-*")) + sorted(root.glob(f"{_MANIFEST_PREFIX}-*.json"))
    for stale in companions:
        generation = generation_of(stale.name)
        if generation is None or generation in live:
            continue
        if newest_generation is not None and generation >= newest_generation:
            continue
        try:
            if stale.is_dir():
                shutil.rmtree(stale)
            else:
                stale.unlink()
            removed.append(stale.name)
        except OSError as e:
            logger.warning("Could not rotate out %s: %s", stale, e)
    return removed


def _sweep_abandoned_staging(root: Path) -> list[str]:
    """Remove staging directories a crashed run left behind.

    Only ones older than :data:`_STAGING_GRACE_S`, because this cannot tell a
    dead run from a live one in another process, and a sweeper that deleted
    the second kind would have turned a tidy-up into the very failure it
    exists to clean up after.
    """
    cutoff = datetime.now(timezone.utc).timestamp() - _STAGING_GRACE_S
    removed: list[str] = []
    for path in sorted(root.glob(f"{_STAGING_PREFIX}*")):
        if not path.is_dir():
            continue
        try:
            if path.stat().st_mtime > cutoff:
                continue
            shutil.rmtree(path)
        except OSError as e:
            logger.warning("Could not clear abandoned staging %s: %s", path, e)
            continue
        removed.append(path.name)
        logger.info("Cleared abandoned backup staging: %s", path.name)
    return removed


def hours_since_last_backup() -> float | None:
    """Age of the newest COMPLETED DB snapshot in hours, by name stamp.

    None when no completed snapshot exists (or none parses) — callers treat
    that as overdue, which is the entire retry mechanism. Reads names, not
    mtimes, for the same reason rotation once did: a restore or an rsync
    rewrites mtimes but not the generation stamp.

    Completeness is why this is not a bare glob any more. A generation that
    failed halfway used to answer 0.0001 hours here, and maintenance.py's
    only retry gate is ``age is None or age >= 24.0`` — so one broken backup
    suppressed every retry for a full day, on the failure where a retry is
    worth the most.
    """
    root = backups_dir()
    newest: datetime | None = None
    for path in root.glob(f"{_DB_PREFIX}-*.db"):
        stamp = path.stem[len(_DB_PREFIX) + 1 :].split("_", 1)[0]  # drop any _NNN collision counter
        try:
            taken = datetime.strptime(stamp, "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if not _completeness(root, path, size):
            continue
        if newest is None or taken > newest:
            newest = taken
    if newest is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - newest).total_seconds() / 3600.0)


def _publish(root: Path, staging: Path, generation: str, staged_db: Path, staged_memories: Path | None) -> None:
    """Move a validated generation out of staging, snapshot LAST.

    The order is the whole design. Everything that reads a backup keys on
    ``sessions-<generation>.db``, so that name appearing is the event the
    system treats as "there is a backup here" — put it last and there is no
    interval in which a half-published generation is recognized. A crash
    before it leaves an orphan corpus and an orphan manifest, which no reader
    looks at and :func:`_rotate_companions` clears on the next run; a crash
    after it leaves nothing to clean up, because there is nothing left.

    ``os.replace`` throughout: staging is a sibling directory inside the
    backups root, so every rename is within one filesystem and atomic.
    """
    if staged_memories is not None:
        final_memories = root / staged_memories.name
        if final_memories.exists():
            shutil.rmtree(final_memories)
        os.replace(staged_memories, final_memories)
    os.replace(staging / "manifest.json", manifest_path(root, generation))
    os.replace(staged_db, root / staged_db.name)


def run_backup(keep: int | None = None) -> dict:
    """Take one snapshot and rotate old ones. Returns a summary dict.

    Raises on failure — callers decide whether a failed backup is fatal
    (a CLI run) or a logged warning (the maintenance tier) — and a raise now
    means nothing was published: the generation is built under staging names,
    validated, and only then moved into place. A failed run leaves freshness
    where it was, so the next hourly check retries it, and leaves the last
    good generation exactly where it was, because rotation happens after the
    publish rather than before it.
    """
    resolved = resolve_keep(keep)
    if resolved == 0:
        return {"skipped": "backup_keep_count is 0", "keep": 0}

    root = backups_dir()
    root.mkdir(parents=True, exist_ok=True)

    with _RUN_LOCK:
        _sweep_abandoned_staging(root)

        # The final name is chosen against the published directory, not
        # against staging: `_unique` exists to disambiguate two backups
        # landing in the same second, and the collision it has to see is with
        # what is already on disk.
        final_db = _unique(root / f"{_DB_PREFIX}-{_timestamp()}.db")
        # Name the corpus from the DB snapshot's *actual* stem, not from a
        # second call to _timestamp(): if the name was disambiguated, or the
        # clock ticked between the two, the pair would otherwise carry
        # different generations and a restore could mix a DB with someone
        # else's memories.
        generation = final_db.stem[len(_DB_PREFIX) + 1 :]

        staging = root / f"{_STAGING_PREFIX}{generation}"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        try:
            staged_db = _snapshot_db(staging / final_db.name)
            facts = _verify_snapshot(staged_db)
            staged_memories, mem_files, mem_vanished = _snapshot_memories(staging / f"{_MEMORIES_PREFIX}-{generation}")
            manifest = {
                "generation": generation,
                "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "complete": True,
                "db": staged_db.name,
                "memories": staged_memories.name if staged_memories else None,
                "memory_files": mem_files,
                "memory_files_vanished": mem_vanished,
                # Recorded, not asserted: the snapshot is internally
                # consistent because SQLite took it inside a read
                # transaction; the corpus was walked afterwards. A generation
                # is whole, not instantaneous. See the module docstring.
                "consistency": "db: transactional (VACUUM INTO); memories: copied after the snapshot",
                **facts,
            }
            (staging / "manifest.json").write_text(json.dumps(manifest, indent=2))
            _publish(root, staging, generation, staged_db, staged_memories)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        shutil.rmtree(staging, ignore_errors=True)

        db_path = root / final_db.name
        mem_path = root / f"{_MEMORIES_PREFIX}-{generation}" if staged_memories else None

        # Rotation runs only now, with the new generation already standing.
        # It used to run whatever the snapshot did, so a broken run could
        # evict a good backup and replace it with nothing.
        #
        # Snapshots rotate scheme-aware (every name this script ever wrote);
        # corpora and manifests follow the generation they belong to.
        #
        # `root` is named rather than defaulted: the scheduled backup writes
        # to, and sweeps, exactly the directory it just wrote into. The legacy
        # directory is rotated only when an operator asks for it, because a
        # scheduled job quietly deleting from a directory this script does not
        # write to is a surprise nobody consented to.
        removed = rotate(resolved, root=root)["removed"]
        removed += _rotate_companions(root, generation)

    return {
        "dir": str(root),
        "generation": generation,
        "db": str(db_path),
        "db_bytes": db_path.stat().st_size,
        "manifest": str(manifest_path(root, generation)),
        "memories": str(mem_path) if mem_path else None,
        "memory_files": mem_files,
        "memory_files_vanished": mem_vanished,
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
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Take no snapshot; list the snapshots rotation would remove and stop",
    )
    args = parser.parse_args(argv)

    if args.dry_run:
        # Deliberately does not snapshot first: a dry run that wrote a new
        # backup before reporting would change the very set it is reporting on.
        keep = resolve_keep(args.keep)
        plan = rotate(keep, dry_run=True)
        if args.json:
            print(json.dumps({"dry_run": True, "keep": keep, **plan}, indent=2))
        else:
            print(f"Backup directory: {backups_dir()}")
            print(f"  retaining the newest {keep} snapshot(s) across all naming schemes")
            if plan["removed"]:
                for name in plan["removed"]:
                    print(f"  would remove: {name}")
                print(f"  would free {plan['bytes_freed']:,} bytes, leaving {plan['kept']} snapshot(s)")
            else:
                print(f"  nothing to remove — {plan['kept']} snapshot(s) present")
        return 0

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
