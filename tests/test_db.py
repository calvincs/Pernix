"""Tests for db/database.py and db/models.py."""

from db import models as db


def test_session_crud():
    sid = db.create_session(title="Test", session_type="normal")
    assert sid
    s = db.get_session(sid)
    assert s["title"] == "Test"
    assert s["state"] == "idle"

    db.update_session(sid, title="Updated")
    s2 = db.get_session(sid)
    assert s2["title"] == "Updated"

    db.delete_session(sid)
    assert db.get_session(sid) is None


def test_message_crud():
    sid = db.create_session()
    mid = db.add_message(sid, "user", "Hello", token_count=5)
    assert mid > 0

    msgs = db.get_messages(sid)
    assert len(msgs) == 1
    assert msgs[0]["content"] == "Hello"
    assert msgs[0]["token_count"] == 5

    db.add_message(sid, "assistant", "Hi!")
    assert len(db.get_messages(sid)) == 2

    db.delete_message(mid)
    assert len(db.get_messages(sid)) == 1


def test_cascade_delete():
    sid = db.create_session()
    db.add_message(sid, "user", "test")
    db.add_message(sid, "assistant", "response")
    assert len(db.get_messages(sid)) == 2

    db.delete_session(sid)
    assert len(db.get_messages(sid)) == 0


def test_delete_session_cleans_messages_fts():
    """delete_session must remove FTS rows — ON DELETE CASCADE never touches
    the FTS table, so without the explicit delete the index leaks forever."""
    from db.database import connect_sessions

    sid = db.create_session()
    db.add_message(sid, "user", "searchable zanzibar content")
    db.add_message(sid, "assistant", "more zanzibar text here")

    with connect_sessions() as conn:
        before = conn.execute("SELECT COUNT(*) c FROM messages_fts WHERE session_id = ?", (sid,)).fetchone()["c"]
    assert before == 2

    db.delete_session(sid)

    with connect_sessions() as conn:
        after = conn.execute("SELECT COUNT(*) c FROM messages_fts WHERE session_id = ?", (sid,)).fetchone()["c"]
    assert after == 0, "messages_fts rows must be deleted with the session"


def test_worker_session_delete():
    parent = db.create_session(session_type="normal")
    worker = db.create_session(session_type="worker", parent_session_id=parent)
    db.add_message(worker, "user", "task")

    db.delete_session(parent)
    assert db.get_session(parent) is None
    assert db.get_session(worker) is None


def test_token_usage():
    sid = db.create_session()
    db.add_token_usage(
        sid,
        model="test",
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
        source="provider",
        provider="ollama",
    )
    usage = db.get_session_usage(sid)
    assert usage["total"] == 150
    assert usage["calls"] == 1


def test_questions():
    sid = db.create_session()
    qid = db.add_question(sid, "What color?", session_title="Test")
    qs = db.get_questions()
    assert len(qs) >= 1
    assert qs[0]["question"] == "What color?"

    db.delete_question(qid)
    assert len(db.get_questions(sid)) == 0


def test_compaction():
    sid = db.create_session()
    db.add_message(sid, "user", "hello")
    db.add_compaction(sid, "Summary text", compacted_up_to=1, original_count=5)
    msgs = db.get_messages(sid)
    compactions = [m for m in msgs if m["role"] == "compaction"]
    assert len(compactions) == 1
    assert "Summary text" in compactions[0]["content"]


def test_failed_migration_rolls_back_atomically(monkeypatch):
    """A migration that dies mid-way must leave no partial DDL behind and
    must not bump the schema version — otherwise the re-run on next boot
    hits 'duplicate column name' and the server refuses to start."""
    import db.database as dbase
    from db.database import connect_sessions

    conn = connect_sessions()
    try:
        before_version = dbase._get_schema_version(conn)
        fake = [
            (
                before_version + 1,
                "test migration that fails after DDL",
                [
                    "ALTER TABLE sessions ADD COLUMN _mig_test_col TEXT",
                    "THIS IS NOT VALID SQL",
                ],
            ),
        ]
        monkeypatch.setattr(dbase, "MIGRATIONS", fake)

        try:
            dbase._run_migrations(conn)
            raise AssertionError("migration should have raised")
        except Exception:
            pass

        assert dbase._get_schema_version(conn) == before_version
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()]
        assert "_mig_test_col" not in cols, "partial DDL must be rolled back"
    finally:
        conn.close()


def test_schema_version():
    v = db.get_schema_version()
    assert v >= 1


def test_db_stats():
    db.create_session()
    stats = db.get_db_stats()
    assert "sessions" in stats
    assert stats["sessions"] >= 1
    assert "db_size_bytes" in stats
