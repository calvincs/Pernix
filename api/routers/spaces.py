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
                # delete_session_async, not to_thread(delete_session): the
                # first half of a delete is loop-affine (Task.cancel, the
                # process sweep, popping the in-memory session) and running
                # it on a worker thread let the DB row go while the turn was
                # still running against it. The async form keeps that half on
                # the loop and only threads the DB/filesystem work.
                await manager.delete_session_async(sid)
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

            names = await _asyncio.to_thread(jobs_for_space, space_id)
            # remove_scheduled_job rewrites cron_jobs.json under a threading
            # lock shared with tool threads — a file write on the loop.
            for name in names:
                await _asyncio.to_thread(remove_scheduled_job, name)
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

            result["jobs_unbound"] = await _asyncio.to_thread(unbind_space_jobs, space_id)
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
    """Sync: callers dispatch it via to_thread (it reads the DB)."""
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

        def _read_override(path=override_path):
            if not path.exists():
                return None
            try:
                return path.read_text(encoding="utf-8")
            except OSError:
                return ""

        override = await _asyncio.to_thread(_read_override)
        files[key] = {
            "default": await _asyncio.to_thread(_default_directive_content, fname),
            "override": override,
        }
    return {"space_id": space_id, "files": files}


@router.put("/api/spaces/{space_id}/directives/{name}")
async def put_directive(space_id: str, name: str, body: dict = {}):
    space, fname = await _asyncio.to_thread(_resolve_directive, space_id, name)
    content = body.get("content")
    if not isinstance(content, str) or not content.strip():
        raise HTTPException(400, detail="content must be a non-empty string")
    if len(content.encode("utf-8")) > _MAX_DIRECTIVE_BYTES:
        raise HTTPException(400, detail=f"content exceeds {_MAX_DIRECTIVE_BYTES} bytes")
    agent_dir = spaces_lib.space_agent_dir(space)

    def _write():
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / fname).write_text(content, encoding="utf-8")

    await _asyncio.to_thread(_write)
    return {"space_id": space_id, "file": name.upper(), "override": True}


@router.delete("/api/spaces/{space_id}/directives/{name}")
async def delete_directive(space_id: str, name: str):
    """Revert to default: remove the override file."""
    space, fname = await _asyncio.to_thread(_resolve_directive, space_id, name)
    path = spaces_lib.space_agent_dir(space) / fname
    await _asyncio.to_thread(path.unlink, True)
    return {"space_id": space_id, "file": name.upper(), "override": False}


# ---------------------------------------------------------------------------
# Space suggestions (v35) — proposals the user accepts or declines
# ---------------------------------------------------------------------------
#
# A distinct prefix, deliberately: /api/spaces/{space_id} is already declared
# above and would swallow /api/spaces/suggestions as a space id.

_SUGGESTION_STATUSES = ("pending", "accepted", "rejected", "expired")
# pending is refused for the bulk clear: it is the one status the user has
# not decided yet, and "clear all" is a tidying gesture, not a decision.
_CLEARABLE_STATUSES = ("accepted", "rejected", "expired")


def _resolve_members(row: dict) -> list[dict]:
    """The suggestion's sessions as the review sheet needs them.

    Ids whose session was deleted since the scan drop out — a suggestion is
    a proposal, not a reference the sessions table has to keep whole.
    """
    out = []
    for sid in row.get("session_ids") or []:
        session = db.get_session(sid)
        if not session:
            continue
        out.append(
            {
                "id": session["id"],
                "title": session.get("title") or "",
                "subtitle": session.get("subtitle") or "",
                "updated_at": session.get("updated_at"),
                "space_id": session.get("space_id"),
            }
        )
    return out


def _enrich_suggestion(row: dict) -> dict:
    """Sync: callers dispatch it via to_thread (it reads the DB)."""
    out = dict(row)
    out["sessions"] = _resolve_members(row)
    space = db.get_space(row["existing_space_id"]) if row.get("existing_space_id") else None
    out["existing_space"] = {"id": space["id"], "label": space["label"], "color": space["color"]} if space else None
    return out


