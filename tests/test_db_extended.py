"""Extended tests for db/models.py: enriched queries, notifications, FTS, etc."""

import pytest

from db import models as db

# ---------------------------------------------------------------------------
# list_sessions_enriched
# ---------------------------------------------------------------------------


def test_list_sessions_enriched_empty():
    result = db.list_sessions_enriched()
    assert result == []


def test_list_sessions_enriched_with_data():
    sid = db.create_session(title="Enriched Test")
    db.add_message(sid, "user", "Hello world")
    db.add_message(sid, "assistant", "Hi there")
    db.add_token_usage(sid, model="test", total_tokens=100)

    result = db.list_sessions_enriched()
    assert len(result) == 1
    assert result[0]["message_count"] == 2
    assert result[0]["total_tokens"] == 100
    assert "Hello" in result[0]["first_message"]


def test_list_sessions_enriched_no_messages():
    sid = db.create_session(title="Empty Session")
    result = db.list_sessions_enriched()
    assert len(result) == 1
    assert result[0]["message_count"] == 0
    assert result[0]["total_tokens"] == 0


# ---------------------------------------------------------------------------
# update_session
# ---------------------------------------------------------------------------


def test_update_session():
    sid = db.create_session(title="Original")
    db.update_session(sid, title="Updated")
    s = db.get_session(sid)
    assert s["title"] == "Updated"


def test_update_session_ignores_unknown():
    sid = db.create_session(title="Test")
    db.update_session(sid, unknown_field="value")
    s = db.get_session(sid)
    assert s["title"] == "Test"


# ---------------------------------------------------------------------------
# Message operations
# ---------------------------------------------------------------------------


def test_delete_message():
    sid = db.create_session()
    mid = db.add_message(sid, "user", "to delete")
    db.delete_message(mid)
    assert db.get_message(mid) is None


def test_delete_messages_from():
    sid = db.create_session()
    id1 = db.add_message(sid, "user", "keep")
    id2 = db.add_message(sid, "assistant", "delete1")
    id3 = db.add_message(sid, "user", "delete2")
    db.delete_messages_from(sid, id2)
    msgs = db.get_messages(sid)
    assert len(msgs) == 1
    assert msgs[0]["id"] == id1


def test_get_last_partial():
    sid = db.create_session()
    db.add_message(sid, "assistant", "normal")
    mid = db.add_message(sid, "assistant", "partial", partial=1)
    result = db.get_last_partial(sid)
    assert result is not None
    assert result["id"] == mid


def test_get_last_partial_none():
    sid = db.create_session()
    db.add_message(sid, "assistant", "normal")
    result = db.get_last_partial(sid)
    assert result is None


def test_add_message_idempotency():
    sid = db.create_session()
    db.add_message(sid, "user", "first", idempotency_key="key1")
    db.add_message(sid, "user", "second", idempotency_key="key2")
    msgs = db.get_messages(sid)
    assert len(msgs) == 2


def test_add_message_with_metadata():
    sid = db.create_session()
    mid = db.add_message(sid, "assistant", "hello", metadata='{"extra": "data"}')
    msg = db.get_message(mid)
    assert msg["metadata"] == '{"extra": "data"}'


def test_clear_messages_only():
    sid = db.create_session()
    db.add_message(sid, "user", "hello")
    db.add_message(sid, "assistant", "world")
    db.clear_messages_only(sid)
    msgs = db.get_messages(sid)
    assert len(msgs) == 0
    # Session should still exist
    s = db.get_session(sid)
    assert s is not None


# ---------------------------------------------------------------------------
# FTS search
# ---------------------------------------------------------------------------


def test_search_messages_fts():
    sid = db.create_session(title="FTS Test")
    db.add_message(sid, "user", "How to configure the postgres database connection")
    db.add_message(sid, "assistant", "Use the DATABASE_URL environment variable to set the connection string")
    results = db.search_messages_fts("database connection")
    assert len(results) >= 1


