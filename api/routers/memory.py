"""Pernix — Memory search and file endpoints."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["memory"])


@router.get("/api/memory/files")
async def list_memory_files():
    from core.memory.store import get_memory_store

    store = get_memory_store()
    if not store:
        return {"files": [], "error": "Memory unavailable"}
    files = store.list_files()
    return {
        "files": [
            {"name": f.name, "description": f.description, "keywords": f.keywords, "entry_count": f.entry_count}
            for f in files
        ]
    }


@router.get("/api/memory/files/{name}")
async def read_memory_file(name: str):
    from core.memory.store import get_memory_store

    store = get_memory_store()
    if not store:
        return {"error": "Memory unavailable"}
    content = store.read_file(name)
    if content is None:
        return {"error": f"File '{name}' not found"}
    return {"name": name, "content": content}


@router.get("/api/memory/search")
async def search_memory(q: str = "", after: int = 0, limit: int = 5):
    from core.memory.store import get_memory_store

    store = get_memory_store()
    if not store:
        return {"results": [], "error": "Memory unavailable"}
    if not q:
        return {"results": []}
    results = store.search(q, limit=limit, after_epoch=after if after else None)
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
    result = store.health_check(fix=True)
    return result
