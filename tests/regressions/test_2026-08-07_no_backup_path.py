"""Regression: there was no backup path, and the documented one was wrong.

Shipped defect (found in the 2026-08-07 architecture review): Pernix had no
backup script, no endpoint and no scheduled dump. The entire story was one
sentence in docs/upgrade.md — "`cp -r data data.backup` works fine" — which is
wrong for a live WAL database: the newest committed rows sit in
`sessions.db-wal` until a checkpoint folds them back, so copying `sessions.db`
alone yields a stale snapshot, and copying the pair separately races the
checkpointer. For a server whose whole value is accumulated memory, the data
had no recovery path at all.

Fix: scripts/backup.py takes a `VACUUM INTO` snapshot (consistent by
construction, taken by SQLite from inside a read transaction) plus a copy of
the markdown memory corpus, and rotates to settings.backup_keep_count.
maintenance.py's 24h tier calls it — first in the tier, before the sweeps that
mutate data.

Extended 2026-09-08 (3.2.2 audit S08): a backup path that produces a
generation nothing can tell apart from a finished one is not a recovery path
either. The publication tests below pin the boundary — a snapshot appears
under its recognized name only after it has been validated and its corpus is
whole — and the full failure-injection suite is in
tests/regressions/test_2026-09-08_a_half_written_backup_counted_as_the_newest_one.py.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from config import settings
from db import models as db
from db.database import connect_sessions
from scripts import backup


def test_snapshot_captures_writes_still_sitting_in_the_wal(tmp_path):
    """The whole point: a write that has NOT been checkpointed must be in the
    snapshot. This is precisely what `cp sessions.db` loses."""
    sid = db.create_session(title="unflushed")
    assert Path(settings.db_path + "-wal").exists(), "expected WAL mode (the case this guards)"

    result = backup.run_backup(keep=3)
    snap = sqlite3.connect(result["db"])
    try:
        row = snap.execute("SELECT title FROM sessions WHERE id = ?", (sid,)).fetchone()
    finally:
        snap.close()
    assert row is not None and row[0] == "unflushed"

    # A naive copy of the main DB file, for contrast: it does not see the row.
    naive = tmp_path / "naive-copy.db"
    naive.write_bytes(Path(settings.db_path).read_bytes())
    conn = sqlite3.connect(naive)
    try:
        assert conn.execute("SELECT COUNT(*) FROM sessions WHERE id = ?", (sid,)).fetchone()[0] == 0
    finally:
        conn.close()


def test_snapshot_is_a_standalone_readable_database():
    db.create_session(title="restorable")
    result = backup.run_backup(keep=3)
    snap = sqlite3.connect(result["db"])
    try:
        assert snap.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        version = snap.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()[0]
    finally:
        snap.close()
    with connect_sessions() as live:
        live_version = live.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()[0]
    assert version == live_version


def test_markdown_corpus_is_copied_and_the_rebuildable_index_is_not():
    mem = Path(settings.memory_dir)
    (mem / "topics").mkdir(parents=True, exist_ok=True)
    (mem / "topics" / "note.md").write_text("# durable fact\n")
    (mem / "_index.db").write_bytes(b"rebuildable")

    result = backup.run_backup(keep=3)
    assert result["memory_files"] == 1
    dest = Path(result["memories"])
    assert (dest / "topics" / "note.md").read_text() == "# durable fact\n"
    assert not (dest / "_index.db").exists(), "the FTS index rebuilds from markdown; backing it up is dead weight"


def test_rotation_keeps_the_newest_n_of_each_artifact():
    (Path(settings.memory_dir) / "m.md").parent.mkdir(parents=True, exist_ok=True)
    (Path(settings.memory_dir) / "m.md").write_text("x")

    for _ in range(5):
        backup.run_backup(keep=2)

    root = backup.backups_dir()
    assert len(sorted(root.glob("sessions-*.db"))) == 2
    assert len(sorted(root.glob("memories-*"))) == 2


def test_keep_count_is_bounds_checked(monkeypatch):
    assert backup.resolve_keep(-5) == backup.KEEP_MIN
    assert backup.resolve_keep(10_000) == backup.KEEP_MAX
    assert backup.resolve_keep(7) == 7
    # 0 is the off switch for the scheduled tier, not an error.
    monkeypatch.setattr(settings, "backup_keep_count", 0)
    assert backup.run_backup()["skipped"]
    assert not backup.backups_dir().exists()


def test_a_snapshot_only_exists_once_the_whole_generation_does():
    """The recovery path is a generation, not a file.

    The snapshot is built under staging and renamed into its recognized name
    last, after the corpus is copied and the manifest written — so there is
    no interval in which a restore could pick a database whose memories are
    still being copied.
    """
    (Path(settings.memory_dir) / "note.md").parent.mkdir(parents=True, exist_ok=True)
    (Path(settings.memory_dir) / "note.md").write_text("# durable\n")

    result = backup.run_backup(keep=3)
    root = backup.backups_dir()

    assert Path(result["db"]).parent == root, "published beside the others, not inside staging"
    assert not list(root.glob(f"{backup._STAGING_PREFIX}*")), "staging is cleared on the way out"

    manifest = json.loads(Path(result["manifest"]).read_text())
    assert manifest["complete"] is True
    assert manifest["db"] == Path(result["db"]).name
    assert manifest["memories"] == Path(result["memories"]).name
    assert manifest["integrity"] == "ok"


def test_a_backup_that_never_finished_leaves_the_path_open(monkeypatch):
    """A failed attempt must not read as the backup it failed to take.

    Freshness is the only thing standing between a failure and a 24-hour
    silence: maintenance retries on `age is None or age >= 24.0`, and a
    half-written generation used to answer 0.0001.
    """

    def _dies_mid_snapshot(dest: Path) -> Path:
        dest.write_bytes(b"")  # what a full disk leaves inside VACUUM INTO
        return dest

    monkeypatch.setattr(backup, "_snapshot_db", _dies_mid_snapshot)
    with pytest.raises(backup.BackupIncomplete):
        backup.run_backup(keep=3)

    assert backup.hours_since_last_backup() is None, "still no backup, so still overdue"
    assert backup.list_snapshots() == []


async def test_maintenance_24h_tier_takes_a_backup(monkeypatch):
    """Pin the wiring, not just the script: the 24h tier is the only thing
    that makes backups happen without a human remembering to."""
    from maintenance import MaintenanceRunner

    runner = MaintenanceRunner()
    runner._tick_count = 1440

    class _StubManager:
        def reap_dead_subscribers(self):
            return 0

        def reap_idle_sessions(self, **_kw):
            return 0

    monkeypatch.setattr("sessions.manager.get_manager", lambda: _StubManager())
    await runner._tick()

    assert sorted(backup.backups_dir().glob("sessions-*.db")), "24h tier did not produce a snapshot"
