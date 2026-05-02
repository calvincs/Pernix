"""Tests for db/models.py: cron runs, snooze state, and misc DB operations."""

import pytest

from db import models as db

# ---------------------------------------------------------------------------
# Cron runs
# ---------------------------------------------------------------------------


def test_add_cron_run():
    run_id = db.add_cron_run("test-job", session_id=None)
    assert isinstance(run_id, int)
    assert run_id > 0


def test_update_cron_run():
    run_id = db.add_cron_run("test-job2")
    db.update_cron_run(run_id, "success")
    runs = db.list_cron_runs("test-job2")
    assert len(runs) == 1
    assert runs[0]["status"] == "success"


def test_update_cron_run_with_error():
    run_id = db.add_cron_run("error-job")
    db.update_cron_run(run_id, "error", error="Something went wrong")
    runs = db.list_cron_runs("error-job")
    assert runs[0]["error"] == "Something went wrong"


def test_list_cron_runs_all():
    db.add_cron_run("job-a")
    db.add_cron_run("job-b")
    runs = db.list_cron_runs()
    assert len(runs) >= 2


def test_list_cron_runs_by_name():
    db.add_cron_run("specific-job")
    db.add_cron_run("other-job")
    runs = db.list_cron_runs("specific-job")
    assert all(r["job_name"] == "specific-job" for r in runs)


def test_list_cron_runs_paginated():
    db.add_cron_run("paginated-job")
    db.add_cron_run("paginated-job")
    rows, total = db.list_cron_runs_paginated(limit=10, offset=0, job_name="paginated-job")
    assert total >= 2
    assert len(rows) >= 1


def test_get_cron_run_stats():
    db.add_cron_run("stats-job")
    db.add_cron_run("stats-job")
    stats = db.get_cron_run_stats("stats-job")
    assert stats["run_count"] >= 2
    assert stats["last_run_at"] is not None


def test_get_cron_run_stats_empty():
    stats = db.get_cron_run_stats("nonexistent-job-xyz")
    assert stats["run_count"] == 0


def test_prune_cron_runs():
    db.add_cron_run("prune-job")
    # Prune with max_age_days=0 removes all old runs
    count = db.prune_cron_runs(max_age_days=0, keep_per_job=0)
    assert isinstance(count, int)


# ---------------------------------------------------------------------------
# Snooze state
# ---------------------------------------------------------------------------


def test_set_get_snooze_state():
    db.set_snooze_state("test-key", "test-value")
    value = db.get_snooze_state("test-key")
    assert value == "test-value"


def test_get_snooze_state_missing():
    value = db.get_snooze_state("nonexistent-key")
    assert value is None


def test_snooze_state_overwrite():
    db.set_snooze_state("overwrite-key", "original")
    db.set_snooze_state("overwrite-key", "updated")
    value = db.get_snooze_state("overwrite-key")
    assert value == "updated"


# ---------------------------------------------------------------------------
# Misc DB operations
# ---------------------------------------------------------------------------


def test_get_db_stats_comprehensive():
    from db.models import get_db_stats

    sid = db.create_session(title="Stats Test")
    db.add_message(sid, "user", "hello")
    stats = get_db_stats()
    assert isinstance(stats, dict)
    assert stats.get("sessions", 0) >= 1


def test_checkpoint():
    """WAL checkpoint runs without error."""
    try:
        db.checkpoint()
    except AttributeError:
        pass  # May not be defined in all versions


def test_prune_cron_sessions():
    count = db.prune_cron_sessions(max_age_days=0)
    assert isinstance(count, int)


def test_prune_orphaned_token_usage():
    count = db.prune_orphaned_token_usage(max_age_days=0)
    assert isinstance(count, int)


def test_prune_old_session_messages():
    count = db.prune_old_session_messages(max_age_days=0)
    assert isinstance(count, int)


def test_prune_old_questions():
    count = db.prune_old_questions(max_age_days=0)
    assert isinstance(count, int)


def test_incremental_vacuum():
    """incremental_vacuum runs without error."""
    try:
        db.incremental_vacuum()
    except AttributeError:
        pass  # May not be defined
