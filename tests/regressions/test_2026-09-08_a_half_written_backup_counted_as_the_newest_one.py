"""Regression: a backup that failed halfway looked exactly like a fresh one.

Shipped defect (3.2.2 audit S08). `run_backup` wrote the database snapshot
straight to its final recognized name and copied the memory corpus
afterwards. Nothing separated a finished generation from an abandoned one, so
every reader agreed on the wrong thing:

* `hours_since_last_backup()` globbed `sessions-*.db` and parsed the NAME. A
  generation that died during the corpus copy answered 0.000147 hours old, and
  maintenance.py's only retry gate is `age is None or age >= 24.0` — so one
  broken backup suppressed every retry for a full day.
* `list_snapshots()` ranked it newest, which is what a restore picks and what
  `/api/storage`'s `last_backup_at` reported.
* Rotation counted it toward `keep`. With `keep=2` and a broken generation
  between two good ones, the good older one was evicted: one usable backup,
  two reported.
* Worst, and verified deterministically with RLIMIT_FSIZE: a failure INSIDE
  `VACUUM INTO` leaves a ZERO-BYTE file wearing the current scheme's name. It
  was accepted, ranked newest, read as ~0 hours old, and suppressed retries
  for 24 h — on the exact condition (disk full) where a backup matters most.
* And the comment at the corpus rotation claimed "Families rotate
  independently so a retained DB snapshot always has its same-generation
  memory corpus." Nothing enforced it: snapshots rotated mtime-ordered and
  scheme-aware, corpora rotated by a lexicographic glob, over sets a partial
  run makes unequal.

Fix: each generation is built in a hidden `.staging-<gen>/` directory,
validated (non-empty, SQLite magic, `PRAGMA quick_check`), given a manifest,
and published by renames that put `sessions-<gen>.db` LAST. That name
appearing IS the completion boundary, so freshness, listing, restore
selection and retention all agree by construction. Rotation runs after the
publish, never before it, and corpora follow the generation they were taken
with rather than a parallel count.

Legacy generations are handled explicitly rather than swept away: a snapshot
with no manifest is a snapshot from a version that did not write them, and
is complete on the only evidence that version recorded — unless it is empty,
which is the one thing no version ever wrote on purpose.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from config import settings
from db import models as db
from scripts import backup

# Bytes for a stand-in snapshot whose contents no assertion depends on. Any
# non-empty file reads as a complete legacy generation; the point of the
# constant is that "not empty" is now load-bearing.
_LEGACY_BYTES = b"a snapshot from before manifests existed"


def _retry_due(age: float | None) -> bool:
    """maintenance.py's gate, verbatim — the only consumer of freshness."""
    return age is None or age >= 24.0


