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
# Archive (file-level) — archived files must stay out of the index
# ---------------------------------------------------------------------------


def test_archived_file_parses_as_empty(store):
    from core.memory.format import is_file_archived, parse_entries_from_markdown

    store.add_entry("Chromecast pairing steps", file_name="pernix.casting")
    store.archive_file("pernix.casting")
    raw = store.read_file("pernix.casting")
    assert is_file_archived(raw)
    assert parse_entries_from_markdown("pernix.casting", raw) == []


def test_per_entry_archive_does_not_archive_file(store):
    """Snooze dedup marks individual entries archived — file stays live."""
    from core.memory.format import is_file_archived, parse_entries_from_markdown

    store.add_entry("Keep this entry", file_name="pernix.notes")
    store.add_entry("Archive this one", file_name="pernix.notes")
    raw = store.read_file("pernix.notes")
    raw = raw.replace(
        "Archive this one",
        "<!-- @archived: true -->\nArchive this one",
    )
    assert not is_file_archived(raw)
    entries = parse_entries_from_markdown("pernix.notes", raw)
    assert [e.content for e in entries] == ["Keep this entry"]


def test_health_check_in_sync_after_archive(store):
    """Archiving must not leave the index permanently 'out of sync' —
    that mismatch made every startup health check reindex and resurrect."""
    store.add_entry("Entry one", file_name="pernix.notes")
    store.add_entry("Old workflow doc", file_name="pernix.old_stuff")
    store.archive_file("pernix.old_stuff")
    health = store.health_check()
    assert health["in_sync"] is True


def test_reindex_does_not_resurrect_archived_file(store):
    store.add_entry("Live entry about sqlite tuning", file_name="pernix.notes")
    store.add_entry("Dead entry about zorbofloop quux", file_name="pernix.old_stuff")
    store.archive_file("pernix.old_stuff")

    store.reindex()

    results = store.search("zorbofloop quux")
    assert all(r.entry.file_name != "pernix.old_stuff" for r in results)
    counts = {f.name: f.entry_count for f in store.list_files()}
    assert counts.get("pernix.old_stuff", 0) == 0
    assert counts["pernix.notes"] == 1


def test_add_entry_revives_archived_file(store):
    from core.memory.format import is_file_archived

    store.add_entry("Original casting note", file_name="pernix.casting")
    store.archive_file("pernix.casting")

    result = store.add_entry("New casting note", file_name="pernix.casting")
    assert "Error" not in result

    raw = store.read_file("pernix.casting")
    assert not is_file_archived(raw)
    # Both the prior entry and the new one are live and indexed again.
    counts = {f.name: f.entry_count for f in store.list_files()}
    assert counts["pernix.casting"] == 2
    assert store.health_check()["in_sync"] is True


# ---------------------------------------------------------------------------
# Epoch identity — (file, epoch) must be unique
# ---------------------------------------------------------------------------


def test_add_entry_same_second_gets_unique_epochs(store):
    """Two writes in the same epoch second must not share identity."""
    fixed = 1700000000
    store.add_entry("First fact", file_name="pernix.notes", epoch=fixed)
    store.add_entry("Second fact", file_name="pernix.notes", epoch=fixed)

    from core.memory.format import parse_entries_from_markdown

    entries = parse_entries_from_markdown("pernix.notes", store.read_file("pernix.notes"))
    epochs = [e.epoch for e in entries]
    assert len(epochs) == len(set(epochs)) == 2
    assert fixed in epochs and fixed + 1 in epochs


def test_update_entry_refuses_ambiguous_epoch(store):
    """Legacy files can hold colliding epochs — update must not rewrite both."""
    store.add_entry("Entry A", file_name="pernix.notes", epoch=1700000000)
    # Forge a legacy collision directly in the markdown (bypassing add_entry).
    md = store._dir / "pernix.notes.md"
    md.write_text(md.read_text() + "\n---\n<!-- @epoch: 1700000000 -->\n<!-- @type: note -->\nEntry B (collided)\n")

    result = store.update_entry("pernix.notes", 1700000000, "Replacement")
    assert "Error" in result and "share epoch" in result

    result = store.delete_entry("pernix.notes", 1700000000)
    assert "Error" in result and "share epoch" in result


