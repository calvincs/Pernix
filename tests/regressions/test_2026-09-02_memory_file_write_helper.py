"""Saving a memory file reached past the store to keep the index honest.

`PUT /api/memory/files/{name}` replaced a file with `rewrite_file` — a
read-modify-write that answers False when the new text matches the old — and
then called the package-internal `_reindex_commit` so search would stop
matching text the file no longer contains. Three problems in one: a router
calling a private method, two lock acquisitions where one would do, and an
index commit landing *outside* the flock that is supposed to make the markdown
and its index move together.

`MemoryStore.write_file` is the one public whole-file save: validate, write
through `_write_locked` (temp file + fsync + rename, writers excluded), and
commit the re-index on `on_written` — inside the lock, exactly as
`update_entry` and `delete_entry` already did.
"""

from __future__ import annotations

import pytest

from core.memory.format import format_entry, format_file_header
from core.memory.store import MemoryStore


@pytest.fixture()
def store(tmp_path):
    return MemoryStore(str(tmp_path / "memories"))


def _entry_count(store: MemoryStore, name: str) -> int:
    row = next((f for f in store.list_files() if f.name == name), None)
    return row.entry_count if row else -1


def test_write_file_replaces_the_markdown_and_the_index_follows(store):
    store.add_entry("Ferrets are nocturnal and sleep eighteen hours", file_name="pernix.notes")
    store.add_entry("Otters hold hands while sleeping to avoid drifting", file_name="pernix.notes")
    assert _entry_count(store, "pernix.notes") == 2
    assert any("ferret" in r.entry.content.lower() for r in store.search("ferrets nocturnal"))

    new_raw = format_file_header("pernix.notes", "Notes", ["notes"]) + format_entry(
        "Pangolins are the only mammals with keratin scales",
    )
    store.write_file("pernix.notes", new_raw)

    # Read back: the markdown is the source of truth and it is exactly what
    # was handed in.
    assert store.read_file("pernix.notes") == new_raw

    # The index followed it — one entry now, and the dropped text no longer
    # matches. (The old code got this right only by pairing two calls.)
    assert _entry_count(store, "pernix.notes") == 1
    assert not [r for r in store.search("ferrets nocturnal") if "ferret" in r.entry.content.lower()]
    assert any("pangolin" in r.entry.content.lower() for r in store.search("pangolins keratin scales"))


def test_write_file_reindexes_even_when_the_bytes_do_not_change(store):
    """The no-op case rewrite_file refused, which is why the router paired it
    with a private re-index. Writing identical markdown still leaves the
    index describing the file."""
    store.add_entry("Cuttlefish see polarized light", file_name="pernix.notes")
    raw = store.read_file("pernix.notes")

    store.write_file("pernix.notes", raw)

    assert store.read_file("pernix.notes") == raw
    assert _entry_count(store, "pernix.notes") == 1


def test_write_file_creates_a_missing_file_with_its_row(store):
    store.write_file("pernix.fresh", format_file_header("pernix.fresh", "Fresh", ["fresh"]))
    assert store.read_file("pernix.fresh") is not None
    assert _entry_count(store, "pernix.fresh") == 0


@pytest.mark.parametrize("bad", ["", "../escape", "has space", "/absolute", ".leading-dot"])
def test_write_file_rejects_an_invalid_name(store, bad):
    with pytest.raises(ValueError):
        store.write_file(bad, "anything")


def test_write_file_leaves_nothing_behind_on_a_failed_write(store, monkeypatch):
    """`_write_locked` unlinks its temp file on any exception; a failing
    re-index must not leave one either, since the sweeps glob the directory."""
    store.add_entry("Axolotls regrow limbs", file_name="pernix.notes")

    def _boom(*_a, **_k):
        raise RuntimeError("index down")

    monkeypatch.setattr(store, "_reindex_commit", _boom)
    with pytest.raises(RuntimeError):
        store.write_file("pernix.notes", "replacement")

    assert not list((store._dir).glob("*.tmp"))
