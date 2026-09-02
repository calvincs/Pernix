"""Pernix — Memory search and file endpoints."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

router = APIRouter(tags=["memory"])

# Search paging. The browser used to ask for a fixed 10 and had no way to ask
# for the eleventh: a corpus of thousands of entries was a ten-row window with
# no edges marked. (S9)
_SEARCH_DEFAULT_LIMIT = 10
_SEARCH_MAX_LIMIT = 100
# Above this the client is paging through more than anyone reads; it also caps
# what one query can pull out of the store in a single pass.
_SEARCH_MAX_SCAN = 500

# Same tolerance the workspace and skill editors use, for the same reason: a
# float mtime round-tripped through JSON is not bit-identical.
MTIME_TOLERANCE_S = 0.001

_MAX_MEMORY_BYTES = 5 * 1024 * 1024


def _memory_path(name: str) -> Path:
    """The markdown file behind a memory name, or a 4xx.

    Mirrors MemoryStore._validate_name's rules rather than reaching into the
    store: a name is a single path segment of [A-Za-z0-9._-], and the resolved
    path must still be inside the memory directory.
    """
    import re

    from config import settings

    if name.endswith(".md"):
        name = name[:-3]
    if not name or not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$", name):
        raise HTTPException(400, detail=f"Invalid memory file name: {name}")
    root = Path(settings.memory_dir).resolve()
    path = (root / f"{name}.md").resolve()
    if not path.is_relative_to(root):
        raise HTTPException(403, detail="Path traversal blocked")
    return path


def _mtime_of(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


@router.get("/api/memory/files")
async def list_memory_files():
    from core.memory.store import get_memory_store

    store = get_memory_store()
    if not store:
        return {"files": [], "error": "Memory unavailable"}
    files = await asyncio.to_thread(store.list_files)
    # Space badges (v33): map pernix.space.<slug>.* files to their space's
    # label + color so the Explorer can chip them. Slug-keyed; a file whose
    # space row is gone (space deleted, files kept) shows the bare slug.
    from core.memory.routing import space_bucket
    from db import models as db

    space_by_slug = {s["slug"]: s for s in await asyncio.to_thread(db.list_spaces)}
    out = []
    for f in files:
        row = {
            "name": f.name,
            "description": f.description,
            "keywords": f.keywords,
            "entry_count": f.entry_count,
            # When a file last changed, so a long list can be sorted and read
            # by recency instead of only by name. (S9)
            "updated": f.updated_at,
            "created": f.created_at,
        }
        slug = space_bucket(f.name)
        if slug:
            sp = space_by_slug.get(slug)
            row["space"] = slug
            row["space_label"] = sp["label"] if sp else slug
            row["space_color"] = sp["color"] if sp else "#888888"
        out.append(row)
    return {"files": out, "total": len(out)}


@router.get("/api/memory/files/{name}")
async def read_memory_file(name: str):
    from core.memory.store import get_memory_store

    store = get_memory_store()
    if not store:
        return {"error": "Memory unavailable"}
    content = await asyncio.to_thread(store.read_file, name)
    if content is None:
        return {"error": f"File '{name}' not found"}
    # Handed back as base_mtime on save — the same optimistic-concurrency
    # contract the workspace and skill editors use. (S9)
    return {"name": name, "content": content, "mtime": _mtime_of(_memory_path(name))}


@router.put("/api/memory/files/{name}")
async def write_memory_file(name: str, body: dict):
    """Replace a memory file's markdown, then re-index it.

    Mirrors PUT /workspace/{path}: `base_mtime` is optional, and sending it
    turns last-writer-wins into a 409 when someone (the agent, a sweep) has
    rewritten the file since it was read. The response carries the new mtime
    so an editor can keep saving without re-reading.
    """
    from core.memory.store import get_memory_store

    store = get_memory_store()
    if not store:
        raise HTTPException(503, detail="Memory unavailable")

    path = _memory_path(name)
    content = body.get("content", "")
    if not isinstance(content, str):
        raise HTTPException(400, detail="content must be a string")
    if len(content.encode("utf-8")) > _MAX_MEMORY_BYTES:
        raise HTTPException(413, detail=f"Content too large (max {_MAX_MEMORY_BYTES} bytes)")
    if not path.is_file():
        raise HTTPException(404, detail=f"Memory file '{name}' not found")

    base_mtime = body.get("base_mtime")
    if base_mtime is not None:
        try:
            base = float(base_mtime)
        except (TypeError, ValueError):
            base = None
        if base is not None:
            current = _mtime_of(path)
            if abs(current - base) > MTIME_TOLERANCE_S:
                return JSONResponse(status_code=409, content={"detail": "changed_on_disk", "mtime": current})

    stem = path.stem

    # write_file is the store's public whole-file save: temp file + fsync +
    # rename, writers excluded, and the FTS re-index committed inside the same
    # lock. The index is derived from the markdown and has to follow it, or the
    # file a user just edited keeps matching searches on text it no longer
    # contains.
    await asyncio.to_thread(store.write_file, stem, content)
    return {"saved": True, "name": stem, "bytes": len(content), "mtime": _mtime_of(path)}


@router.get("/api/memory/search")
async def search_memory(
    q: str = "",
    after: int = 0,
    limit: int = _SEARCH_DEFAULT_LIMIT,
    offset: int = 0,
    space: str = "",
):
    """`space` (a slug) prioritizes that space's pernix.space.<slug>.* files.

    `limit`/`offset` page the ranked results; `has_more` says whether another
    page exists. There is no cheap exact `total` — the hybrid ranker fuses two
    result sets and stops at the scan cap — so the honest answer is the flag,
    not a number the next query would contradict.
    """
    from core.memory.store import get_memory_store

    store = get_memory_store()
    if not store:
        return {"results": [], "error": "Memory unavailable"}
    if not q:
        return {"results": [], "offset": 0, "limit": limit, "has_more": False, "returned": 0}

    limit = max(1, min(int(limit), _SEARCH_MAX_LIMIT))
    offset = max(0, int(offset))
    # One extra row is what tells a Load more button whether to exist.
    scan = min(offset + limit + 1, _SEARCH_MAX_SCAN)

    results = await asyncio.to_thread(
        lambda: store.search(q, limit=scan, after_epoch=after if after else None, space_slug=space or None)
    )
    page = results[offset : offset + limit]
    return {
        "results": [
            {
                "file": r.entry.file_name,
                "content": r.entry.content[:500],
                "score": round(r.score, 2),
                "epoch": r.entry.epoch,
                "type": r.entry.entry_type,
                "source": r.source,
            }
            for r in page
        ],
        "offset": offset,
        "limit": limit,
        "returned": len(page),
        "has_more": len(results) > offset + len(page),
    }


@router.post("/api/memory/maintenance")
async def memory_maintenance():
    from core.memory.store import get_memory_store

    store = get_memory_store()
    if not store:
        return {"error": "Memory unavailable"}
    result = await asyncio.to_thread(store.health_check, fix=True)
    return result