def test_health_check_repairs_epoch_collisions(store):
    store.add_entry("Entry A", file_name="pernix.notes", epoch=1700000000)
    md = store._dir / "pernix.notes.md"
    md.write_text(md.read_text() + "\n---\n<!-- @epoch: 1700000000 -->\n<!-- @type: note -->\nEntry B (collided)\n")

    health = store.health_check()
    assert health["epoch_collisions"] == 1

    health = store.health_check(fix=True)
    assert health["repaired_epoch_collisions"] == 1
    assert health["epoch_collisions"] == 0

    from core.memory.format import parse_entries_from_markdown

    entries = parse_entries_from_markdown("pernix.notes", store.read_file("pernix.notes"))
    epochs = sorted(e.epoch for e in entries)
    assert epochs == [1700000000, 1700000001]
    # Each entry is individually addressable again.
    assert "Error" not in store.update_entry("pernix.notes", 1700000001, "Entry B repaired")


# ---------------------------------------------------------------------------
# Separator sanitization — bare `---` lines must not split entries
# ---------------------------------------------------------------------------


def test_add_entry_with_horizontal_rule_survives_reindex(store):
    from core.memory.format import parse_entries_from_markdown

    store.add_entry("Steps:\n1. do one\n---\n2. do two after the rule", file_name="pernix.notes")
    store.add_entry("Plain second entry", file_name="pernix.notes")

    entries = parse_entries_from_markdown("pernix.notes", store.read_file("pernix.notes"))
    assert len(entries) == 2
    assert "do two after the rule" in entries[0].content

    # Without sanitization the fragment after `---` was epoch-less and
    # silently dropped here.
    assert store.reindex() == 2
    assert store.health_check()["in_sync"] is True


def test_update_entry_with_horizontal_rule(store):
    from core.memory.format import parse_entries_from_markdown

    store.add_entry("Original entry", file_name="pernix.notes", epoch=1700000000)
    result = store.update_entry("pernix.notes", 1700000000, "New intro\n---\nnew outro")
    assert "Error" not in result

    entries = parse_entries_from_markdown("pernix.notes", store.read_file("pernix.notes"))
    assert len(entries) == 1
    assert "new outro" in entries[0].content


# ---------------------------------------------------------------------------
# Dedup supersede hint
# ---------------------------------------------------------------------------


def test_duplicate_skip_points_at_existing_entry(store):
    """A skipped duplicate must reference the matched entry so a newer or
    corrected fact can supersede the stale one via update_memory instead of
    being silently discarded (first-writer-wins)."""
    original = "The staging server runs on port 8090 with TLS enabled and auto-restart configured."
    store.add_entry(original, file_name="pernix.config", epoch=1700000000)

    updated = "The staging server runs on port 8091 with TLS enabled and auto-restart configured."
    result = store.add_entry(updated, file_name="pernix.config")

    assert "skipped" in result
    assert "pernix.config@1700000000" in result
    assert "update_memory" in result
    assert "port 8090" in result  # preview of the existing entry


def test_find_duplicate_returns_none_for_novel_content(store):
    store.add_entry(
        "The staging server runs on port 8090 with TLS enabled and auto-restart configured.",
        file_name="pernix.config",
    )
    assert store.find_duplicate("Completely unrelated fact about chromecast pairing on the LAN.") is None


# ---------------------------------------------------------------------------
# Hit tracking — automated paths must not inflate usage counts
# ---------------------------------------------------------------------------


def _hit_count(store, file_name: str) -> int:
    conn = store._connect()
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(hit_count), 0) AS total FROM memory_hits WHERE file_name = ?",
            (file_name,),
        ).fetchone()
        return row["total"]
    finally:
        conn.close()


def test_search_with_track_hits_false_records_nothing(store):
    store.add_entry("Chromecast pairing uses mDNS discovery", file_name="pernix.casting")

    store.search("chromecast pairing", _track_hits=False)
    assert _hit_count(store, "pernix.casting") == 0

    store.search("chromecast pairing")
    assert _hit_count(store, "pernix.casting") > 0


def test_search_lessons_with_track_hits_false_records_nothing(store):
    store.add_entry(
        "Lesson: yt-dlp needs cookies for age-gated videos",
        file_name="pernix.lessons",
        entry_type="lesson",
    )

    store.search_lessons("yt-dlp cookies", _track_hits=False)
    assert _hit_count(store, "pernix.lessons") == 0

    store.search_lessons("yt-dlp cookies")
    assert _hit_count(store, "pernix.lessons") > 0


