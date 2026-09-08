"""A rebuild and a remembered fact, running at once, indexed it twice.

`reindex()` took no lock at all. It opened a write transaction with `DELETE
FROM memory_fts` / `DELETE FROM memory_files` and then held that transaction
open across the whole corpus scan, which let this interleaving happen:

  * `add_entry` checked the old *committed* index for a free epoch — WAL
    readers see the last commit, not the rebuild's uncommitted deletes — and
    kept the epoch it had picked.
  * It appended to the markdown and flushed.
  * It then blocked on its own `INSERT`, behind the rebuild's writer lock.
  * The rebuild scanned that markdown, indexed the new entry, and committed.
  * The waiting insert woke up and added the same entry a second time, with a
    second `entry_count = entry_count + 1` on top of the count the rebuild had
    just set absolutely.

Four entries in the markdown, five rows in the index, one epoch twice, a file
declaring five, and `health_check` answering `in_sync: False`. The other half
of the same race needs no duplicate to hurt: past the 5s busy timeout the
mutation raises `OperationalError: database is locked` *after* its markdown
has already landed, so the entry is in the source of truth, missing from the
index, and the caller has been told the write failed.

`add_entry` is the only writer that can double, and the reason is worth
keeping straight: update / delete / whole-file save all publish through
`_reindex_file`, which is absolute (`DELETE … WHERE file_name = ?`, reinsert,
`entry_count = ?`), so they correctly overwrite the rebuild's answer when they
unblock. `add_entry` is the one relative mutation — `INSERT` plus
`entry_count + 1` — and relative arithmetic on top of a number somebody else
just replaced is the whole bug.

The fix is exclusion: a rebuild is a mutation of every file's rows and now
queues with the others on the store's lock. That is not new blocking — the
`DELETE` already excluded every writer for exactly the same span — it just
makes the waiting orderly and the arithmetic right. What must NOT change is
that the deletes and inserts stay inside one transaction: an exception during
the scan rolls the whole rebuild back, so a half-built index can never be
published, and moving the scan outside that transaction without a replacement
protocol would trade this race for a worse one.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

import core.memory.store as store_mod
from core.memory.format import parse_entries_from_markdown
from core.memory.store import MemoryStore


@pytest.fixture
def store(tmp_path):
    return MemoryStore(str(tmp_path / "memories"))


# ---------------------------------------------------------------------------
# Barriers. Two signals, one event: whichever the world provides.
# ---------------------------------------------------------------------------


class _GatedLock:
    """The store's mutation lock, plus a signal for "this acquire will wait".

    A non-blocking acquire that FAILS proves the lock is held elsewhere and
    this caller is about to queue. With the rebuild holding it, that fires the
    moment a mutation reaches the gate — which is where a fixed store makes it
    stop.
    """

    def __init__(self, inner: threading.Lock, on_block):
        self._inner = inner
        self._on_block = on_block

    def __enter__(self):
        if not self._inner.acquire(blocking=False):
            self._on_block()
            self._inner.acquire()
        return self

    def __exit__(self, *_exc):
        self._inner.release()
        return False


def _classify(sql: str) -> str:
    """Which side of the race this statement belongs to.

    Told apart by the statement itself rather than by which thread ran it: a
    thread's id is only knowable after it has started, and by then it may
    already be past the point the barrier needs to catch.
    """
    s = " ".join(sql.split()).upper()
    if s.startswith("DELETE FROM MEMORY_FILES") and "WHERE" not in s:
        # Only a whole-corpus rebuild clears the registry outright.
        return "rebuild_cleared_the_index"
    if s.startswith(("INSERT INTO MEMORY_FTS", "DELETE FROM MEMORY_FTS", "UPDATE MEMORY_FTS")):
        return "mutation_publishing"
    return ""


class _Worker(threading.Thread):
    def __init__(self, fn):
        super().__init__(daemon=True)
        self._fn = fn
        self.result = None
        self.error: BaseException | None = None

    def run(self):
        try:
            self.result = self._fn()
        except BaseException as exc:  # noqa: BLE001 — the test inspects it
            self.error = exc

    def settle(self):
        self.join(timeout=30)
        assert not self.is_alive(), "a writer never finished"
        return self


class _Interleave:
    """Runs one mutation against a rebuild frozen with the index cleared.

    The rebuild stops the instant it has emptied `memory_fts` / `memory_files`
    — write transaction open, corpus not yet read, which is the state the old
    code held for the whole scan. It resumes only once the mutation has
    provably reached one of two places, and which one is exactly the tell:

      * queued at the store's lock, which is where a rebuild that excludes
        writers makes it stop; or
      * about to publish its own index rows, markdown already written, which
        is where a rebuild that excludes nothing lets it get to.

    Either way the resume is caused by the mutation thread, so there is
    nothing to sleep on and no ordering left to luck. Freezing before the
    read is the whole point: pausing at the parse instead lets `reindex` read
    the pre-append text, and the interleaving quietly does not happen.
    """

    def __init__(self, store: MemoryStore, monkeypatch):
        self._store = store
        self.rebuild_cleared = threading.Event()
        self.mutation_ready = threading.Event()

        store._lock = _GatedLock(store._lock, self.mutation_ready.set)

        real_connect = store._connect
        outer = self

        class _Watched:
            def __init__(self, inner):
                self._inner = inner

            def execute(self, sql, *args):
                kind = _classify(sql)
                if kind == "rebuild_cleared_the_index" and not outer.rebuild_cleared.is_set():
                    cursor = self._inner.execute(sql, *args)
                    outer.rebuild_cleared.set()
                    outer.mutation_ready.wait()
                    return cursor
                if kind == "mutation_publishing" and outer.rebuild_cleared.is_set():
                    outer.mutation_ready.set()
                return self._inner.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        monkeypatch.setattr(store, "_connect", lambda: _Watched(real_connect()))

    def run(self, mutation):
        rebuild = _Worker(self._store.reindex)
        mutator = _Worker(mutation)

        rebuild.start()
        self.rebuild_cleared.wait()
        mutator.start()
        # If the mutation ever reached neither signal, `settle`'s bounded join
        # reports a deadlock instead of hanging the suite.
        mutator.settle()
        self.mutation_ready.set()
        rebuild.settle()
        return rebuild, mutator


# ---------------------------------------------------------------------------
# Reading the index back
# ---------------------------------------------------------------------------


def _index_rows(store: MemoryStore) -> list[tuple[str, str]]:
    conn = store._connect()
    try:
        return [(r["file_name"], r["epoch"]) for r in conn.execute("SELECT file_name, epoch FROM memory_fts")]
    finally:
        conn.close()


def _declared_count(store: MemoryStore, name: str) -> int:
    row = next((f for f in store.list_files() if f.name == name), None)
    return row.entry_count if row else -1


def _markdown_count(store: MemoryStore, name: str) -> int:
    return len(parse_entries_from_markdown(name, store.read_file(name) or ""))


def _assert_index_describes_the_markdown(store: MemoryStore, name: str):
    rows = _index_rows(store)
    assert len(rows) == len(set(rows)), f"an entry is indexed more than once: {rows}"
    real = _markdown_count(store, name)
    assert len([r for r in rows if r[0] == name]) == real
    assert _declared_count(store, name) == real
    assert store.health_check()["in_sync"] is True


def _seed(store: MemoryStore, n: int = 3):
    for i in range(n):
        store.add_entry(f"Seed entry {i} about otters and their sleeping arrangements", file_name="pernix.notes")


# ---------------------------------------------------------------------------
# The interleavings
# ---------------------------------------------------------------------------


def test_an_append_during_a_rebuild_is_indexed_exactly_once(store, monkeypatch):
    """The measured failure: 4 markdown entries, 5 index rows, one epoch twice."""
    _seed(store, 3)

    rebuild, mutator = _Interleave(store, monkeypatch).run(
        lambda: store.add_entry("Pangolins are the only mammals with keratin scales", file_name="pernix.notes")
    )

    assert rebuild.error is None, rebuild.error
    assert mutator.error is None, mutator.error
    assert "Error" not in (mutator.result or ""), mutator.result

    _assert_index_describes_the_markdown(store, "pernix.notes")
    assert _markdown_count(store, "pernix.notes") == 4
    assert "keratin scales" in store.read_file("pernix.notes")


def test_an_editor_save_during_a_rebuild_leaves_one_consistent_index(store, monkeypatch):
    _seed(store, 3)
    replacement = store.read_file("pernix.notes") + "\n"

    rebuild, mutator = _Interleave(store, monkeypatch).run(
        lambda: store.write_file("pernix.notes", replacement + "<!-- @epoch: 4242 -->\nA whole-file save\n")
    )

    assert rebuild.error is None, rebuild.error
    assert mutator.error is None, mutator.error
    _assert_index_describes_the_markdown(store, "pernix.notes")


def test_an_update_during_a_rebuild_leaves_one_consistent_index(store, monkeypatch):
    _seed(store, 3)
    epoch = parse_entries_from_markdown("pernix.notes", store.read_file("pernix.notes"))[0].epoch

    rebuild, mutator = _Interleave(store, monkeypatch).run(
        lambda: store.update_entry("pernix.notes", epoch, "Corrected: otters raft, they do not hold hands")
    )

    assert rebuild.error is None, rebuild.error
    assert mutator.error is None, mutator.error
    assert "Error" not in mutator.result, mutator.result
    _assert_index_describes_the_markdown(store, "pernix.notes")
    assert "Corrected: otters raft" in store.read_file("pernix.notes")
    assert _markdown_count(store, "pernix.notes") == 3


def test_a_delete_during_a_rebuild_leaves_one_consistent_index(store, monkeypatch):
    _seed(store, 3)
    epoch = parse_entries_from_markdown("pernix.notes", store.read_file("pernix.notes"))[-1].epoch

    rebuild, mutator = _Interleave(store, monkeypatch).run(lambda: store.delete_entry("pernix.notes", epoch))

    assert rebuild.error is None, rebuild.error
    assert mutator.error is None, mutator.error
    assert "Error" not in mutator.result, mutator.result
    _assert_index_describes_the_markdown(store, "pernix.notes")
    assert _markdown_count(store, "pernix.notes") == 2


def test_a_mutation_during_a_rebuild_never_reports_a_locked_database(store, monkeypatch):
    """The half of the race with no duplicate in it.

    Past the busy timeout the mutation used to raise with its markdown already
    written — present in the source of truth, absent from the index, reported
    to the caller as a failure. Waiting on the store's lock has no timeout, so
    the outcome is a wait, not a lie.

    This pins the invariant rather than reproducing the five-second stall:
    holding a rebuild open that long is a sleep, and a sleep is not a barrier.
    The interleaving is the same one the tests above drive.
    """
    _seed(store, 2)

    rebuild, mutator = _Interleave(store, monkeypatch).run(
        lambda: store.add_entry("Cuttlefish see polarized light", file_name="pernix.notes")
    )

    assert not isinstance(mutator.error, sqlite3.OperationalError)
    assert mutator.error is None, mutator.error
    assert "Cuttlefish" in store.read_file("pernix.notes")
    assert any("cuttlefish" in r.entry.content.lower() for r in store.search("cuttlefish polarized"))


# ---------------------------------------------------------------------------
# What must not regress
# ---------------------------------------------------------------------------


def test_a_rebuild_that_fails_midway_publishes_no_half_index(store, monkeypatch):
    """All-or-nothing was already true and has to stay true.

    Every DELETE and INSERT lives in one uncommitted transaction closed by the
    `finally`, so an exception during the scan rolls the rebuild back and the
    previous, usable index survives untouched.
    """
    _seed(store, 2)
    store.add_entry("Axolotls regrow limbs", file_name="pernix.other")
    before = sorted(_index_rows(store))
    assert before

    real_parse = store_mod.parse_entries_from_markdown
    seen: list[str] = []
    # Disarmed rather than un-patched: `monkeypatch.undo()` would also undo
    # conftest's data-directory isolation, which is patched on the same object.
    armed = {"on": True}

    def _explode(file_name, text):
        if armed["on"]:
            seen.append(file_name)
            if len(seen) > 1:
                raise RuntimeError("corpus went away mid-scan")
        return real_parse(file_name, text)

    monkeypatch.setattr(store_mod, "parse_entries_from_markdown", _explode)

    with pytest.raises(RuntimeError):
        store.reindex()

    armed["on"] = False
    assert sorted(_index_rows(store)) == before, "a partial rebuild was published"
    assert any("axolotl" in r.entry.content.lower() for r in store.search("axolotls regrow limbs"))


def test_the_lock_is_released_when_a_rebuild_raises(store, monkeypatch):
    """A rebuild that dies holding the mutation lock would wedge every writer."""
    _seed(store, 1)
    real_parse = store_mod.parse_entries_from_markdown
    armed = {"on": True}

    def _explode(file_name, text):
        if armed["on"]:
            raise RuntimeError("nope")
        return real_parse(file_name, text)

    monkeypatch.setattr(store_mod, "parse_entries_from_markdown", _explode)
    with pytest.raises(RuntimeError):
        store.reindex()
    armed["on"] = False

    assert store._lock.acquire(blocking=False), "reindex kept the lock"
    store._lock.release()
    assert "Error" not in store.add_entry("Still writable afterwards", file_name="pernix.notes")


# ---------------------------------------------------------------------------
# health_check's own arithmetic
# ---------------------------------------------------------------------------


def test_health_check_reports_the_corpus_the_repair_left_behind(store):
    """The verdict used to be computed before the repair that invalidates it.

    `repair_epoch_collisions` drops identical twins from the markdown and
    re-indexes the files it touched. Counting once, up front, meant deciding
    whether to rebuild — and reporting to the caller — from numbers describing
    a corpus that no longer existed.
    """
    store.add_entry("Ferrets sleep eighteen hours a day", file_name="pernix.notes", epoch=1000)
    md = store._dir / "pernix.notes.md"
    raw = md.read_text()
    twin = raw.split("\n---\n")[-1]
    md.write_text(raw + "\n---\n" + twin)
    store.reindex()

    assert _markdown_count(store, "pernix.notes") == 2

    result = store.health_check(fix=True)

    assert result["repaired_epoch_collisions"] == 1
    assert result["markdown_entries"] == 1, "counted before the twin was dropped"
    assert result["indexed_entries"] == len(_index_rows(store))
    assert result["in_sync"] is True
    assert result["epoch_collisions"] == 0


def test_health_check_reports_what_the_rebuild_actually_indexed(store):
    """`indexed_entries` used to be the stale pre-rebuild count — which is
    also the number the startup log prints as "reindexed N entries"."""
    _seed(store, 3)
    conn = store._connect()
    try:
        conn.execute("DELETE FROM memory_fts")
        conn.commit()
    finally:
        conn.close()

    result = store.health_check(fix=True)

    assert result["action"] == "reindexed"
    assert result["indexed_entries"] == 3
    assert result["indexed_entries"] == len(_index_rows(store))
    assert result["in_sync"] is True
