"""One hand-created memory file could stop the whole memory subsystem.

reindex registered `md_path.stem` without validating it, so a file called
"my notes.md" (the docs invite hand edits) entered the index under a name
the store refuses to open. Every sweep that then called read_file(name) on
it raised ValueError — and before the snooze ladder was guarded, that
ended the entire cycle, every cycle. A single non-UTF-8 byte did the same
via UnicodeDecodeError, in both reindex and the 6-hourly health check.
"""

import pytest

from core.memory.store import MemoryStore


@pytest.fixture
def store(tmp_path):
    return MemoryStore(str(tmp_path / "memories"))


def test_a_file_with_spaces_in_its_name_is_skipped_not_indexed(store):
    store.add_entry("a real memory", file_name="pernix.notes")
    (store._dir / "my notes.md").write_text("<!-- @epoch: 1 -->\nhand written\n")

    store.reindex()
    names = {f.name for f in store.list_files()}
    assert "pernix.notes" in names
    assert "my notes" not in names, "the index must not name a file the store cannot open"


def test_the_rest_of_the_corpus_still_indexes(store):
    for i in range(3):
        store.add_entry(f"memory {i}", file_name=f"pernix.file{i}")
    (store._dir / "bad name here.md").write_text("junk\n")

    assert store.reindex() >= 3
    assert store.health_check()["in_sync"]


def test_a_non_utf8_byte_does_not_take_the_reindex_down(store):
    store.add_entry("a real memory", file_name="pernix.notes")
    (store._dir / "pernix.legacy.md").write_bytes(b"<!-- @epoch: 1 -->\nca\xf9 latin-1 byte\n")

    count = store.reindex()  # must not raise
    assert count >= 1
    store.health_check()  # nor here


def test_health_check_ignores_unindexable_files_rather_than_reporting_drift(store):
    store.add_entry("a real memory", file_name="pernix.notes")
    (store._dir / "not a name.md").write_text("<!-- @epoch: 5 -->\nstray\n")
    store.reindex()
    assert store.health_check()["in_sync"], "an unindexable file must not read as index drift"
