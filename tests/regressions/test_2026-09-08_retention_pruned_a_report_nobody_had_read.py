"""Regression — 2026-09-08, harness audit 3.2.1 H20.

Worker pruning selected on two things: session_type and age. `SELECT id FROM
sessions WHERE session_type = ? AND updated_at < ?` — no state predicate at
all. The single exemption was `watched_worker_ids()`, which reads only from
parents currently sitting in `awaiting_workers`, so the moment a parent moved
on to anything else its workers became ordinary rows waiting for a date.

Measured at the audit baseline with a 30-day window and 90-day-old rows: a
PAUSED long-research worker, an AWAITING-INPUT worker and a finished worker
whose report had never been collected were all deleted. Only the watched one
survived.

What the prune left behind was a digest line: id, quoted title, last-active
date. The comment above it said the worker's result already lives in the
parent's transcript — true only when the parent collected it, and these are
precisely the ones it had not. The digest could not fail loudly either: its
whole body is wrapped in try/except, it runs AFTER the delete loop, and the
per-row fetch that feeds it swallows its own errors, so by construction a
failed archive could not stop a single deletion.

The parent was then told something affirmatively false. With the session row
gone, get_worker_result fell through to "Worker abc12345 produced no output.
It may have failed silently or timed out. Consider retrying with
retry_worker()" — a description of a worker that never worked, for one that
had finished and filed a report.

Pinned here: pruning is a lifecycle decision, not a date comparison. A worker
is prunable once its result has been consumed or the task explicitly
abandoned; non-terminal and referenced workers are held; nothing is held
forever (an abandonment horizon eventually releases anything); a result
manifest carrying the actual result is written durably BEFORE the transcript
goes and a failed write cancels that deletion; and a worker whose transcript
retention removed is reported as exactly that.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from db import models as db
from db.database import connect_sessions

REPORT = "FINAL REPORT: the decisive benchmark number is 41.7% and the cause is a stale index."


def _ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _backdate(sid: str, days: float) -> None:
    with connect_sessions() as conn:
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (_ago(days), sid))


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr("config.settings.worker_session_retention_days", 30)
    monkeypatch.setattr("config.settings.worker_abandoned_after_days", 180)
    monkeypatch.setattr("config.settings.worker_result_manifest_retention_days", 365)


@pytest.fixture
def parent():
    pid = db.create_session(title="Parent research task")
    db.update_session(pid, state_v2="idle_ready")
    return pid


def _worker(parent_id: str, title: str, state: str, days: float, *, report: str | None = REPORT) -> str:
    wid = db.create_session(title=title, session_type="worker", parent_session_id=parent_id)
    db.update_session(wid, state_v2=state)
    if report:
        db.add_message(wid, "assistant", report)
    _backdate(wid, days)
    return wid


# ---------------------------------------------------------------------------
# What must survive
# ---------------------------------------------------------------------------


async def test_a_paused_worker_is_not_residue(windows, parent):
    from core.retention import prune_worker_sessions

    paused = _worker(parent, "Paused long research", "paused", 90)
    assert await prune_worker_sessions() == 0
    assert db.get_session(paused) is not None


async def test_a_worker_waiting_on_an_answer_is_not_residue(windows, parent):
    from core.retention import prune_worker_sessions

    waiting = _worker(parent, "Worker awaiting user input", "awaiting_user", 90)
    assert await prune_worker_sessions() == 0
    assert db.get_session(waiting) is not None


async def test_a_finished_result_nobody_read_survives_its_window(windows, parent):
    """The audit's central case: finished, filed, never collected, deleted."""
    from core.retention import prune_worker_sessions

    done = _worker(parent, "Worker FINISHED, result never collected", "idle_ready", 90)
    assert await prune_worker_sessions() == 0
    assert db.get_session(done) is not None
    msgs = db.get_messages(done)
    assert any(REPORT in (m.get("content") or "") for m in msgs)


async def test_a_watched_worker_survives_whatever_state_its_parent_is_in(windows, parent):
    """`watched_worker_ids()` only looked at parents in awaiting_workers, so a
    parent that moved on stopped protecting the work it was still holding a
    reference to."""
    from core.retention import prune_worker_sessions

    watched = _worker(parent, "Watched by an idle parent", "idle_ready", 90)
    db.update_session(parent, watched_worker_ids=f'["{watched}"]')
    db.mark_worker_result_consumed(watched)  # even consumed, a live reference holds it
    assert await prune_worker_sessions() == 0
    assert db.get_session(watched) is not None


# ---------------------------------------------------------------------------
# What may go, and what it leaves behind
# ---------------------------------------------------------------------------


async def test_consumed_terminal_work_is_pruned_and_its_result_kept(windows, parent):
    from core.retention import prune_worker_sessions

    done = _worker(parent, "Worker whose report was read", "idle_ready", 90)
    db.mark_worker_result_consumed(done)

    assert await prune_worker_sessions() == 1
    assert db.get_session(done) is None, "consumed transcripts are still residue"

    manifest = db.get_worker_manifest(done)
    assert manifest is not None, "a title/date digest is not a recoverable research result"
    assert REPORT in manifest["result"]
    assert manifest["parent_session_id"] == parent
    assert manifest["title"] == "Worker whose report was read"
    assert manifest["archived_at"]


