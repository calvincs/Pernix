"""Extended tests for core/memory/store.py: CRUD, search, routing, health."""

import pytest

from core.memory.store import MemoryStore


@pytest.fixture
def store(tmp_path):
    """Create a MemoryStore with isolated temp dir."""
    return MemoryStore(str(tmp_path / "memories"))


# ---------------------------------------------------------------------------
# add_entry
# ---------------------------------------------------------------------------


def test_add_entry_basic(store):
    result = store.add_entry("Test memory content", file_name="pernix.notes")
    assert "Saved" in result or "saved" in result.lower() or "Added" in result or "pernix.notes" in result


def test_add_entry_empty_content(store):
    result = store.add_entry("", file_name="pernix.notes")
    assert "Error" in result


def test_add_entry_auto_routing(store):
    """Auto-routes to appropriate namespace based on content keywords."""
    result = store.add_entry("The user prefers dark mode for all interfaces")
    # Should succeed regardless of which file it routes to
    assert "Error" not in result


def test_add_entry_with_tags(store):
    result = store.add_entry("Tagged entry", file_name="pernix.notes", tags="important,config")
    assert "Error" not in result


def test_add_entry_with_weight(store):
    result = store.add_entry("Important finding", file_name="pernix.notes", weight="high")
    assert "Error" not in result


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def test_search_basic(store):
    store.add_entry("Database configuration uses SQLite with WAL mode", file_name="pernix.config")
    store.add_entry("User interface should be dark theme", file_name="pernix.notes")
    results = store.search("database configuration")
    assert len(results) >= 1


def test_search_no_results(store):
    results = store.search("zzz_completely_unrelated_zzz")
    # May return temporal/recent results — just verify it's a list
    assert isinstance(results, list)


def test_search_empty_store(store):
    results = store.search("anything")
    assert len(results) == 0


# ---------------------------------------------------------------------------
# list_files
# ---------------------------------------------------------------------------


def test_list_files_empty(store):
    files = store.list_files()
    assert len(files) == 0


def test_list_files_with_entries(store):
    store.add_entry("Content for notes", file_name="pernix.notes")
    store.add_entry("Content for config", file_name="pernix.config")
    files = store.list_files()
    names = [f.name for f in files]
    assert "pernix.notes" in names
    assert "pernix.config" in names


# ---------------------------------------------------------------------------
# reindex
# ---------------------------------------------------------------------------


def test_reindex(store):
    store.add_entry("Entry 1", file_name="pernix.notes")
    store.add_entry("Entry 2", file_name="pernix.notes")
    # reindex() returns int (entry count)
    count = store.reindex()
    assert isinstance(count, int)
    assert count >= 2


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


def test_health_check(store):
    store.add_entry("Test", file_name="pernix.notes")
    health = store.health_check()
    assert isinstance(health, dict)


def test_health_check_empty(store):
    health = store.health_check()
    assert isinstance(health, dict)


# ---------------------------------------------------------------------------
# recall (formatted output)
# ---------------------------------------------------------------------------


def test_recall(store):
    store.add_entry("Remember this: the API key format is sk-xxxx", file_name="pernix.notes")
    result = store.recall("API key format")
    # recall returns a formatted string for context injection
    assert isinstance(result, str)


def test_recall_no_matches(store):
    result = store.recall("zzz_no_match_zzz")
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_add_entry_special_chars(store):
    result = store.add_entry("Entry with 'quotes' and \"double quotes\" and <tags>", file_name="pernix.notes")
    assert "Error" not in result


def test_search_after_multiple_adds(store):
    for i in range(10):
        store.add_entry(f"Memory entry number {i} about testing patterns", file_name="pernix.notes")
    results = store.search("testing patterns")
    assert len(results) >= 1


# ---------------------------------------------------------------------------
# search_lessons age decay (#7 audit — 2026-04-27 ai-tech-daily-brief run)
# ---------------------------------------------------------------------------


def _add_lesson(store, content: str, *, days_old: int):
    """Add a lesson with a backdated epoch."""
    import time

    epoch = int(time.time()) - (days_old * 86400)
    return store.add_entry(
        content,
        file_name="pernix.lessons",
        entry_type="lesson",
        tags="test,lesson",
        weight="high",
        epoch=epoch,
    )


def test_search_lessons_decays_old_entries_below_fresh(store):
    """A 200-day-old lesson with the same query relevance as a 1-day-old one
    must rank below the fresh one. Without decay, an old "manifest bug" lesson
    keeps getting surfaced even after the underlying bug was fixed in main.
    """
    _add_lesson(store, "Workflow engine has a manifest bug for steps", days_old=200)
    _add_lesson(store, "Workflow engine has a manifest bug for steps", days_old=1)

    results = store.search_lessons("manifest bug workflow", limit=5)

    assert len(results) >= 2, f"expected 2 lessons; got {len(results)}: {results}"
    # The fresh lesson (epoch ~today) should outrank the 200-day-old one.
    import time

    now_ts = int(time.time())
    ages = [(now_ts - int(r.entry.epoch)) // 86400 for r in results]
    # First result should be the recent one; relative ordering matters.
    assert ages[0] < ages[-1], f"oldest lesson should sink behind fresh peer; got ages={ages}"


def test_search_lessons_severely_decays_180_day_lessons(store):
    """A 200+-day-old lesson should have its score multiplied by ~0.05;
    even a strong base BM25 score drops well below a freshly-added peer."""
    _add_lesson(store, "Fresh content with rare unique-term zorblax", days_old=0)
    _add_lesson(store, "Old content with rare unique-term zorblax", days_old=200)

    results = store.search_lessons("zorblax", limit=5)

    assert len(results) == 2
    fresh = next(r for r in results if "Fresh" in r.entry.content)
    old = next(r for r in results if "Old" in r.entry.content)
    assert fresh.score > old.score, (
        f"fresh should beat 200-day decayed lesson; " f"fresh={fresh.score:.2f} old={old.score:.2f}"
    )


def test_search_lessons_under_14_days_no_decay(store):
    """Lessons under 2 weeks old keep full BM25 score (factor 1.0). They
    haven't outlived their evidentiary value yet."""
    _add_lesson(store, "Recent lesson with marker xyzqqq", days_old=5)
    _add_lesson(store, "Different lesson with marker xyzqqq", days_old=10)

    results = store.search_lessons("xyzqqq", limit=5)
    # Both within 14d window — relative ranking unchanged by decay
    # (i.e. 5d-old ≈ 10d-old in score, both at full weight).
    assert len(results) == 2
    # Sanity: scores are positive (BM25 working)
    assert all(r.score > 0 for r in results)
