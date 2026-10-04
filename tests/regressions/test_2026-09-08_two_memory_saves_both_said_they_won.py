"""The memory editor's conflict check ran nowhere near the write it guarded.

`PUT /api/memory/files/{name}` validated `base_mtime` in the handler and then
called `store.write_file`, which took the store's lock for the first time
inside itself. Nothing spanned the two. Two saves carrying the same
`base_mtime` both passed the check before either reached the lock, both wrote,
both answered `{"saved": true}`, and one of the two edits was gone with
nobody told. An agent's `add_entry` landing in the same window was destroyed
the same way — accepted with a 200, overwritten by a draft that predated it.

The read had the mirror-image hole. GET fetched the content in one call and
stat'ed the path in another, so it could hand back the older text paired with
the mtime of the version that had just replaced it: a token certifying bytes
the editor was never shown. From there the 409 could not fire at all — the
next save matched a version that was already current and quietly won. The
save response repeated the trick, refreshing the editor's token from a stat
taken after the lock had been released.

Validation now happens inside the store's mutation lock, immediately before
the replacement, and the version returned is computed from the bytes that
were written rather than from a later look at the disk. Reads answer from one
open descriptor, so content and version are one observation. The token itself
is a digest of the bytes — `base_mtime` still works for the shipped editor,
but a float compare needs a millisecond of slack for the JSON round-trip and
two writes inside that slack are indistinguishable to it, which is exactly
the case a content digest decides correctly.

The exclusion is in-process. Another OS process, or a human with the file
open in vim, is outside it: a digest compare under a lock is a consistency
check, not filesystem compare-and-swap.
"""

from __future__ import annotations

import asyncio
import os
import threading

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from core.memory.store import MemoryStore, MemoryVersionConflict, _revision_of


@pytest.fixture
def store(monkeypatch, tmp_path):
    """A real MemoryStore on a temp dir, wired in as the API's singleton."""
    st = MemoryStore(memory_dir=str(tmp_path / "memories"))
    monkeypatch.setattr("core.memory.store.get_memory_store", lambda: st)
    return st


def _app():
    from api.routers import memory

    app = FastAPI()
    app.include_router(memory.router)
    return app


async def _get(url):
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as c:
        return await c.get(url)


async def _put(url, body):
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as c:
        return await c.put(url, json=body)


# ---------------------------------------------------------------------------
# Barriers. Every wait below is on an event another thread provably sets;
# nothing here sleeps, and nothing depends on how fast a machine runs.
# ---------------------------------------------------------------------------