async def test_an_explicitly_abandoned_worker_is_pruned_with_its_manifest(windows, parent):
    from core.retention import prune_worker_sessions

    dropped = _worker(parent, "Worker the parent gave up on", "idle_ready", 90)
    db.mark_worker_abandoned(dropped)

    assert await prune_worker_sessions() == 1
    assert db.get_session(dropped) is None
    assert REPORT in db.get_worker_manifest(dropped)["result"]


async def test_nothing_is_retained_forever(windows, parent):
    """The constraint cuts both ways: an unconsumed, unabandoned worker still
    has to expire, or the fix is an unbounded leak wearing a safety label."""
    from core.retention import prune_worker_sessions

    ancient = _worker(parent, "Worker nobody ever came back for", "paused", 400)
    fresh_enough = _worker(parent, "Worker from last quarter", "paused", 90)

    assert await prune_worker_sessions() == 1
    assert db.get_session(ancient) is None
    assert db.get_session(fresh_enough) is not None
    manifest = db.get_worker_manifest(ancient)
    assert REPORT in manifest["result"], "expiring a task is not a reason to lose its work"


async def test_a_failed_archive_cancels_the_deletion(windows, parent, monkeypatch):
    """The digest ran after the delete loop and swallowed its own errors, so
    it could not have blocked a prune even in principle."""
    from core import retention

    done = _worker(parent, "Worker whose archive will fail", "idle_ready", 90)
    db.mark_worker_result_consumed(done)

    def _boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(db, "upsert_worker_manifest", _boom)
    assert await retention.prune_worker_sessions() == 0
    assert db.get_session(done) is not None, "no archive, no delete"


async def test_a_prunable_worker_with_a_held_child_is_left_alone(windows, parent):
    """delete_session cascades to child sessions, so pruning a parent worker
    would take a protected sub-worker with it."""
    from core.retention import prune_worker_sessions

    lead = _worker(parent, "Lead worker", "idle_ready", 90)
    db.mark_worker_result_consumed(lead)
    sub = _worker(lead, "Sub-worker still paused", "paused", 90)

    assert await prune_worker_sessions() == 0
    assert db.get_session(lead) is not None and db.get_session(sub) is not None


# ---------------------------------------------------------------------------
# What the parent is told afterwards
# ---------------------------------------------------------------------------


async def test_the_parent_is_not_told_a_filed_report_produced_no_output(windows, parent, tmp_path, monkeypatch):
    from core.extensions.orchestration import get_worker_result
    from core.retention import prune_worker_sessions

    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path / "workspace"))
    done = _worker(parent, "Worker whose report was read", "idle_ready", 90)
    db.mark_worker_result_consumed(done)
    assert await prune_worker_sessions() == 1

    out = get_worker_result(done)
    assert "produced no output" not in out
    assert "failed silently" not in out
    assert "Consider retrying with retry_worker()" not in out, "a retry cannot retrieve a deleted transcript"
    assert REPORT in out, "the manifest is the result record; serve it"
    assert "retention" in out.lower()
    assert "ARCHIVED" in out


async def test_reading_a_result_marks_it_consumed(windows, parent, tmp_path, monkeypatch):
    """Consumption has to be recorded where consumption happens, or the
    protection above never releases anything."""
    from core.extensions.orchestration import get_worker_result
    from core.retention import prune_worker_sessions

    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path / "workspace"))
    done = _worker(parent, "Worker whose report gets read", "idle_ready", 90)
    assert await prune_worker_sessions() == 0

    assert REPORT in get_worker_result(done)
    assert (db.get_session(done) or {}).get("result_consumed_at")
    assert await prune_worker_sessions() == 1


# ---------------------------------------------------------------------------
# Two windows, not one
# ---------------------------------------------------------------------------


def test_manifests_outlive_transcripts_and_expire_on_their_own(windows, parent):
    db.upsert_worker_manifest(
        {
            "worker_id": "w-old",
            "parent_session_id": parent,
            "title": "Ancient worker",
            "result": REPORT,
            "result_source": "transcript",
        }
    )
    db.upsert_worker_manifest(
        {
            "worker_id": "w-new",
            "parent_session_id": parent,
            "title": "Recent worker",
            "result": REPORT,
            "result_source": "transcript",
        }
    )
    with connect_sessions() as conn:
        conn.execute("UPDATE worker_result_manifests SET archived_at = ? WHERE worker_id = ?", (_ago(500), "w-old"))

    from core.retention import prune_worker_manifests

    assert prune_worker_manifests(365) == 1
    assert db.get_worker_manifest("w-old") is None
    assert db.get_worker_manifest("w-new") is not None
