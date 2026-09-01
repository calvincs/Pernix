"""Pernix — Spaces: CRUD + per-space directive overrides (plan v33).

The router owns delete orchestration (detach vs cascade) because it is the
only place that sees every space-keyed artifact at once: sessions, memory
files, workspace folder, directive overrides, bound cron jobs, and the
shared kernel.
"""

from __future__ import annotations

import asyncio as _asyncio
import logging
import re
import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException

from core import spaces as spaces_lib
from db import models as db

logger = logging.getLogger("pernix.api.spaces")

router = APIRouter(tags=["spaces"])

_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_DEFAULT_COLOR = "#7c9cff"


def _validate_color(color: str) -> str:
    color = (color or "").strip() or _DEFAULT_COLOR
    if not _COLOR_RE.match(color):
        raise HTTPException(400, detail="color must be a #rrggbb hex value")
    return color.lower()


@router.get("/api/spaces")
async def list_spaces():
    return {"items": await _asyncio.to_thread(db.list_spaces)}


@router.post("/api/spaces")
async def create_space(body: dict = {}):
    label = str(body.get("label") or "").strip()[:120]
    if not label:
        raise HTTPException(400, detail="label is required")
    color = _validate_color(str(body.get("color") or ""))
    try:
        slug = spaces_lib.slugify(label)
    except ValueError as e:
        raise HTTPException(400, detail=str(e)) from e
    if await _asyncio.to_thread(db.get_space_by_slug, slug):
        raise HTTPException(409, detail=f"a space with slug '{slug}' already exists")
    space = await _asyncio.to_thread(db.create_space, label, color, slug)
    spaces_lib.invalidate_space_cache()
    # The workspace home is part of the space's contract — create it now so
    # the Explorer shows the folder before any session writes to it.
    try:
        spaces_lib.space_workspace_home(space).mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning("workspace home creation failed for space %s: %s", slug, e)
    return space


@router.patch("/api/spaces/{space_id}")
async def update_space(space_id: str, body: dict = {}):
    space = await _asyncio.to_thread(db.get_space, space_id)
    if not space:
        raise HTTPException(404, detail=f"Space {space_id} not found")
    updates: dict = {}
    if "label" in body:
        label = str(body["label"] or "").strip()[:120]
        if not label:
            raise HTTPException(400, detail="label must be non-empty")
        updates["label"] = label
    if "color" in body:
        updates["color"] = _validate_color(str(body["color"] or ""))
    if "sort_order" in body:
        try:
            updates["sort_order"] = int(body["sort_order"])
        except (TypeError, ValueError) as e:
            raise HTTPException(400, detail="sort_order must be an integer") from e
    if updates:
        await _asyncio.to_thread(db.update_space, space_id, **updates)
        spaces_lib.invalidate_space_cache()
    return await _asyncio.to_thread(db.get_space, space_id)


@router.delete("/api/spaces/{space_id}")
async def delete_space(space_id: str, cascade: bool = False):
    """Delete a space. cascade=false (default) detaches: sessions return to
    the ungrouped list, memory files/workspace folder stay, jobs unbind.
    cascade=true deletes sessions, space memory files, the workspace folder
    and bound jobs. Directive overrides and the shared kernel are space
    config, not user artifacts — they go in both modes."""
    from sessions.manager import get_manager

    space = await _asyncio.to_thread(db.get_space, space_id)
    if not space:
        raise HTTPException(404, detail=f"Space {space_id} not found")
    slug = space["slug"]
    manager = get_manager()
    result: dict = {"space_id": space_id, "cascade": cascade}

    if cascade:
        session_ids = await _asyncio.to_thread(db.list_space_session_ids, space_id)
        for sid in session_ids:
            try:
                await _asyncio.to_thread(manager.delete_session, sid)
            except Exception as e:
                logger.warning("cascade: session %s delete failed: %s", sid[:12], e)
        result["sessions_deleted"] = len(session_ids)

        from core.memory.store import get_memory_store

        store = get_memory_store()
        prefix = spaces_lib.space_memory_prefix(space)
        deleted_files = 0
        for f in await _asyncio.to_thread(store.list_files):
            if f.name.startswith(prefix):
                try:
                    await _asyncio.to_thread(store.delete_file, f.name)
                    deleted_files += 1
                except Exception as e:
                    logger.warning("cascade: memory file %s delete failed: %s", f.name, e)
        result["memory_files_deleted"] = deleted_files

        home = spaces_lib.space_workspace_home(space)
        if home.exists():
            await _asyncio.to_thread(shutil.rmtree, home, ignore_errors=True)

        try:
            from core.extensions.scheduling import jobs_for_space, remove_scheduled_job

            names = jobs_for_space(space_id)
            for name in names:
                remove_scheduled_job(name)
            result["jobs_removed"] = len(names)
        except Exception as e:
            logger.warning("cascade: job removal failed: %s", e)
    else:
        detached = await _asyncio.to_thread(db.detach_space_sessions, space_id)
        # Live session objects must lose the space too, or they keep the
        # old home/kernel until restart.
        for sid in list(manager._sessions):
            live = manager.get(sid)
            if live is not None and live.space_id == space_id:
                live.space_id = None
                live.workspace_home = None
        result["sessions_detached"] = detached
        try:
            from core.extensions.scheduling import unbind_space_jobs

            result["jobs_unbound"] = unbind_space_jobs(space_id)
        except Exception as e:
            logger.warning("detach: job unbind failed: %s", e)

    # Both modes: shared kernel + directive overrides go with the space.
    try:
        from core.kernel import get_kernel_registry

        get_kernel_registry().shutdown_session_detached(f"space-{space_id}", snapshot=False, purge_state=True)
    except Exception as e:
        logger.debug("space kernel shutdown skipped: %s", e)
    agent_dir = spaces_lib.space_agent_dir(space)
    if agent_dir.exists():
        await _asyncio.to_thread(shutil.rmtree, agent_dir, ignore_errors=True)

    await _asyncio.to_thread(db.delete_space, space_id)
    spaces_lib.invalidate_space_cache()
    logger.info("Deleted space %s (%s), cascade=%s", slug, space_id, cascade)
    return result