# ---------------------------------------------------------------------------
# Age + provenance in recall output
# ---------------------------------------------------------------------------


def test_search_results_carry_source_and_format_shows_provenance(store):
    from core.memory.search import format_result_line

    store.add_entry(
        "The user prefers terse weekly summaries",
        file_name="user.profile",
        epoch=1700000000,
        source="distill",
    )
    results = store.search("terse weekly summaries", _track_hits=False)
    r = next(res for res in results if res.entry.file_name == "user.profile")
    assert r.entry.source == "distill"

    line = format_result_line(r)
    assert "source=distill" in line
    assert "date=2023-11-1" in line  # epoch 1700000000 → 2023-11-14/15 (tz-dependent)
    assert "epoch=1700000000" in line


def test_update_entry_stamps_updated_and_recall_shows_it(store):
    from core.memory.format import parse_entries_from_markdown
    from core.memory.search import format_result_line

    store.add_entry("Server port is 8090", file_name="pernix.config", epoch=1700000000)
    result = store.update_entry("pernix.config", 1700000000, "Server port is 8091 since June 2026")
    assert "Error" not in result

    entries = parse_entries_from_markdown("pernix.config", store.read_file("pernix.config"))
    assert len(entries) == 1
    assert entries[0].updated > 0
    assert entries[0].epoch == 1700000000  # identity unchanged

    results = store.search("server port", _track_hits=False)
    r = next(res for res in results if res.entry.file_name == "pernix.config")
    assert r.entry.updated > 0
    line = format_result_line(r)
    # The correction date is shown instead of the (old) epoch date.
    assert "updated=" in line and "date=" not in line


def test_update_entry_twice_keeps_single_updated_stamp(store):
    from core.memory.format import parse_entries_from_markdown

    store.add_entry("Fact v1", file_name="pernix.notes", epoch=1700000000)
    store.update_entry("pernix.notes", 1700000000, "Fact v2")
    store.update_entry("pernix.notes", 1700000000, "Fact v3")

    raw = store.read_file("pernix.notes")
    assert raw.count("@updated:") == 1
    entries = parse_entries_from_markdown("pernix.notes", raw)
    assert entries[0].content == "Fact v3"


# ---------------------------------------------------------------------------
# Hybrid search — temporal entries pad, never displace
# ---------------------------------------------------------------------------


def test_temporal_entries_do_not_displace_keyword_matches(store):
    """When BM25 fills the requested top-k, today's unrelated entries must
    not push relevant matches out (they used to enter at flat score 1.0)."""
    import time as _time

    old = int(_time.time()) - 7 * 86400  # outside the 24h temporal window
    for i in range(6):
        store.add_entry(f"kumquat protocol step {i}", file_name="pernix.notes", epoch=old + i)
    store.add_entry("fresh unrelated chromecast note", file_name="pernix.casting")
    store.add_entry("fresh unrelated linkedin note", file_name="pernix.social")

    results = store.search("kumquat protocol", limit=5, _track_hits=False)
    assert len(results) == 5
    assert all("kumquat" in r.entry.content for r in results)


def test_temporal_entries_pad_when_few_keyword_matches(store):
    import time as _time

    old = int(_time.time()) - 7 * 86400
    store.add_entry("kumquat protocol overview", file_name="pernix.notes", epoch=old)
    store.add_entry("fresh chromecast pairing note", file_name="pernix.casting")

    results = store.search("kumquat protocol", limit=5, _track_hits=False)
    # Keyword match ranks first; recent entry pads the remaining slots.
    assert results[0].entry.content == "kumquat protocol overview"
    assert any(r.source == "temporal" for r in results[1:])


# ---------------------------------------------------------------------------
# BM25 length normalization
# ---------------------------------------------------------------------------


def test_bm25_scores_are_length_normalized(store):
    """Padding a query with filler tokens must not inflate the score —
    the raw OR-sum grew with query length, making the documented absolute
    thresholds (3.0 strong / 1.0 noise) meaningless for long queries."""
    store.add_entry("zorblax calibration uses kumquat brine at 40 degrees", file_name="pernix.notes")

    short = store.search("zorblax calibration", mode="bm25", _track_hits=False)
    long = store.search(
        "how would someone possibly configure the zorblax calibration procedure for the brine again today",
        mode="bm25",
        _track_hits=False,
    )

    assert short and long
    assert short[0].entry.content == long[0].entry.content
    assert long[0].score < short[0].score


