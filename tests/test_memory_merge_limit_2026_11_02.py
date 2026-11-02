"""The memory merge reply limit, and what a fuse does to the original entry.

At max_tokens=2000 most merge replies were cut off mid-JSON and thrown away.
Raising the limit means fuses happen for real, and fused text is model-written
with no fact check, so the target-file original must stay recoverable.
"""

import inspect

import pytest

from core.memory import sweeps
from core.memory.consolidate import LLM_MERGE_MAX_TOKENS, MergeDecision, execute_merge
from core.memory.format import parse_entries_from_markdown
from core.memory.store import MemoryStore

ORIGINAL = "Server A hosts the production database on port 5432 for the main app."
FUSED = "Server A hosts production on 5432 and staging on 5433 for the main app."


@pytest.fixture
def store(tmp_path):
    return MemoryStore(str(tmp_path / "memories"))


def _fuse(store):
    store.add_entry(ORIGINAL, file_name="tgt.file", entry_type="finding", epoch=100)
    store.add_entry(
        "Server A also hosts the staging database on port 5433 for testing.",
        file_name="src.file",
        entry_type="finding",
        epoch=200,
        skip_dedup=True,
    )
    return execute_merge(
        store,
        MergeDecision(
            target_file="tgt.file",
            source_files=["src.file"],
            strategy="llm",
            entries_to_keep=[],
            entries_to_archive=[],
            fused_entries=[{"file": "src.file", "epoch": 200, "fuse_target_epoch": 100, "fused_content": FUSED}],
            target_description="d",
            reason="test",
        ),
    )


def test_reply_limit_fits_a_real_merge_verdict():
    assert LLM_MERGE_MAX_TOKENS >= 7000


def test_sweep_asks_for_the_same_limit_the_budget_reserves():
    source = inspect.getsource(sweeps)
    assert "max_tokens=LLM_MERGE_MAX_TOKENS" in source
    assert "max_tokens=2000,\n                )\n                decision = parse_llm_merge_response" not in source


def test_fuse_keeps_the_target_original_in_the_file(store):
    assert _fuse(store)["entries_fused"] == 1
    raw = store.read_file("tgt.file")
    assert ORIGINAL in raw
    assert "<!-- @epoch: 100 -->\n<!-- @archived: true -->" in raw


def test_archived_original_stays_out_of_live_entries_and_reindex(store):
    _fuse(store)
    live = parse_entries_from_markdown("tgt.file", store.read_file("tgt.file"))
    assert [e.content for e in live] == [FUSED]

    store.reindex()
    conn = store._connect()
    try:
        rows = conn.execute("SELECT content FROM memory_fts WHERE file_name = 'tgt.file'").fetchall()
    finally:
        conn.close()
    assert [r[0] for r in rows] == [FUSED]


def test_fuse_keeps_the_entry_count_right(store):
    _fuse(store)
    conn = store._connect()
    try:
        count = conn.execute("SELECT entry_count FROM memory_files WHERE name = 'tgt.file'").fetchone()[0]
    finally:
        conn.close()
    assert count == 1


def test_archive_file_survives_per_entry_archive_tags(store):
    """Real source files hold per-entry tags; they must not read as archived."""
    from core.memory.format import is_file_archived
    from core.memory.sweeps import archive_entries

    store.add_entry("An old fact about the east coast rail schedule.", file_name="src.file", epoch=100)
    store.add_entry("A live fact about the postal sorting office hours.", file_name="src.file", epoch=200)
    archive_entries(store, "src.file", {100})

    store.archive_file("src.file")

    assert is_file_archived(store.read_file("src.file"))
    assert parse_entries_from_markdown("src.file", store.read_file("src.file")) == []
    store.reindex()
    conn = store._connect()
    try:
        count = conn.execute("SELECT count(*) FROM memory_fts WHERE file_name = 'src.file'").fetchone()[0]
    finally:
        conn.close()
    assert count == 0


def test_merge_rung_outlasts_a_real_merge():
    from core.snooze import RUNG_TIMEOUTS

    assert RUNG_TIMEOUTS["consolidate_files"] >= 180
