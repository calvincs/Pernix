"""Live entries moved into an archived memory file were lost at the next reindex.

On 2026-11-03 at 12:14 the bloated-file split sweep moved 13 and 8 live
entries into `pernix.daily_brief_news_and_local_context` and
`pernix.geopolitical_events_and_conflict_tracking`. Both files carried the
file-level archived header from an earlier merge. `move_entries` appended
and indexed the entries but left the header, so the markdown read as having
no live entries. Search found them until the next reindex, which would drop
them for good. The mismatch also made every 6-hourly reconcile a full
reindex, and the first turn after a deploy waited 66 s for one.

Now `move_entries` reopens an archived target first, keeping its old entries
archived, and the health check repairs files already left half live.
"""

from __future__ import annotations

import pytest

from core.memory.format import is_file_archived, parse_entries_from_markdown, reopen_archived_file
from core.memory.store import MemoryStore


@pytest.fixture
def store(tmp_path):
    return MemoryStore(str(tmp_path / "memories"))


def _epochs(store, name):
    return sorted(e.epoch for e in parse_entries_from_markdown(name, store.read_file(name)))


def _indexed(store, name):
    conn = store._connect()
    try:
        return sorted(
            int(r["epoch"]) for r in conn.execute("SELECT epoch FROM memory_fts WHERE file_name = ?", (name,))
        )
    finally:
        conn.close()


def _archived_target(store):
    store.add_entry("Old fact one about the brief", file_name="pernix.brief_target", epoch=1000, skip_dedup=True)
    store.add_entry("Old fact two about the brief", file_name="pernix.brief_target", epoch=1001, skip_dedup=True)
    store.archive_file("pernix.brief_target")
    store.add_entry("A live fact that will move", file_name="pernix.brief_source", epoch=2000, skip_dedup=True)
    store.add_entry("Another live fact to move", file_name="pernix.brief_source", epoch=2001, skip_dedup=True)


def test_reopen_keeps_old_entries_archived():
    text = (
        "<!-- @file: x.y -->\n<!-- @archived: true -->\n<!-- @created: 1 -->\n"
        "\n---\n<!-- @epoch: 10 -->\nold\n"
        "\n---\n<!-- @epoch: 11 -->\n<!-- @archived: true -->\nretired\n"
        "\n---\n<!-- @epoch: 12 -->\nkept\n"
    )
    out = reopen_archived_file(text, keep_live={12: "kept"})

    assert not is_file_archived(out)
    assert [e.epoch for e in parse_entries_from_markdown("x.y", out)] == [12]
    assert reopen_archived_file(out) == out  # a live file is left alone


def test_reopen_needs_the_content_to_match_not_just_the_epoch():
    text = "<!-- @file: x.y -->\n<!-- @archived: true -->\n" "\n---\n<!-- @epoch: 12 -->\nretired twin\n"
    out = reopen_archived_file(text, keep_live={12: "the moved fact"})

    assert parse_entries_from_markdown("x.y", out) == []


def test_moved_entries_survive_a_reindex(store):
    _archived_target(store)

    moved = store.move_entries("pernix.brief_source", "pernix.brief_target", [2000, 2001])

    assert moved == 2
    assert _epochs(store, "pernix.brief_target") == [2000, 2001]  # old 1000/1001 stay archived
    assert store._survey()["in_sync"]
    store.reindex()
    assert _indexed(store, "pernix.brief_target") == [2000, 2001]


def test_a_moved_entry_does_not_reuse_an_archived_epoch(store):
    store.add_entry("Old fact in the target", file_name="pernix.brief_target", epoch=2000, skip_dedup=True)
    store.archive_file("pernix.brief_target")
    store.add_entry("A live fact that will move", file_name="pernix.brief_source", epoch=2000, skip_dedup=True)

    store.move_entries("pernix.brief_source", "pernix.brief_target", [2000])

    assert _epochs(store, "pernix.brief_target") == [2001]
    assert store._survey()["epoch_collisions"] == 0


def test_health_check_repairs_a_half_live_file_without_a_reindex(store, monkeypatch):
    _archived_target(store)
    # Recreate the 11-03 damage: entries appended and indexed, header left archived.
    store.move_entries("pernix.brief_source", "pernix.brief_target", [2000, 2001])
    store.rewrite_file(
        "pernix.brief_target",
        lambda raw: raw.replace(
            "<!-- @file: pernix.brief_target -->", "<!-- @file: pernix.brief_target -->\n<!-- @archived: true -->", 1
        ),
    )
    assert not store._survey()["in_sync"]

    def _no_reindex():
        raise AssertionError("the repair should make a reindex unnecessary")

    monkeypatch.setattr(store, "reindex", _no_reindex)
    result = store.health_check(fix=True)

    assert result["in_sync"]
    assert _epochs(store, "pernix.brief_target") == [2000, 2001]


def test_drift_is_named_before_the_reindex(store, caplog):
    store.add_entry("Some fact", file_name="pernix.drifty", epoch=3000, skip_dedup=True)
    conn = store._connect()
    try:
        conn.execute("DELETE FROM memory_fts WHERE file_name = 'pernix.drifty'")
        conn.commit()
    finally:
        conn.close()

    with caplog.at_level("INFO", logger="pernix.memory"):
        store.health_check(fix=True)

    assert "pernix.drifty (md 1, idx 0)" in caplog.text


def test_boot_warms_the_memory_store():
    import inspect

    from api import app as app_module

    assert "_warm_memory_store" in inspect.getsource(app_module.lifespan)


def test_reopen_matches_a_body_that_holds_a_comment_line():
    text = (
        "<!-- @file: x.y -->\n<!-- @archived: true -->\n"
        "\n---\n<!-- @epoch: 12 -->\nfirst line\n<!-- quoted html -->\nlast line\n"
    )
    out = reopen_archived_file(text, keep_live={12: "first line\nlast line"})

    assert [e.epoch for e in parse_entries_from_markdown("x.y", out)] == [12]
