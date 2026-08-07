"""Tests for core/memory/sweeps.py — the memory-store surgery snooze schedules.

Ported from tests/test_snooze.py and tests/test_snooze_deep.py when these
helpers moved out of SnoozeRunner. Snooze's own tests still cover the
delegates; these cover the seams directly.
"""

import time

import pytest

from core.memory import sweeps


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from core.memory.store import MemoryStore

    return MemoryStore(str(tmp_path / "memories"))


# ---------------------------------------------------------------------------
# extract_tags (pure logic)
# ---------------------------------------------------------------------------


def test_extract_tags_technical_terms():
    content = "Use file_read and file_write tools in the agent_loop context"
    tags = sweeps.extract_tags(content, [])
    assert any("file" in t or "agent" in t or "_" in t for t in tags)


def test_extract_tags_proper_nouns():
    content = "The SQLite database and PostgreSQL adapter both support WAL mode"
    assert isinstance(sweeps.extract_tags(content, []), list)


def test_extract_tags_dedup_existing():
    existing = ["database", "config"]
    tags = sweeps.extract_tags("database config settings", existing)
    existing_lower = {t.lower() for t in existing}
    for t in tags:
        assert t.lower() not in existing_lower


def test_extract_tags_max_five():
    content = "file_read file_write file_edit file_search file_list file_delete file_move file_copy"
    assert len(sweeps.extract_tags(content, [])) <= 5


# ---------------------------------------------------------------------------
# archive_entries: markdown tag + FTS removal
# ---------------------------------------------------------------------------


def test_archive_entries_in_file(store):
    """The markdown half adds the archived tag to the entry."""
    epoch = int(time.time())
    store.add_entry("Content to archive", file_name="pernix.notes", epoch=epoch)

    sweeps._archive_entries_in_file(store, "pernix.notes", {epoch})

    content = store.read_file("pernix.notes")
    assert content is not None
    assert "@archived" in content


def test_remove_from_index(store):
    """The index half drops the entry from FTS5."""
    epoch = int(time.time())
    store.add_entry("Content to remove from index", file_name="pernix.notes", epoch=epoch)

    sweeps._remove_from_index(store, "pernix.notes", {epoch})

    conn = store._connect()
    try:
        rows = conn.execute(
            "SELECT 1 FROM memory_fts WHERE file_name = ? AND epoch = ?", ("pernix.notes", str(epoch))
        ).fetchall()
    finally:
        conn.close()
    assert rows == []


def test_archive_entries_pairs_both_halves(store):
    """archive_entries leaves markdown and index agreeing — the invariant the
    split call sites used to break under cancellation."""
    epoch = int(time.time())
    store.add_entry("Both halves", file_name="pernix.notes", epoch=epoch)

    sweeps.archive_entries(store, "pernix.notes", {epoch})

    assert "@archived" in (store.read_file("pernix.notes") or "")
    conn = store._connect()
    try:
        rows = conn.execute(
            "SELECT 1 FROM memory_fts WHERE file_name = ? AND epoch = ?", ("pernix.notes", str(epoch))
        ).fetchall()
    finally:
        conn.close()
    assert rows == []


# ---------------------------------------------------------------------------
# update_tags_in_markdown
# ---------------------------------------------------------------------------


def test_update_tags_in_markdown(store):
    epoch = int(time.time())
    store.add_entry("Entry without enriched tags", file_name="pernix.notes", epoch=epoch)

    sweeps.update_tags_in_markdown(store, "pernix.notes", epoch, ["new_tag", "enriched"])

    content = store.read_file("pernix.notes")
    assert content is not None
    assert "new_tag" in content


def test_update_tags_in_markdown_missing_file(store):
    """Missing file is a no-op, not a raise."""
    sweeps.update_tags_in_markdown(store, "nonexistent.file", 12345, ["tag"])


# ---------------------------------------------------------------------------
# reroute scoring seams (flattened out of the old nested _scan_for_candidates)
# ---------------------------------------------------------------------------


class _Entry:
    def __init__(self, epoch, content, tags=(), entry_type="note"):
        self.epoch = epoch
        self.content = content
        self.tags = list(tags)
        self.entry_type = entry_type


def test_classify_entry_flags_profile_outside_user_profile():
    entry = _Entry(1, "The user lives in Denver", entry_type="profile")
    out = sweeps.classify_entry(entry, "pernix.notes", {"pernix.notes": set()})
    assert out is not None
    assert out["confidence"] == "high"
    assert out["target_file"] == "user.profile"


def test_classify_entry_keeps_profile_already_in_user_profile():
    entry = _Entry(1, "The user lives in Denver", entry_type="profile")
    assert sweeps.classify_entry(entry, "user.profile", {"user.profile": set()}) is None


def test_classify_entry_medium_confidence_on_zero_affinity():
    entry = _Entry(1, "kubernetes cluster autoscaling", tags=["kubernetes"])
    keywords = {"pernix.notes": {"snooze"}, "ops.infra": {"kubernetes"}}
    out = sweeps.classify_entry(entry, "pernix.notes", keywords)
    assert out is not None
    assert out["confidence"] == "medium"
    assert out["target_file"] == "ops.infra"


def test_classify_entry_keeps_when_current_file_scores():
    entry = _Entry(1, "kubernetes cluster autoscaling", tags=["kubernetes"])
    keywords = {"pernix.notes": {"kubernetes"}, "ops.infra": {"kubernetes"}}
    assert sweeps.classify_entry(entry, "pernix.notes", keywords) is None


# ---------------------------------------------------------------------------
# stale-prune cohort math
# ---------------------------------------------------------------------------


def _row(epoch, hits, weight="normal"):
    return {"file_name": "f", "epoch": str(epoch), "weight": weight, "content": "c", "hit_count": hits}


def test_stale_candidates_skips_young_and_high_weight():
    now = int(time.time())
    day = 86400
    rows = [_row(now - 5 * day, 0) for _ in range(5)]  # all < 30d
    assert sweeps._stale_candidates(rows, now) == []

    old = now - 100 * day
    rows = [_row(old, 0, weight="high"), _row(old, 9), _row(old, 9), _row(old, 9)]
    assert all(c["weight"] != "high" for c in sweeps._stale_candidates(rows, now))


def test_stale_candidates_picks_below_cohort_average():
    now = int(time.time())
    old = now - 100 * 86400  # 90d cohort
    rows = [_row(old, 0), _row(old, 10), _row(old, 10)]
    out = sweeps._stale_candidates(rows, now)
    assert [c["hit_count"] for c in out] == [0]
    assert out[0]["cohort"] == "90d"


def test_stale_candidates_needs_three_per_cohort():
    now = int(time.time())
    old = now - 100 * 86400
    assert sweeps._stale_candidates([_row(old, 0), _row(old, 10)], now) == []
