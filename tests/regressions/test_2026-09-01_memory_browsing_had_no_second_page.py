"""Memory search was a fixed ten-row window with no way past it.

`GET /api/memory/search` took a `limit` and nothing else: no offset, no
signal that an eleventh match existed. On a corpus of thousands of entries
the Explorer showed ten rows and gave no hint there was more, and the file
list carried no timestamp to sort by.

And a memory file was read-only over the API — the Explorer's viewer had no
edit affordance because there was no endpoint behind one. `PUT
/api/memory/files/{name}` now mirrors the workspace editor's contract, 409
included, and re-indexes so search stops matching text the file no longer
contains.

Covers: paging (limit/offset/has_more), the file list's `updated`, the read
endpoint's `mtime`, and the write endpoint's save / conflict / traversal
behaviour.

The version half of that contract grew a second, exact token in 2026-09-08:
`revision` is a digest of the bytes the read handed out, and `base_revision`
is compared inside the store's mutation lock instead of in the handler. The
sequential cases live here beside the mtime ones they extend; the concurrent
ones, where the old boundary actually failed, are in
test_2026-09-08_two_memory_saves_both_said_they_won.py.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


def _app():
    from api.routers import memory

    app = FastAPI()
    app.include_router(memory.router)
    return app


@pytest.fixture
def store(monkeypatch, tmp_path):
    """A real MemoryStore on a temp dir, wired in as the singleton."""
    from core.memory.store import MemoryStore

    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    st = MemoryStore(memory_dir=str(tmp_path / "memories"))
    monkeypatch.setattr("core.memory.store.get_memory_store", lambda: st)
    return st


async def _get(url):
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as c:
        return await c.get(url)


async def _put(url, body):
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as c:
        return await c.put(url, json=body)


def _seed(store, n=25, word="borogove"):
    for i in range(n):
        store.add_entry(f"entry {i} about {word} number {i}", file_name="pernix.paging")


async def test_search_pages_with_limit_and_offset(store):
    _seed(store)

    first = (await _get("/api/memory/search?q=borogove&limit=5")).json()
    assert first["limit"] == 5
    assert first["offset"] == 0
    assert len(first["results"]) == 5
    assert first["has_more"] is True

    second = (await _get("/api/memory/search?q=borogove&limit=5&offset=5")).json()
    assert second["offset"] == 5
    assert len(second["results"]) == 5
    # A second page is a different page.
    assert [r["content"] for r in second["results"]] != [r["content"] for r in first["results"]]


async def test_has_more_is_false_on_the_last_page(store):
    _seed(store, n=6)
    page = (await _get("/api/memory/search?q=borogove&limit=5&offset=5")).json()
    assert page["returned"] == 1
    assert page["has_more"] is False


async def test_limit_is_clamped_to_the_documented_maximum(store):
    from api.routers.memory import _SEARCH_MAX_LIMIT

    _seed(store, n=3)
    data = (await _get("/api/memory/search?q=borogove&limit=99999")).json()
    assert data["limit"] == _SEARCH_MAX_LIMIT
    # A negative offset is a caller bug, not a reason to 500.
    assert (await _get("/api/memory/search?q=borogove&offset=-4")).json()["offset"] == 0


async def test_empty_query_still_answers_with_the_paging_shape(store):
    data = (await _get("/api/memory/search?q=")).json()
    assert data["results"] == []
    assert data["has_more"] is False


async def test_file_list_carries_entry_count_and_updated(store):
    _seed(store, n=3)
    files = (await _get("/api/memory/files")).json()["files"]
    row = next(f for f in files if f["name"] == "pernix.paging")
    assert row["entry_count"] == 3
    assert isinstance(row["updated"], int) and row["updated"] > 0


async def test_read_returns_an_mtime_to_save_against(store):
    _seed(store, n=1)
    data = (await _get("/api/memory/files/pernix.paging")).json()
    assert data["content"]
    assert data["mtime"] > 0


async def test_put_writes_and_reindexes(store):
    _seed(store, n=1, word="borogove")
    read = (await _get("/api/memory/files/pernix.paging")).json()

    body = read["content"].replace("borogove", "toves")
    resp = await _put("/api/memory/files/pernix.paging", {"content": body, "base_mtime": read["mtime"]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["saved"] is True

    again = (await _get("/api/memory/files/pernix.paging")).json()
    assert "toves" in again["content"]
    assert "borogove" not in again["content"]
    # The index is derived from the markdown, so it has to have followed it.
    # (The hybrid ranker still answers a no-match query with its nearest rows,
    # so the check that matters is that no indexed row carries the old text.)
    hits = (await _get("/api/memory/search?q=toves")).json()["results"]
    assert hits and any("toves" in h["content"] for h in hits)
    stale = (await _get("/api/memory/search?q=borogove")).json()["results"]
    assert not any("borogove" in h["content"] for h in stale)


async def test_put_409s_when_the_file_moved_under_the_editor(store):
    _seed(store, n=1)
    read = (await _get("/api/memory/files/pernix.paging")).json()

    # Somebody else writes first.
    ok = await _put("/api/memory/files/pernix.paging", {"content": "# rewritten by the agent\n"})
    assert ok.status_code == 200

    late = await _put(
        "/api/memory/files/pernix.paging",
        {"content": "# what the editor had\n", "base_mtime": read["mtime"]},
    )
    assert late.status_code == 409
    assert late.json()["detail"] == "changed_on_disk"
    assert late.json()["mtime"] > 0
    # Refused, not partially applied.
    assert "rewritten by the agent" in (await _get("/api/memory/files/pernix.paging")).json()["content"]


async def test_put_without_base_mtime_keeps_last_writer_wins(store):
    _seed(store, n=1)
    resp = await _put("/api/memory/files/pernix.paging", {"content": "# forced\n"})
    assert resp.status_code == 200
    assert "forced" in (await _get("/api/memory/files/pernix.paging")).json()["content"]


async def test_read_also_returns_an_exact_revision_to_save_against(store):
    _seed(store, n=1)
    data = (await _get("/api/memory/files/pernix.paging")).json()
    assert isinstance(data["revision"], str) and data["revision"].startswith("sha256:")
    # Same bytes, same token — a read is repeatable while nothing writes.
    again = (await _get("/api/memory/files/pernix.paging")).json()
    assert again["revision"] == data["revision"]


async def test_put_409s_on_a_stale_revision_and_keeps_the_other_writer(store):
    _seed(store, n=1)
    read = (await _get("/api/memory/files/pernix.paging")).json()

    ok = await _put("/api/memory/files/pernix.paging", {"content": "# rewritten by the agent\n"})
    assert ok.status_code == 200

    late = await _put(
        "/api/memory/files/pernix.paging",
        {"content": "# what the editor had\n", "base_revision": read["revision"]},
    )
    assert late.status_code == 409
    assert late.json()["detail"] == "changed_on_disk"
    # The 409 names the version that IS there, both ways, so a client can say
    # what it would have overwritten without going and looking (another race).
    assert late.json()["revision"] == ok.json()["revision"]
    assert late.json()["mtime"] > 0
    assert "rewritten by the agent" in (await _get("/api/memory/files/pernix.paging")).json()["content"]


async def test_a_save_hands_back_a_version_the_next_save_can_use(store):
    """An editor that keeps saving must not have to re-read between saves."""
    _seed(store, n=1)
    read = (await _get("/api/memory/files/pernix.paging")).json()

    first = await _put("/api/memory/files/pernix.paging", {"content": "# one\n", "base_revision": read["revision"]})
    assert first.status_code == 200

    second = await _put(
        "/api/memory/files/pernix.paging",
        {"content": "# two\n", "base_revision": first.json()["revision"]},
    )
    assert second.status_code == 200
    assert (await _get("/api/memory/files/pernix.paging")).json()["content"] == "# two\n"

    # And the version it superseded is now stale, as it must be.
    stale = await _put(
        "/api/memory/files/pernix.paging",
        {"content": "# three\n", "base_revision": first.json()["revision"]},
    )
    assert stale.status_code == 409


async def test_a_revision_wins_over_a_stale_mtime_sent_beside_it(store):
    """The shipped editor sends both. The exact one decides."""
    _seed(store, n=1)
    read = (await _get("/api/memory/files/pernix.paging")).json()

    resp = await _put(
        "/api/memory/files/pernix.paging",
        {"content": "# saved\n", "base_revision": read["revision"], "base_mtime": read["mtime"] - 500},
    )
    assert resp.status_code == 200, resp.text
    assert (await _get("/api/memory/files/pernix.paging")).json()["content"] == "# saved\n"


async def test_put_refuses_traversal_and_unknown_files(store):
    assert (await _put("/api/memory/files/..%2F..%2Fetc%2Fpasswd", {"content": "x"})).status_code in (400, 403, 404)
    assert (await _put("/api/memory/files/pernix.nope", {"content": "x"})).status_code == 404