class _GatedLock:
    """The store's mutation lock, plus a signal for "this acquire will wait".

    A non-blocking acquire that FAILS is proof, not a guess, that someone else
    holds the lock and this caller is about to queue behind them. That makes
    "the second writer is now waiting at the gate" an event a test can wait on
    instead of a race it has to hope for.
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


class _Worker(threading.Thread):
    """Runs one callable off-thread and keeps whatever came back — value or
    exception — so the assertions can be made on the main thread."""

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
        # Generous, and only ever reached if the code under test deadlocks —
        # a failure mode worth reporting as a failure rather than a hang.
        self.join(timeout=30)
        assert not self.is_alive(), "writer never finished"
        return self


def _seed(store: MemoryStore, text: str = "Otters hold hands while sleeping") -> tuple[str, object]:
    store.add_entry(text, file_name="pernix.notes")
    content, version = store.read_file_versioned("pernix.notes")
    return content, version


# ---------------------------------------------------------------------------
# The two-writer window
# ---------------------------------------------------------------------------


def test_two_saves_from_one_base_version_leave_exactly_one_winner(store):
    """Both editors read the same version; both save. One must be refused.

    The first writer is held INSIDE the critical section until the second is
    provably queued for it — which is the interleaving the old code could not
    survive, because by then both had already passed a check made outside.
    """
    content, base = _seed(store)
    queued = threading.Event()
    inside = threading.Event()

    store._lock = _GatedLock(store._lock, queued.set)

    real_commit = store._reindex_commit

    def _hold(name, raw):
        if not inside.is_set():
            inside.set()
            queued.wait()
        real_commit(name, raw)

    store._reindex_commit = _hold

    first = _Worker(
        lambda: store.write_file("pernix.notes", content + "\nfirst editor\n", expected_revision=base.revision)
    )
    second = _Worker(
        lambda: store.write_file("pernix.notes", content + "\nsecond editor\n", expected_revision=base.revision)
    )

    first.start()
    inside.wait()
    second.start()

    first.settle()
    second.settle()

    assert first.error is None, first.error
    assert isinstance(second.error, MemoryVersionConflict)

    # Refused, not partially applied: the loser's text is nowhere.
    on_disk = store.read_file("pernix.notes")
    assert "first editor" in on_disk
    assert "second editor" not in on_disk

    # And the conflict says what IS there, so the user can be told what they
    # would have overwritten.
    assert second.error.current.revision == _revision_of(on_disk.encode("utf-8"))


def test_an_append_in_the_save_window_is_not_destroyed(store):
    """An agent writes between the editor's read and the editor's write.

    The append is stopped exactly where the old check used to sit: the save
    request has arrived and chosen its expected version, and the store has not
    touched the file yet. Under the old boundary the save sailed through and
    took the append with it.
    """
    content, base = _seed(store)
    at_the_gate = threading.Event()
    appended = threading.Event()

    real_ensure = store._ensure_file

    def _gate(name, file_content=""):
        real_ensure(name, file_content)
        if not at_the_gate.is_set():
            at_the_gate.set()
            appended.wait()

    store._ensure_file = _gate

    saver = _Worker(
        lambda: store.write_file("pernix.notes", content + "\nthe editor's draft\n", expected_revision=base.revision)
    )
    saver.start()
    at_the_gate.wait()

    store.add_entry("Pangolins are the only mammals with keratin scales", file_name="pernix.notes")
    appended.set()
    saver.settle()

    assert isinstance(saver.error, MemoryVersionConflict)
    on_disk = store.read_file("pernix.notes")
    assert "keratin scales" in on_disk, "the append was overwritten by a draft that predated it"
    assert "the editor's draft" not in on_disk


def test_a_save_reports_the_version_it_committed_not_a_later_one(store):
    """The token handed back must describe the caller's own bytes.

    A stat taken after the lock is released can just as easily describe the
    writer who was queued behind you — and an editor that adopts it is set up
    to overwrite them on its next save without a warning.
    """
    content, base = _seed(store)
    queued = threading.Event()
    inside = threading.Event()
    store._lock = _GatedLock(store._lock, queued.set)

    real_commit = store._reindex_commit

    def _hold(name, raw):
        if not inside.is_set():
            inside.set()
            queued.wait()
        real_commit(name, raw)

    store._reindex_commit = _hold

    mine = content + "\nmine\n"
    first = _Worker(lambda: store.write_file("pernix.notes", mine, expected_revision=base.revision))
    first.start()
    inside.wait()

    # Queued the whole time the first writer is inside, and lands the instant
    # it releases.
    second = _Worker(lambda: store.write_file("pernix.notes", content + "\ntheirs\n"))
    second.start()

    first.settle()
    second.settle()
    assert first.error is None, first.error
    assert second.error is None, second.error

    assert first.result.revision == _revision_of(mine.encode("utf-8"))
    assert first.result.size == len(mine.encode("utf-8"))

    # It is a real base, not a decoration: the file has since moved, so
    # saving against it conflicts rather than silently winning.
    with pytest.raises(MemoryVersionConflict):
        store.write_file("pernix.notes", "anything", expected_revision=first.result.revision)


async def test_two_concurrent_saves_through_the_api_do_not_both_report_success(store):
    """The bug in the shape it was reported in, end to end.

    Both handlers are in flight before either write begins — the old check ran
    on the event loop, so both had already passed it. Measured against the old
    code this pair returned `{'saved': True}` twice and the first editor's
    text was not in the file afterwards.
    """
    from api.routers.memory import write_memory_file

    _seed(store)
    read = (await _get("/api/memory/files/pernix.notes")).json()

    first, second = await asyncio.gather(
        write_memory_file(
            "pernix.notes",
            {"content": read["content"] + "\nfirst editor\n", "base_revision": read["revision"]},
        ),
        write_memory_file(
            "pernix.notes",
            {"content": read["content"] + "\nsecond editor\n", "base_revision": read["revision"]},
        ),
    )

    codes = sorted(getattr(r, "status_code", 200) for r in (first, second))
    assert codes == [200, 409]

    final = store.read_file("pernix.notes")
    assert ("first editor" in final) != ("second editor" in final), "both drafts cannot be in the file"


# ---------------------------------------------------------------------------
# The read side
# ---------------------------------------------------------------------------


async def test_the_version_a_read_hands_out_describes_the_bytes_it_handed_out(store):
    _seed(store)
    data = (await _get("/api/memory/files/pernix.notes")).json()

    assert data["revision"] == _revision_of(data["content"].encode("utf-8"))
    assert data["mtime"] > 0

    # And it is immediately usable as a base — the pair was read as one.
    ok = await _put(
        "/api/memory/files/pernix.notes",
        {"content": data["content"] + "\nedited\n", "base_revision": data["revision"]},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["revision"] == _revision_of((data["content"] + "\nedited\n").encode("utf-8"))


async def test_a_rewrite_that_keeps_the_mtime_still_conflicts(store, tmp_path):
    """The case a float mtime cannot decide, which is why a digest exists.

    Restoring the mtime after a rewrite is contrived on purpose: it is the
    clean, deterministic form of two writes landing inside the millisecond of
    tolerance the mtime compare has to carry for the JSON round-trip. The
    legacy check accepts it — documented, and the reason the shipped editor
    now sends `base_revision` as well.
    """
    _seed(store)
    read = (await _get("/api/memory/files/pernix.notes")).json()

    path = tmp_path / "memories" / "pernix.notes.md"
    before = os.stat(path)
    store.write_file("pernix.notes", read["content"] + "\nwritten by the agent\n")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert os.stat(path).st_mtime == read["mtime"]

    late = await _put(
        "/api/memory/files/pernix.notes",
        {"content": read["content"] + "\nwhat the editor had\n", "base_revision": read["revision"]},
    )
    assert late.status_code == 409
    assert late.json()["detail"] == "changed_on_disk"
    assert late.json()["revision"] == _revision_of(store.read_file("pernix.notes").encode("utf-8"))
    assert "written by the agent" in store.read_file("pernix.notes")

    # The mtime-only contract cannot see it. Stated, not hidden.
    blind = await _put(
        "/api/memory/files/pernix.notes",
        {"content": "# clobbered\n", "base_mtime": read["mtime"]},
    )
    assert blind.status_code == 200


# ---------------------------------------------------------------------------
# The contract the shipped editor was already speaking
# ---------------------------------------------------------------------------


async def test_the_legacy_base_mtime_contract_still_conflicts_and_still_saves(store):
    _seed(store)
    read = (await _get("/api/memory/files/pernix.notes")).json()

    ok = await _put("/api/memory/files/pernix.notes", {"content": "# mine\n", "base_mtime": read["mtime"]})
    assert ok.status_code == 200
    assert ok.json()["saved"] is True
    assert ok.json()["mtime"] > 0

    late = await _put("/api/memory/files/pernix.notes", {"content": "# stale\n", "base_mtime": read["mtime"]})
    assert late.status_code == 409
    assert late.json()["detail"] == "changed_on_disk"
    assert store.read_file("pernix.notes") == "# mine\n"


async def test_a_save_with_no_base_version_is_still_last_writer_wins(store):
    """Overwrite is the editor's deliberate opt-out and has to keep working."""
    _seed(store)
    resp = await _put("/api/memory/files/pernix.notes", {"content": "# forced\n"})
    assert resp.status_code == 200
    assert store.read_file("pernix.notes") == "# forced\n"


async def test_a_garbled_base_version_is_rejected_not_ignored(store):
    _seed(store)
    read = (await _get("/api/memory/files/pernix.notes")).json()

    # A revision that is not a string is a client bug worth naming.
    bad = await _put("/api/memory/files/pernix.notes", {"content": "x", "base_revision": 17})
    assert bad.status_code == 400

    # A revision that is merely wrong is a conflict, not a 400.
    stale = await _put("/api/memory/files/pernix.notes", {"content": "x", "base_revision": "sha256:" + "0" * 32})
    assert stale.status_code == 409
    assert store.read_file("pernix.notes") == read["content"]