def test_prepare_fts_query_returns_token_count():
    from core.memory.search import prepare_fts_query

    fts, n = prepare_fts_query("web scraping challenges")
    assert n == 3
    assert fts == '"web" OR "scraping" OR "challenges"'

    fts, n = prepare_fts_query("!!!")
    assert n == 1  # degenerate fallback quotes the raw query


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


def test_inferred_tags_survive_markdown_roundtrip(store):
    from core.memory.format import parse_entries_from_markdown

    store.add_entry("Kubernetes deployment rollback procedure using kubectl rollout undo.", file_name="ops.k8s")
    entries = parse_entries_from_markdown("ops.k8s", store.read_file("ops.k8s"))
    assert entries[0].tags, "inferred tags must reach markdown, not just FTS"
    n = store.reindex()
    assert n == 1
    results = store.search(entries[0].tags[0], mode="bm25", limit=3)
    assert results, "tags must survive reindex"


def test_at_tags_filter_is_real(store):
    store.add_entry("Deploy notes for the alpha service revision batch.", file_name="ops.notes", tags="deploy,alpha")
    store.add_entry(
        "Deploy notes for the beta service revision batch two.",
        file_name="ops.notes",
        tags="deploy,beta",
        skip_dedup=True,
    )
    hits = store.search("deploy notes @tags: beta", mode="bm25", limit=5)
    assert len(hits) == 1 and "beta" in hits[0].entry.tags
    only_tag = store.search("@tags: alpha", mode="bm25", limit=5)
    assert len(only_tag) == 1 and "alpha" in only_tag[0].entry.tags


# ---------------------------------------------------------------------------
# Exact-duplicate gate (2026-08-19)
# ---------------------------------------------------------------------------

_SNAPSHOT = (
    "US Market data for May 4, 2026 from CNBC: Dow Jones at 49,499.27 (-0.31%), "
    "S&P 500 at 7,230.12. Notable news: Spirit Airlines ceasing operations."
)


def test_exact_duplicate_is_refused_even_when_search_misses(store, monkeypatch):
    """The regression that put 409 redundant copies in the live store.

    find_duplicate's similarity gate is only as good as search RANKING: it
    inspects the top-3 candidates, and in a file of near-identical market
    snapshots the byte-identical twin can rank fourth (or the hybrid channel
    can degrade entirely when the embed endpoint is down — the box logged
    both on 2026-08-19, and dream flagged three of the resulting copies as a
    data-ingestion bug). Search returning NOTHING is the worst case of that
    failure; the exact-equality pre-gate must hold regardless.
    """
    r1 = store.add_entry(_SNAPSHOT, file_name="market.snapshots", source="distill")
    assert r1.startswith("Saved")
    monkeypatch.setattr(store, "search", lambda *a, **k: [])
    r2 = store.add_entry(_SNAPSHOT, file_name="market.snapshots", source="distill")
    assert "duplicate of market.snapshots@" in r2
    # And the file really has one copy, not two.
    assert store.read_file("market.snapshots").count("Dow Jones at 49,499.27") == 1


def test_exact_duplicate_refused_via_supersede_path(store, monkeypatch):
    """distill writes through add_or_supersede_entry — identical content is
    not a correction, so it must be refused there too, not rewritten."""
    store.add_entry(_SNAPSHOT, file_name="market.snapshots", source="distill")
    monkeypatch.setattr(store, "search", lambda *a, **k: [])
    r = store.add_or_supersede_entry(_SNAPSHOT, file_name="market.snapshots", source="distill")
    assert "duplicate of" in r
    assert store.read_file("market.snapshots").count("Dow Jones at 49,499.27") == 1


def test_near_duplicate_still_goes_through_the_similarity_gate(store):
    """The pre-gate is byte-exact by design — cosmetic edits stay the
    similarity gate's job, and genuinely new content still saves."""
    store.add_entry(_SNAPSHOT, file_name="market.snapshots", source="distill")
    novel = (
        "The weekend brief job schedule moved from 12-hour cadence to weekly "
        "execution after the workspace review on May 9."
    )
    assert store.add_entry(novel, file_name="market.snapshots", source="distill").startswith("Saved")