# ---------------------------------------------------------------------------
# Directive overrides
# ---------------------------------------------------------------------------

# Fixed enum — the path segment never touches the filesystem un-validated.
_DIRECTIVE_FILES = {"SOUL": "SOUL.md", "RULES": "RULES.md", "SESSIONS": "SESSIONS.md"}
_MAX_DIRECTIVE_BYTES = 64_000


def _default_directive_content(fname: str) -> str:
    """The shared default the editor shows read-only. Mirrors the compiler's
    SESSIONS.md-or-INSTRUCTIONS.md first-wins rule for the SESSIONS slot."""
    base = Path("data/agent")
    candidates = [fname] if fname != "SESSIONS.md" else ["SESSIONS.md", "INSTRUCTIONS.md"]
    for cand in candidates:
        p = base / cand
        if p.exists():
            try:
                return p.read_text(encoding="utf-8")
            except OSError:
                return ""
    return ""


def _resolve_directive(space_id: str, name: str) -> tuple[dict, str]:
    space = db.get_space(space_id)
    if not space:
        raise HTTPException(404, detail=f"Space {space_id} not found")
    fname = _DIRECTIVE_FILES.get(name.upper())
    if not fname:
        raise HTTPException(400, detail=f"unknown directive {name!r}; use one of {sorted(_DIRECTIVE_FILES)}")
    return space, fname


@router.get("/api/spaces/{space_id}/directives")
async def get_directives(space_id: str):
    space = await _asyncio.to_thread(db.get_space, space_id)
    if not space:
        raise HTTPException(404, detail=f"Space {space_id} not found")
    agent_dir = spaces_lib.space_agent_dir(space)
    files = {}
    for key, fname in _DIRECTIVE_FILES.items():
        override_path = agent_dir / fname
        override = None
        if override_path.exists():
            try:
                override = override_path.read_text(encoding="utf-8")
            except OSError:
                override = ""
        files[key] = {
            "default": await _asyncio.to_thread(_default_directive_content, fname),
            "override": override,
        }
    return {"space_id": space_id, "files": files}


@router.put("/api/spaces/{space_id}/directives/{name}")
async def put_directive(space_id: str, name: str, body: dict = {}):
    space, fname = _resolve_directive(space_id, name)
    content = body.get("content")
    if not isinstance(content, str) or not content.strip():
        raise HTTPException(400, detail="content must be a non-empty string")
    if len(content.encode("utf-8")) > _MAX_DIRECTIVE_BYTES:
        raise HTTPException(400, detail=f"content exceeds {_MAX_DIRECTIVE_BYTES} bytes")
    agent_dir = spaces_lib.space_agent_dir(space)
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / fname).write_text(content, encoding="utf-8")
    return {"space_id": space_id, "file": name.upper(), "override": True}


@router.delete("/api/spaces/{space_id}/directives/{name}")
async def delete_directive(space_id: str, name: str):
    """Revert to default: remove the override file."""
    space, fname = _resolve_directive(space_id, name)
    path = spaces_lib.space_agent_dir(space) / fname
    if path.exists():
        path.unlink()
    return {"space_id": space_id, "file": name.upper(), "override": False}