def _stamp(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y%m%d-%H%M%S")


@pytest.fixture
def corpus():
    """Six markdown files, the shape the audit injected ENOSPC into."""
    root = Path(settings.memory_dir)
    (root / "topics").mkdir(parents=True, exist_ok=True)
    for i in range(6):
        (root / "topics" / f"note-{i}.md").write_text(f"# fact {i}\n")
    db.create_session(title="something worth keeping")
    return root


@pytest.fixture
def backups(corpus):
    root = backup.backups_dir()
    root.mkdir(parents=True, exist_ok=True)
    return root


# ---------------------------------------------------------------------------
# A failed generation is not published at all
# ---------------------------------------------------------------------------


def test_a_disk_full_during_the_corpus_copy_publishes_nothing(backups, monkeypatch):
    """The filed case: ENOSPC two files into a six-file corpus."""
    copied = {"n": 0}
    real_copy = shutil.copy2

    def _full_disk(src, dst, *a, **kw):
        copied["n"] += 1
        if copied["n"] > 2:
            raise OSError(28, "No space left on device")
        return real_copy(src, dst, *a, **kw)

    monkeypatch.setattr(backup.shutil, "copy2", _full_disk)

    with pytest.raises(OSError):
        backup.run_backup(keep=3)

    assert backup.list_snapshots(backups) == [], "a failed generation must not be recognized"
    assert list(backups.glob(f"{backup._MEMORIES_PREFIX}-*")) == [], "and must not leave two-sixths of a corpus"
    assert backup.hours_since_last_backup() is None
    assert _retry_due(backup.hours_since_last_backup()), "the next hourly check must retry"


def test_a_failure_inside_vacuum_into_publishes_nothing(backups, monkeypatch):
    """The worse-than-filed case: a zero-byte file wearing the real name.

    Reproduced by writing exactly what RLIMIT_FSIZE left behind, rather than
    by exhausting a real disk.
    """

    def _dies_of_a_full_disk(dest: Path) -> Path:
        dest.write_bytes(b"")
        return dest

    monkeypatch.setattr(backup, "_snapshot_db", _dies_of_a_full_disk)

    with pytest.raises(backup.BackupIncomplete) as caught:
        backup.run_backup(keep=3)
    assert "empty" in str(caught.value)

    assert backup.list_snapshots(backups) == []
    assert backup.hours_since_last_backup() is None


def test_a_truncated_snapshot_is_caught_before_it_is_published(backups, monkeypatch):
    """Non-empty is not the same as readable — quick_check has to run."""

    def _writes_rubbish(dest: Path) -> Path:
        dest.write_bytes(b"SQLite format 3\x00" + b"\x00" * 64)
        return dest

    monkeypatch.setattr(backup, "_snapshot_db", _writes_rubbish)

    with pytest.raises(backup.BackupIncomplete):
        backup.run_backup(keep=3)
    assert backup.list_snapshots(backups) == []


def test_a_failed_run_does_not_displace_the_last_good_generation(backups, monkeypatch):
    """Rotation used to run whatever the snapshot did."""
    first = backup.run_backup(keep=1)
    good = Path(first["db"]).name

    monkeypatch.setattr(backup, "_snapshot_db", lambda dest: (dest.write_bytes(b""), dest)[1])
    with pytest.raises(backup.BackupIncomplete):
        backup.run_backup(keep=1)

    assert [s["path"].name for s in backup.list_snapshots(backups)] == [good], "keep=1 still means the good one"
    assert (backups / f"{backup._MEMORIES_PREFIX}-{first['generation']}").is_dir()


def test_a_failed_run_leaves_an_older_backup_still_reading_as_overdue(backups, monkeypatch):
    """The 24-hour suppression, stated as the gate maintenance actually uses."""
    (backups / f"sessions-{_stamp(25)}.db").write_bytes(_LEGACY_BYTES)
    assert _retry_due(backup.hours_since_last_backup()), "25h old — overdue before we start"

    monkeypatch.setattr(backup, "_snapshot_db", lambda dest: (dest.write_bytes(b""), dest)[1])
    with pytest.raises(backup.BackupIncomplete):
        backup.run_backup(keep=3)

    age = backup.hours_since_last_backup()
    assert age is not None and age >= 24.0
    assert _retry_due(age), "a failed attempt must not stand in for the backup it failed to take"


# ---------------------------------------------------------------------------
# What a completed generation looks like, and that every reader agrees
# ---------------------------------------------------------------------------


def test_a_published_generation_carries_a_manifest_that_names_its_parts(backups):
    result = backup.run_backup(keep=3)
    manifest = json.loads(Path(result["manifest"]).read_text())

    assert manifest["complete"] is True
    assert manifest["generation"] == result["generation"]
    assert manifest["db"] == Path(result["db"]).name
    assert manifest["memories"] == Path(result["memories"]).name
    assert manifest["memory_files"] == 6
    assert manifest["integrity"] == "ok"
    assert manifest["schema_version"], "a snapshot whose schema nobody recorded is a restore nobody can plan"
    # Recorded, not overclaimed: publication makes a generation whole, not
    # instantaneous. The corpus is walked after the snapshot is taken.
    assert "copied after the snapshot" in manifest["consistency"]


def test_nothing_is_visible_until_the_snapshot_lands(backups, monkeypatch):
    """The publish order, checked from inside the publish.

    The corpus and the manifest go first; `sessions-<gen>.db` goes last. A
    crash between them must leave nothing any reader calls a backup.
    """
    seen: dict = {}
    real_replace = os.replace

    def _watching_replace(src, dst):
        real_replace(src, dst)
        if Path(dst).name.startswith(f"{backup._MEMORIES_PREFIX}-"):
            seen["after_corpus"] = backup.list_snapshots(backups)
            seen["freshness_after_corpus"] = backup.hours_since_last_backup()

    monkeypatch.setattr(backup.os, "replace", _watching_replace)
    backup.run_backup(keep=3)

    assert seen["after_corpus"] == [], "a corpus on disk is not yet a backup"
    assert seen["freshness_after_corpus"] is None
    assert len(backup.list_snapshots(backups)) == 1, "and the snapshot landing is what makes it one"


def test_a_half_published_generation_is_invisible_and_then_cleaned(backups):
    """A crash after the corpus and before the snapshot: orphans, not a backup."""
    orphan = _stamp(2)
    (backups / f"{backup._MEMORIES_PREFIX}-{orphan}").mkdir()
    (backups / f"{backup._MEMORIES_PREFIX}-{orphan}" / "note.md").write_text("x")
    backup.manifest_path(backups, orphan).write_text(json.dumps({"generation": orphan, "complete": True}))

    assert backup.list_snapshots(backups) == [], "no snapshot, no generation"
    assert backup.hours_since_last_backup() is None

    backup.run_backup(keep=3)
    assert not (backups / f"{backup._MEMORIES_PREFIX}-{orphan}").exists()
    assert not backup.manifest_path(backups, orphan).exists()


def test_a_staged_generation_is_invisible_while_it_is_being_built(backups):
    """Staging wears the FINAL names — the directory is what hides them."""
    generation = _stamp(0)
    staging = backups / f"{backup._STAGING_PREFIX}{generation}"
    staging.mkdir()
    (staging / f"sessions-{generation}.db").write_bytes(b"half a snapshot")

    assert backup.list_snapshots(backups) == []
    assert backup.hours_since_last_backup() is None


def test_abandoned_staging_is_swept_and_a_live_one_is_left_alone(backups):
    stale = backups / f"{backup._STAGING_PREFIX}{_stamp(48)}"
    stale.mkdir()
    (stale / "sessions-old.db").write_bytes(b"x")
    old = time.time() - backup._STAGING_GRACE_S - 60
    os.utime(stale, (old, old))

    live = backups / f"{backup._STAGING_PREFIX}{_stamp(0.1)}"
    live.mkdir()

    backup.run_backup(keep=3)

    assert not stale.exists(), "a crashed run's staging is not kept forever"
    assert live.is_dir(), "and a run another process may still be inside is not deleted out from under it"


# ---------------------------------------------------------------------------
# Retention: slots, pairing, and not making legacy backups disappear
# ---------------------------------------------------------------------------


def test_an_incomplete_generation_does_not_spend_a_retention_slot(backups):
    """The verified theft: keep=2, a broken G2, and the good G1 was evicted."""
    (backups / f"sessions-{_stamp(72)}.db").write_bytes(_LEGACY_BYTES)  # G1, good
    broken = backups / f"sessions-{_stamp(48)}.db"
    broken.write_bytes(b"")  # G2, a VACUUM INTO that died

    result = backup.run_backup(keep=2)  # G3

    surviving = [s["path"].name for s in backup.list_snapshots(backups)]
    assert Path(result["db"]).name in surviving
    assert len(surviving) == 2, "two usable backups, which is what keep=2 promised"
    assert broken.name not in surviving
    assert not broken.exists(), "and the broken one is gone rather than counted"


def test_every_retained_snapshot_still_has_its_own_corpus(backups):
    """The invariant the old comment asserted and nothing enforced."""
    for _ in range(4):
        backup.run_backup(keep=2)

    snapshots = backup.list_snapshots(backups)
    assert len(snapshots) == 2
    for entry in snapshots:
        paired = backups / f"{backup._MEMORIES_PREFIX}-{entry['generation']}"
        assert paired.is_dir(), f"{entry['path'].name} lost its memories"
        assert sorted(p.name for p in paired.rglob("*.md")) == [f"note-{i}.md" for i in range(6)]

    corpora = sorted(p.name for p in backups.glob(f"{backup._MEMORIES_PREFIX}-*"))
    assert len(corpora) == 2, "and no corpus outlives the snapshot it was taken with"


def test_a_legacy_generation_is_still_a_backup(backups):
    """Older valid backups must not vanish because a newer scheme arrived."""
    for name in ("sessions.20260825T183703Z.db", "sessions.db.20260824-103602", f"sessions-{_stamp(30)}.db"):
        (backups / name).write_bytes(_LEGACY_BYTES)

    found = backup.list_snapshots(backups)
    assert len(found) == 3
    assert all(entry["complete"] for entry in found), "no manifest means legacy, not broken"

    age = backup.hours_since_last_backup()
    assert age is not None and 29.5 < age < 30.5, "the legacy stamped one is the freshness answer"


def test_a_zero_byte_legacy_residue_is_listed_but_never_counted(backups):
    """What an older version already left on a box that ran out of disk."""
    good = backups / f"sessions-{_stamp(30)}.db"
    good.write_bytes(_LEGACY_BYTES)
    residue = backups / f"sessions-{_stamp(1)}.db"
    residue.write_bytes(b"")

    listed = {s["path"].name: s["complete"] for s in backup.list_snapshots(backups)}
    assert listed[residue.name] is False, "visible, so an operator can see why the numbers differ"
    assert listed[good.name] is True

    age = backup.hours_since_last_backup()
    assert age is not None and age > 24, "freshness skips it, so the retry it suppressed happens"
    assert backup.snapshots_beyond_keep(5, backup.list_snapshots(backups)) == [
        s for s in backup.list_snapshots(backups) if s["path"].name == residue.name
    ], "removable however generous the keep count"


async def test_the_storage_ledger_reports_the_last_COMPLETED_backup(backups):
    """`last_backup_at` is the number an operator decides by."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from api.routers import storage

    good = backups / f"sessions-{_stamp(30)}.db"
    good.write_bytes(_LEGACY_BYTES)
    (backups / f"sessions-{_stamp(1)}.db").write_bytes(b"")

    app = FastAPI()
    app.include_router(storage.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        block = (await client.get("/api/storage")).json()["backups"]

    assert block["count"] == 1, "one backup, not two"
    assert len(block["incomplete"]) == 1
    reported = datetime.fromisoformat(block["last_backup_at"].replace("Z", "+00:00"))
    assert abs((reported - datetime.fromtimestamp(good.stat().st_mtime, tz=timezone.utc)).total_seconds()) < 2
