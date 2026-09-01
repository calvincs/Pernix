"""Pernix — Memory search and file endpoints."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter

router = APIRouter(tags=["memory"])


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
        row = {"name": f.name, "description": f.description, "keywords": f.keywords, "entry_count": f.entry_count}
        slug = space_bucket(f.name)
        if slug:
            sp = space_by_slug.get(slug)
            row["space"] = slug
            row["space_label"] = sp["label"] if sp else slug
            row["space_color"] = sp["color"] if sp else "#888888"
        out.append(row)
    return {"files": out}


@router.get("/api/memory/files/{name}")
async def read_memory_file(name: str):
    from core.memory.store import get_memory_store

    store = get_memory_store()
    if not store:
        return {"error": "Memory unavailable"}
    content = await asyncio.to_thread(store.read_file, name)
    if content is None:
        return {"error": f"File '{name}' not found"}
    return {"name": name, "content": content}


@router.get("/api/memory/search")
async def search_memory(q: str = "", after: int = 0, limit: int = 5, space: str = ""):
    """`space` (a slug) prioritizes that space's pernix.space.<slug>.* files."""
    from core.memory.store import get_memory_store

    store = get_memory_store()
    if not store:
        return {"results": [], "error": "Memory unavailable"}
    if not q:
        return {"results": []}
    results = await asyncio.to_thread(
        lambda: store.search(q, limit=limit, after_epoch=after if after else None, space_slug=space or None)
    )
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
            for r in results
        ]
    }


@router.post("/api/memory/maintenance")
async def memory_maintenance():
    from core.memory.store import get_memory_store

    store = get_memory_store()
    if not store:
        return {"error": "Memory unavailable"}
    result = await asyncio.to_thread(store.health_check, fix=True)
    return result
