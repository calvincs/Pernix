"""Regression: memory files were truncated *before* the flock was taken.

Shipped defect (architecture review 2026-08-07, "correctness bugs"): five
rewrite sites opened a memory file with mode "w" and called `fcntl.flock` on
the next line. `open(path, "w")` truncates at open, so the lock guarded
nothing — a concurrent reader (recall, reindex, a hand-editing user) landing
between the truncation and the write observed an empty memory file, and
`parse_entries_from_markdown` reads that as a file with no entries. Sites:
`store.update_entry`, `store.delete_entry`, `store.archive_file`,
`store.repair_epoch_collisions`, `sweeps._archive_entries_in_file`,
`sweeps.update_tags_in_markdown`.

Fix: one `MemoryStore._write_locked` primitive — os.open(O_RDWR|O_CREAT),
flock, then seek/truncate/write, so the empty window lives entirely inside the
lock. `MemoryStore.rewrite_file` exposes read-modify-write on the public
surface, and the two sweeps call it instead of reimplementing the sequence
against `store._dir` / `store._lock` (the private-state copy is how the race
was duplicated into that module in the first place).
"""

from __future__ import annotations

import fcntl
import re
import threading
import time
from pathlib import Path

import pytest

from core.memory.store import MemoryStore

_ENTRY = "The reverse proxy terminates TLS and forwards plain HTTP to the app on the loopback interface."


@pytest.fixture
def store(tmp_path):
    return MemoryStore(str(tmp_path / "memories"))


def test_content_survives_while_another_holder_owns_the_lock(store):
    """A blocked writer must not have truncated the file yet."""
    store.add_entry(_ENTRY, file_name="pernix.config")
    md_path = Path(store._dir) / "pernix.config.md"
    before = md_path.read_text()

    started = threading.Event()
    done = threading.Event()

    def _writer():
        started.set()
        store.rewrite_file("pernix.config", lambda raw: raw + "\n<!-- rewritten -->")
        done.set()

    blocker = open(md_path, "r+")
    fcntl.flock(blocker.fileno(), fcntl.LOCK_EX)
    thread = threading.Thread(target=_writer, daemon=True)
    try:
        thread.start()
        started.wait(timeout=5)
        # Give the blocked writer time to have done any damage it was going to.
        time.sleep(0.3)
        assert not done.is_set(), "writer should be parked on the flock"
        # The pre-fix code truncated here; the file would read as "".
        assert md_path.read_text() == before
    finally:
        fcntl.flock(blocker.fileno(), fcntl.LOCK_UN)
        blocker.close()

    thread.join(timeout=5)
    assert done.is_set()
    assert md_path.read_text() == before + "\n<!-- rewritten -->"


def test_rewrite_file_is_a_no_op_when_transform_declines(store):
    store.add_entry(_ENTRY, file_name="pernix.config")
    md_path = Path(store._dir) / "pernix.config.md"
    before = md_path.read_text()

    assert store.rewrite_file("pernix.config", lambda raw: None) is False
    assert store.rewrite_file("pernix.config", lambda raw: raw) is False
    assert store.rewrite_file("pernix.absent", lambda raw: raw + "x") is False
    assert md_path.read_text() == before


def test_update_and_delete_still_round_trip_through_the_index(store):
    confirmation = store.add_entry(_ENTRY, file_name="pernix.config")
    epoch = int(confirmation.split("epoch=")[1].rstrip(")"))

    store.update_entry("pernix.config", epoch, "TLS now terminates in the app itself, no proxy in the path.")
    assert "reverse proxy" not in store.read_file("pernix.config")
    assert store.health_check()["in_sync"]

    store.delete_entry("pernix.config", epoch)
    assert store.health_check()["in_sync"]
    assert {f.name: f.entry_count for f in store.list_files()}["pernix.config"] == 0


# `with open(..., "w")` only — the prose in _write_locked's docstring names the
# broken pattern on purpose and must not trip this. A truncating open of a
# `*_tmp`/`tmp_*` path is the ATOMIC pattern (write a sibling temp, fsync,
# os.replace onto the target), which is what _write_locked does now: nothing
# reads that temp file, and the destination is only ever swapped whole.
_TRUNCATING_OPEN = re.compile(r"""with\s+open\((?![^)]*tmp)[^)]*["']w["']""")


def test_no_truncating_open_remains_in_the_memory_write_paths():
    """Pin the pattern: no rewrite truncates a file readers can see.

    The original defect was `open(path, "w")` (truncates at open time) with
    the flock taken afterwards, so a reader landing in the gap saw an empty
    memory file. Writing a temp file and renaming has no such gap.
    """
    root = Path(__file__).resolve().parents[2] / "core" / "memory"
    for name in ("store.py", "sweeps.py"):
        source = (root / name).read_text()
        assert not _TRUNCATING_OPEN.search(source), f"{name} reintroduced a truncate-then-lock rewrite"


def test_the_atomic_temp_write_is_still_recognised_as_safe():
    """The guard above must not be satisfied by simply deleting the write."""
    root = Path(__file__).resolve().parents[2] / "core" / "memory"
    source = (root / "store.py").read_text()
    assert "os.replace(tmp_path, md_path)" in source, "the rewrite must land via an atomic rename"
    assert "os.fsync(" in source, "the temp file must be flushed to disk before the rename"
