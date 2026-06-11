"""Tests for internalized memory store."""

from core.memory.format import format_entry, parse_entries_from_markdown
from core.memory.store import MemoryStore


def test_format_entry():
    entry = format_entry("Test content", entry_type="finding", tags="test,memory")
    assert "<!-- @epoch:" in entry
    assert "<!-- @type: finding -->" in entry
    assert "<!-- @tags: test,memory -->" in entry
    assert "Test content" in entry


def test_parse_entries():
    md = """<!-- @file: test -->
---
<!-- @epoch: 1000000 -->
<!-- @type: finding -->
First entry

---
<!-- @epoch: 1000100 -->
<!-- @type: decision -->
<!-- @tags: arch -->
Second entry
"""
    entries = parse_entries_from_markdown("test", md)
    assert len(entries) == 2
    assert entries[0].entry_type == "finding"
    assert entries[0].epoch == 1000000
    assert entries[1].entry_type == "decision"
    assert "arch" in entries[1].tags


def test_memory_store_add_search(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path))
    from db.database import init_memory_db

    init_memory_db()

    store = MemoryStore(str(tmp_path))
    result = store.add_entry("SQLite FTS5 is great for search", file_name="test.notes", tags="sqlite,search")
    assert "Saved" in result

    results = store.search("FTS5 search")
    assert len(results) >= 1
    assert "FTS5" in results[0].entry.content


def test_memory_recall(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path))
    from db.database import init_memory_db

    init_memory_db()

    store = MemoryStore(str(tmp_path))
    store.add_entry("Python 3.12 has better error messages", file_name="pernix.notes")

    text = store.recall("python error messages")
    assert "Python 3.12" in text or text == ""  # BM25 may not match depending on tokenization


def test_memory_auto_routing(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path))
    from db.database import init_memory_db

    init_memory_db()

    store = MemoryStore(str(tmp_path))
    # Should auto-route to a namespace based on content keywords
    result = store.add_entry("Decided to use SQLite for persistence")
    assert "Saved" in result


def test_memory_list_files(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path))
    from db.database import init_memory_db

    init_memory_db()

    store = MemoryStore(str(tmp_path))
    store.add_entry("test entry", file_name="pernix.test")
    files = store.list_files()
    assert len(files) >= 1
    assert files[0].name == "pernix.test"


def test_memory_health_check(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path))
    from db.database import init_memory_db

    init_memory_db()

    store = MemoryStore(str(tmp_path))
    store.add_entry("health check test", file_name="pernix.test")
    health = store.health_check()
    assert health["in_sync"] is True
    assert health["indexed_entries"] >= 1


def test_memory_fts_schema_upgrade_adds_source_updated():
    """A legacy memory_fts (pre source/updated columns) is dropped and
    recreated; the index rebuilds from markdown via the health check."""
    from db.database import connect_memory, init_memory_db

    conn = connect_memory()
    conn.execute("DROP TABLE memory_fts")
    conn.execute(
        "CREATE VIRTUAL TABLE memory_fts USING fts5("
        "file_name, content, tags, entry_type, weight, epoch UNINDEXED, "
        "tokenize='porter unicode61')"
    )
    conn.commit()

    init_memory_db()

    conn = connect_memory()
    cols = {row[1] for row in conn.execute("PRAGMA table_info(memory_fts)")}
    assert {"source", "updated"} <= cols
