"""Rotation across every snapshot naming scheme the backup script has used.

The bug this pins: rotation globbed for the one name it writes today, so the
snapshots left behind by earlier versions were invisible to it forever. A box
set to keep 7 was holding 40 files and 2.7 GB.
"""

import os
import time

import pytest

from config import settings
from scripts import backup

# One name per scheme, oldest first — the order the box acquired them in.
ISO = "sessions.2026-01-02T03:04:05.123456+00:00.db"
SUFFIXED = "sessions.db.20260201-030405"
STAMPED = "sessions-20260301-030405.db"
STAMPED_COLLISION = "sessions-20260301-030405_001.db"

# The same ISO instant in its compact spelling — the oldest scheme on the box,
# and the one the "iso" pattern used to miss because it insisted on dashes.
COMPACT_ISO = "sessions.20251202T030405Z.db"

# Things that live in the same directory and are not snapshots.
NON_SNAPSHOTS = (
    "settings-20260102.json",
    "settings.json",
    "sessions.db",
    "sessions.db-wal",
    "sessions.db-shm",
    "README",
)


@pytest.fixture
def backups(tmp_path, monkeypatch):
    """A backups dir holding all three schemes plus files rotation must ignore.

    mtimes are set explicitly and one second apart: the fixture's own write
    order is not a fact any assertion should depend on.
    """
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "sessions.db"))
    root = backup.backups_dir()
    root.mkdir(parents=True, exist_ok=True)

    base = time.time() - 10_000
    for i, name in enumerate([ISO, SUFFIXED, STAMPED, STAMPED_COLLISION]):
        path = root / name
        path.write_bytes(b"x" * (100 * (i + 1)))
        os.utime(path, (base + i, base + i))
    for name in NON_SNAPSHOTS:
        (root / name).write_text("not a snapshot")
    (root / "memories-20260301-030405").mkdir()
    (root / "memories-20260301-030405" / "notes.md").write_text("corpus")
    return root


def test_list_snapshots_finds_all_three_schemes(backups):
    found = backup.list_snapshots(backups)
    assert [s["path"].name for s in found] == [STAMPED_COLLISION, STAMPED, SUFFIXED, ISO], "newest first, by mtime"
    assert {s["scheme"] for s in found} == {"stamped", "suffixed", "iso"}
    assert [s["bytes"] for s in found] == [400, 300, 200, 100]


def test_list_snapshots_ignores_everything_else(backups):
    names = {s["path"].name for s in backup.list_snapshots(backups)}
    assert names.isdisjoint(NON_SNAPSHOTS)
    assert "memories-20260301-030405" not in names


def test_scheme_labels():
    assert backup.snapshot_scheme(ISO) == "iso"
    assert backup.snapshot_scheme(COMPACT_ISO) == "iso"
    assert backup.snapshot_scheme(SUFFIXED) == "suffixed"
    assert backup.snapshot_scheme(STAMPED) == "stamped"
    assert backup.snapshot_scheme(STAMPED_COLLISION) == "stamped"
    for name in NON_SNAPSHOTS:
        assert backup.snapshot_scheme(name) is None, name


def test_the_names_on_the_box():
    """The literal filenames beside the production database, verbatim.

    The compact ISO form is the box's oldest scheme and five files wore it;
    until the pattern accepted a stamp without dashes they were invisible to
    rotation forever, which is exactly the bug the scheme table exists to stop.
    The three non-snapshots are the near misses that share a prefix: a settings
    dump, a hand-made ``.bak`` of the database, and a memory corpus directory.
    """
    assert backup.snapshot_scheme("sessions.20260825T183703Z.db") == "iso"
    assert backup.snapshot_scheme("sessions-20260826-024434.db") == "stamped"
    assert backup.snapshot_scheme("sessions.db.20260824-103602") == "suffixed"
    assert backup.snapshot_scheme("settings-20260902-130414.json") is None
    assert backup.snapshot_scheme("sessions.db.bak-20260831-132226") is None
    assert backup.snapshot_scheme("memories-20260902-160055") is None


def test_compact_iso_snapshots_rotate_like_any_other(backups):
    """A scheme that is recognised but never rotated would be no better."""
    path = backups / COMPACT_ISO
    path.write_bytes(b"x" * 50)
    os.utime(path, (time.time() - 20_000, time.time() - 20_000))  # older than all four

    found = backup.list_snapshots(backups)
    assert [s["path"].name for s in found][-1] == COMPACT_ISO, "oldest, so last"
    assert found[-1]["scheme"] == "iso"

    result = backup.rotate(4)
    assert result["removed"] == [COMPACT_ISO]
    assert result["bytes_freed"] == 50
    assert not path.exists()


def test_rotate_keeps_newest_across_schemes(backups):
    result = backup.rotate(2)
    assert result["removed"] == [SUFFIXED, ISO], "the two oldest, whatever their naming era"
    assert result["bytes_freed"] == 300
    assert result["kept"] == 2
    left = {p.name for p in backups.iterdir()}
    assert left == {STAMPED, STAMPED_COLLISION, *NON_SNAPSHOTS, "memories-20260301-030405"}


def test_rotate_dry_run_deletes_nothing(backups):
    before = sorted(p.name for p in backups.iterdir())
    result = backup.rotate(1, dry_run=True)
    assert result["removed"] == [STAMPED, SUFFIXED, ISO]
    assert result["bytes_freed"] == 600
    assert result["kept"] == 1
    assert sorted(p.name for p in backups.iterdir()) == before


def test_rotate_keep_zero_removes_nothing(backups):
    """0 means "stop taking backups", not "delete the ones I have"."""
    result = backup.rotate(0)
    assert result == {"removed": [], "bytes_freed": 0, "kept": 4}
    assert len(backup.list_snapshots(backups)) == 4


def test_rotate_below_keep_is_a_no_op(backups):
    result = backup.rotate(10)
    assert result["removed"] == []
    assert result["kept"] == 4


def test_run_backup_sweeps_the_old_schemes_out(backups, monkeypatch):
    """The regression itself: a real backup run must rotate every era.

    Before the fix this kept the one snapshot it had just written and left the
    three older-scheme files in place indefinitely.
    """
    monkeypatch.setattr(settings, "memory_dir", str(backups.parent / "memories"))
    from db.database import init_db

    init_db()

    result = backup.run_backup(keep=2)

    assert ISO in result["rotated_out"]
    assert SUFFIXED in result["rotated_out"]
    remaining = backup.list_snapshots(backups)
    assert len(remaining) == 2
    assert remaining[0]["scheme"] == "stamped", "the snapshot just taken survives its own rotation"


def test_cli_dry_run_takes_no_snapshot(backups, capsys, monkeypatch):
    monkeypatch.setattr(settings, "backup_keep_count", 2)
    assert backup.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert ISO in out and SUFFIXED in out
    assert STAMPED not in out.replace(STAMPED_COLLISION, ""), "retained snapshots are not listed for removal"
    assert len(backup.list_snapshots(backups)) == 4, "a dry run writes nothing and deletes nothing"


def test_cli_dry_run_json(backups, capsys, monkeypatch):
    import json

    monkeypatch.setattr(settings, "backup_keep_count", 1)
    assert backup.main(["--dry-run", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["keep"] == 1
    assert payload["removed"] == [STAMPED, SUFFIXED, ISO]


def test_hours_since_last_backup_still_reads_names(backups):
    """maintenance.py imports this; the rotation change must not disturb it."""
    age = backup.hours_since_last_backup()
    assert age is not None and age > 0