def _validate_directive_payload(raw) -> dict[str, str]:
    """Body directives are FULL file contents the user may have edited in
    the sheet, so they get put_directive's validation — they land in exactly
    the same files. Returns {filename: content}."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise HTTPException(400, detail="directives must be an object of {name: content}")
    out: dict[str, str] = {}
    for name, content in raw.items():
        key = str(name).upper()
        fname = _DIRECTIVE_FILES.get(key)
        if not fname:
            raise HTTPException(400, detail=f"unknown directive {name!r}; use one of {sorted(_DIRECTIVE_FILES)}")
        if not isinstance(content, str) or not content.strip():
            raise HTTPException(400, detail=f"{key} content must be a non-empty string")
        if len(content.encode("utf-8")) > _MAX_DIRECTIVE_BYTES:
            raise HTTPException(400, detail=f"{key} content exceeds {_MAX_DIRECTIVE_BYTES} bytes")
        out[fname] = content
    return out


def _write_directives(space: dict, files: dict[str, str]) -> None:
    agent_dir = spaces_lib.space_agent_dir(space)
    agent_dir.mkdir(parents=True, exist_ok=True)
    for fname, content in files.items():
        (agent_dir / fname).write_text(content, encoding="utf-8")


def _apply_live_space(session_id: str, space_id: str) -> None:
    """Mirror a move onto the in-memory session, exactly as the session
    PATCH does — the same helper, not a copy of it. Without this a loaded
    session keeps its old workspace home and kernel until a restart."""
    try:
        from sessions.manager import _apply_space_fields, get_manager

        live = get_manager().get(session_id)
        if live is None:
            return
        live.space_id = None
        live.workspace_home = None
        _apply_space_fields(live, space_id)
    except Exception as e:
        logger.warning("live space update failed for %s: %s", session_id[:12], e)


@router.get("/api/space-suggestions")
async def list_space_suggestions(status: str = "pending"):
    status = (status or "pending").strip().lower()
    if status != "all" and status not in _SUGGESTION_STATUSES:
        raise HTTPException(400, detail=f"status must be one of {sorted((*_SUGGESTION_STATUSES, 'all'))}")

    def _load():
        rows = db.list_space_suggestions(None if status == "all" else status)
        return [_enrich_suggestion(r) for r in rows]

    return {"suggestions": await _asyncio.to_thread(_load), "status": status}


@router.post("/api/space-suggestions/scan")
async def scan_space_suggestions(body: dict = {}):
    """Run a scan now. dry_run (the default) proposes without storing, so
    the settings pane can show what a scan would find before committing."""
    from core import space_suggest

    if space_suggest.scan_running():
        raise HTTPException(409, detail="a space-suggestion scan is already running")
    dry_run = body.get("dry_run", True)
    return await space_suggest.scan(force=True, dry_run=bool(dry_run))


@router.get("/api/space-suggestions/{suggestion_id}")
async def get_space_suggestion(suggestion_id: str):
    def _load():
        row = db.get_space_suggestion(suggestion_id)
        if not row:
            return None
        out = _enrich_suggestion(row)
        drafts = out.get("directives") or {}
        enriched: dict = {}
        for name, entry in drafts.items():
            key = str(name).upper()
            fname = _DIRECTIVE_FILES.get(key)
            if not fname or not isinstance(entry, dict):
                continue
            # The sheet shows the default read-only next to the draft: the
            # addition is APPENDED to it, so the reader has to see both.
            enriched[key] = {**entry, "default": _default_directive_content(fname)}
        out["directives"] = enriched or None
        return out

    row = await _asyncio.to_thread(_load)
    if row is None:
        raise HTTPException(404, detail=f"Suggestion {suggestion_id} not found")
    return row


@router.post("/api/space-suggestions/{suggestion_id}/accept")
async def accept_space_suggestion(suggestion_id: str, body: dict = {}):
    """The click that makes a suggestion real: create the space (or reuse
    the named one), write whichever directive files the sheet sent, and move
    the chosen members. A move that fails is reported, not rolled back —
    half a filing is better than an error that undoes the space too."""
    row = await _asyncio.to_thread(db.get_space_suggestion, suggestion_id)
    if not row:
        raise HTTPException(404, detail=f"Suggestion {suggestion_id} not found")
    if row["status"] != "pending":
        raise HTTPException(409, detail=f"suggestion {suggestion_id} is {row['status']}, not pending")

    directives = _validate_directive_payload(body.get("directives"))

    if row["kind"] == "existing":
        target = row.get("existing_space_id") or ""
        space = await _asyncio.to_thread(db.get_space, target)
        if not space:
            # The space was deleted between the scan and the click. The
            # suggestion can never be accepted now, so retire it.
            await _asyncio.to_thread(db.set_space_suggestion_status, suggestion_id, "expired")
            raise HTTPException(409, detail=f"Space {target} no longer exists; the suggestion has been expired")
    else:
        label = str(body.get("label") or row["label"]).strip()[:120]
        if not label:
            raise HTTPException(400, detail="label is required")
        color = _validate_color(str(body.get("color") or row["color"]))
        try:
            slug = spaces_lib.slugify(label)
        except ValueError as e:
            raise HTTPException(400, detail=str(e)) from e
        if await _asyncio.to_thread(db.get_space_by_slug, slug):
            # The detail names the slug so the sheet can ask for another name.
            raise HTTPException(409, detail=f"a space with slug '{slug}' already exists")
        space = await _asyncio.to_thread(db.create_space, label, color, slug)
        spaces_lib.invalidate_space_cache()
        target = space["id"]
        try:
            spaces_lib.space_workspace_home(space).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("workspace home creation failed for space %s: %s", slug, e)
        # Only a brand-new space gets directive files written: an existing
        # space may already carry hand-edited overrides, and an accept must
        # never silently replace them.
        if directives:
            await _asyncio.to_thread(_write_directives, space, directives)

    members = list(row.get("session_ids") or [])
    requested = body.get("session_ids")
    # Ids outside the suggestion's members are ignored — the body chooses
    # which members to move, it does not extend the proposal.
    wanted = [str(s) for s in requested if str(s) in members] if isinstance(requested, list) else members

    moved = 0
    failed: list[str] = []
    for sid in wanted:
        if not await _asyncio.to_thread(db.get_session, sid):
            failed.append(sid)
            continue
        try:
            # set_session_meta, not update_session: filing a chat must not
            # change its recency, or every accepted suggestion would drag
            # its members to the top of Today.
            await _asyncio.to_thread(db.set_session_meta, sid, space_id=target)
        except Exception as e:
            logger.warning("suggestion %s: moving session %s failed: %s", suggestion_id[:8], sid[:12], e)
            failed.append(sid)
            continue
        moved += 1
        _apply_live_space(sid, target)

    await _asyncio.to_thread(db.set_space_suggestion_status, suggestion_id, "accepted", space_id=target)
    spaces_lib.invalidate_space_cache()
    logger.info(
        "Accepted space suggestion %s (%s) into space %s: %d moved, %d failed",
        suggestion_id,
        row["topic_key"],
        target,
        moved,
        len(failed),
    )
    return {
        "status": "accepted",
        "space": await _asyncio.to_thread(db.get_space, target),
        "moved": moved,
        "failed": failed,
    }


@router.post("/api/space-suggestions/{suggestion_id}/reject")
async def reject_space_suggestion(suggestion_id: str):
    """Decline. The row stays: the topic is what suppresses the same
    grouping next month, and the user can clear it to re-arm it."""
    row = await _asyncio.to_thread(db.get_space_suggestion, suggestion_id)
    if not row:
        raise HTTPException(404, detail=f"Suggestion {suggestion_id} not found")
    if row["status"] != "pending":
        raise HTTPException(409, detail=f"suggestion {suggestion_id} is {row['status']}, not pending")
    await _asyncio.to_thread(db.set_space_suggestion_status, suggestion_id, "rejected")
    return {"status": "rejected"}


@router.delete("/api/space-suggestions")
async def clear_space_suggestions(status: str = "rejected"):
    status = (status or "").strip().lower()
    if status == "pending":
        raise HTTPException(400, detail="pending suggestions are cleared by accepting or declining them")
    if status not in _CLEARABLE_STATUSES:
        raise HTTPException(400, detail=f"status must be one of {sorted(_CLEARABLE_STATUSES)}")
    cleared = await _asyncio.to_thread(db.delete_space_suggestions_by_status, status)
    return {"cleared": cleared, "status": status}


@router.delete("/api/space-suggestions/{suggestion_id}")
async def delete_space_suggestion(suggestion_id: str):
    """Forget one row whatever its status. Clearing a declined topic re-arms
    it: the scan may propose that grouping again."""
    if not await _asyncio.to_thread(db.delete_space_suggestion, suggestion_id):
        raise HTTPException(404, detail=f"Suggestion {suggestion_id} not found")
    return {"cleared": 1}