def test_search_messages_fts_empty_query():
    result = db.search_messages_fts("")
    assert result == []


def test_search_messages_fts_exclude_session():
    sid1 = db.create_session(title="Session 1")
    sid2 = db.create_session(title="Session 2")
    db.add_message(sid1, "user", "unique_marker_alpha database")
    db.add_message(sid2, "user", "unique_marker_alpha database")
    results = db.search_messages_fts("unique_marker_alpha", exclude_session=sid1)
    # Should only find results from sid2
    for r in results:
        assert r["session_id"] != sid1


def test_get_message_context():
    sid = db.create_session()
    ids = []
    for i in range(5):
        ids.append(db.add_message(sid, "user" if i % 2 == 0 else "assistant", f"msg {i}"))
    ctx = db.get_message_context(sid, ids[2], window=1)
    assert len(ctx) >= 2  # At least the target and one neighbor


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


def test_add_get_notifications():
    nid = db.add_notification(session_id="s1", title="Test", body="Hello")
    notifications = db.get_notifications()
    assert len(notifications) >= 1
    found = [n for n in notifications if n["id"] == nid]
    assert len(found) == 1
    assert found[0]["title"] == "Test"


def test_delete_notification():
    nid = db.add_notification(title="Delete Me", body="gone")
    db.delete_notification(nid)
    notifications = db.get_notifications()
    assert all(n["id"] != nid for n in notifications)


# ---------------------------------------------------------------------------
# Worker sessions
# ---------------------------------------------------------------------------


def test_get_worker_sessions():
    parent = db.create_session(title="Parent")
    child1 = db.create_session(title="Worker 1", parent_session_id=parent)
    child2 = db.create_session(title="Worker 2", parent_session_id=parent)
    workers = db.get_worker_sessions(parent)
    assert len(workers) == 2


# ---------------------------------------------------------------------------
# Token usage
# ---------------------------------------------------------------------------


def test_get_session_usage():
    sid = db.create_session()
    db.add_token_usage(sid, model="m1", prompt_tokens=100, completion_tokens=50, total_tokens=150)
    db.add_token_usage(sid, model="m1", prompt_tokens=200, completion_tokens=100, total_tokens=300)
    usage = db.get_session_usage(sid)
    assert usage["prompt"] == 300
    assert usage["completion"] == 150
    assert usage["total"] == 450
    assert usage["calls"] == 2


# ---------------------------------------------------------------------------
# Workflow run orphan sweep (Fix 3 from session 7b97cf7ef84a investigation)
# ---------------------------------------------------------------------------


def test_fail_orphaned_workflow_runs_marks_running_rows_failed():
    """Rows stuck at status='running' across a process restart must be
    swept to 'failed' at startup. run_workflow is in-process with no resume
    path, so a 'running' row that survives a restart is by definition dead.
    """
    db.create_workflow_run(run_id="orphan01", workflow_name="wf-a", run_dir="x/orphan01", step_count=3)
    db.create_workflow_run(run_id="orphan02", workflow_name="wf-b", run_dir="x/orphan02", step_count=1)
    # One that already finished — must NOT be touched.
    db.create_workflow_run(run_id="alive03", workflow_name="wf-c", run_dir="x/alive03", step_count=1)
    db.finish_workflow_run("alive03", "complete", 1, 0, 0)

    swept = db.fail_orphaned_workflow_runs()
    assert swept == 2

    a = db.get_workflow_run("orphan01")
    b = db.get_workflow_run("orphan02")
    c = db.get_workflow_run("alive03")

    assert a["status"] == "failed" and a["completed_at"] is not None
    assert b["status"] == "failed" and b["completed_at"] is not None
    # Already-complete runs must be left alone.
    assert c["status"] == "complete"

    # Idempotent: running it again with no orphans returns 0.
    assert db.fail_orphaned_workflow_runs() == 0
