"""Regression: the daily backup starved whenever the process restarted daily.

Shipped defect (found in the 2026-08-15 box audit): the backup ran in
maintenance's 24h tier, keyed on `tick % 1440 == 0` — and the tick counter
starts at zero on every process start. A box that deploys (restarts) more
often than once a day never reaches tick 1440, so every deploy day was
silently a no-backup day: the live box took no scheduled backup between
Aug 11 and Aug 15 across a four-day deploy streak, while logging nothing.

Fix: due-ness comes from the newest snapshot's own name-encoded timestamp
(`scripts.backup.hours_since_last_backup`), checked hourly (`tick % 60`), so
the schedule survives restarts and drifts at most one hour per day.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from scripts import backup


class _StubManager:
    def reap_dead_subscribers(self):
        return 0

    def reap_idle_sessions(self, **_kw):
        return 0


async def _run_tick(monkeypatch, tick: int) -> None:
    from maintenance import MaintenanceRunner

    runner = MaintenanceRunner()
    runner._tick_count = tick
    monkeypatch.setattr("sessions.manager.get_manager", lambda: _StubManager())
    await runner._tick()


def test_age_is_none_with_no_snapshots():
    assert not sorted(backup.backups_dir().glob("sessions-*.db"))
    assert backup.hours_since_last_backup() is None


def test_age_reads_the_name_stamp_not_the_mtime():
    root = backup.backups_dir()
    root.mkdir(parents=True, exist_ok=True)
    stamp = (datetime.now(timezone.utc) - timedelta(hours=30)).strftime("%Y%m%d-%H%M%S")
    (root / f"sessions-{stamp}.db").write_bytes(b"")  # fresh mtime, old name
    (root / "sessions-not-a-stamp.db").write_bytes(b"")  # malformed: ignored
    age = backup.hours_since_last_backup()
    assert age is not None and 29.5 < age < 30.5


async def test_hourly_tick_takes_the_backup_when_overdue(monkeypatch):
    """The restart scenario: fresh process (low tick), stale last snapshot."""
    root = backup.backups_dir()
    root.mkdir(parents=True, exist_ok=True)
    stamp = (datetime.now(timezone.utc) - timedelta(hours=25)).strftime("%Y%m%d-%H%M%S")
    (root / f"sessions-{stamp}.db").write_bytes(b"")

    await _run_tick(monkeypatch, tick=60)  # one hour of uptime — not 24
    assert len(sorted(root.glob("sessions-*.db"))) == 2, "overdue backup did not run on the hourly check"


async def test_hourly_tick_skips_when_a_snapshot_is_fresh(monkeypatch):
    result = backup.run_backup(keep=3)
    before = sorted(backup.backups_dir().glob("sessions-*.db"))
    assert result["db"]

    await _run_tick(monkeypatch, tick=60)
    assert sorted(backup.backups_dir().glob("sessions-*.db")) == before, "fresh snapshot must not be duplicated hourly"
