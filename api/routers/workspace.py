"""Pernix — Workspace file management and upload endpoints."""

from __future__ import annotations

import os
import shutil
import unicodedata
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from config import settings

router = APIRouter(tags=["workspace"])

MAX_SEARCH_RESULTS = 50

BLOCKED_EXTENSIONS = {".exe", ".sh", ".php", ".bat", ".cmd", ".com", ".scr", ".msi", ".dll"}
MAX_UPLOAD_SIZE = 250 * 1024 * 1024  # 250MB

# Slack for the mtime comparison in a conditional PUT. Filesystem timestamp
# resolution and the round-trip through JSON both cost precision, and a
# sub-second difference is never a real second writer.
MTIME_TOLERANCE_S = 0.5

_CONTENT_TYPES = {
    ".html": "text/html",
    ".htm": "text/html",
    ".js": "application/javascript",
    ".ts": "application/javascript",
    ".css": "text/css",
    ".json": "application/json",
    ".py": "text/x-python",
    ".md": "text/markdown",
    ".svg": "image/svg+xml",
    ".xml": "application/xml",
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".yaml": "text/yaml",
    ".yml": "text/yaml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
}


@router.get("/api/workspace")
async def list_workspace(
    path: str = Query("", description="Directory path relative to workspace root"),
    q: str = Query("", description="Search query — substring match on file paths"),
):
    """List workspace directory contents or search for files.

    Without ``q``: returns immediate children (files + dirs) of ``path``.
    With ``q``: searches the full workspace tree for matching file paths.
    """
    workspace = Path(settings.workspace_dir).resolve()
    if not workspace.exists():
        return {"entries": [], "path": path, "parent": None}

    # ── Search mode ──────────────────────────────────────────────────
    if q:
        return _search_workspace(workspace, q.strip(), path)

    # ── Directory listing mode ───────────────────────────────────────
    target = (workspace / path).resolve() if path else workspace
    if not target.is_relative_to(workspace):
        raise HTTPException(403, detail="Path traversal blocked")
    if not target.exists() or not target.is_dir():
        raise HTTPException(404, detail=f"Directory not found: {path}")

    parent = None
    if path:
        parent_path = str(Path(path).parent)
        parent = "" if parent_path == "." else parent_path

    entries = []
    try:
        with os.scandir(target) as it:
            for entry in it:
                rel = os.path.relpath(entry.path, workspace)
                try:
                    if entry.is_dir(follow_symlinks=False):
                        child_count = 0
                        dir_size = 0
                        dir_mtime = 0.0
                        try:
                            dir_mtime = entry.stat(follow_symlinks=False).st_mtime
                            with os.scandir(entry.path) as sub_it:
                                for child in sub_it:
                                    child_count += 1
                                    if child.is_file(follow_symlinks=False):
                                        try:
                                            dir_size += child.stat(follow_symlinks=False).st_size
                                        except OSError:
                                            pass
                        except OSError:
                            pass
                        entries.append(
                            {
                                "name": entry.name,
                                "type": "dir",
                                "path": rel,
                                "children": child_count,
                                "size": dir_size,
                                "modified": dir_mtime,
                            }
                        )
                    elif entry.is_file(follow_symlinks=False):
                        st = entry.stat(follow_symlinks=False)
                        entries.append(
                            {
                                "name": entry.name,
                                "type": "file",
                                "path": rel,
                                "size": st.st_size,
                                "modified": st.st_mtime,
                            }
                        )
                except OSError:
                    continue
    except OSError:
        raise HTTPException(500, detail="Failed to read directory")

    # Sort: dirs first (alphabetical), then files (alphabetical)
    entries.sort(key=lambda e: (0 if e["type"] == "dir" else 1, e["name"].lower()))
    return {"entries": entries, "path": path, "parent": parent}


def _search_workspace(workspace: Path, query: str, scope: str) -> dict:
    """Search workspace files by substring match on path. Returns flat list."""
    q_lower = query.lower()
    results = []

    start_dir = workspace
    if scope:
        scoped = (workspace / scope).resolve()
        if scoped.is_relative_to(workspace) and scoped.is_dir():
            start_dir = scoped

    for dirpath, dirnames, filenames in os.walk(start_dir):
        dirnames.sort()
        for fname in filenames:
            fpath = os.path.join(dirpath, fname)
            rel = os.path.relpath(fpath, workspace)
            if q_lower in rel.lower():
                try:
                    st = os.lstat(fpath)
                    results.append(
                        {
                            "name": fname,
                            "type": "file",
                            "path": rel,
                            "size": st.st_size,
                            "modified": st.st_mtime,
                        }
                    )
                except OSError:
                    continue
                if len(results) >= MAX_SEARCH_RESULTS:
                    return {
                        "entries": results,
                        "path": scope,
                        "search": query,
                        "truncated": True,
                    }

    results.sort(key=lambda e: e["path"].lower())
    return {"entries": results, "path": scope, "search": query}


@router.get("/workspace/{path:path}")
async def serve_workspace_file(path: str):
    workspace = Path(settings.workspace_dir).resolve()
    file_path = (workspace / path).resolve()
    if not file_path.is_relative_to(workspace):
        raise HTTPException(403, detail="Path traversal blocked")
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(404, detail="File not found")
    media_type = _CONTENT_TYPES.get(file_path.suffix.lower())
    # The editor keeps this value and hands it back as base_mtime on save,
    # which is how a PUT can tell "nobody touched it" from "the agent
    # rewrote it while you were typing".
    try:
        mtime = file_path.stat().st_mtime
    except OSError:
        mtime = 0.0
    # Workspace files are agent- and upload-authored, i.e. untrusted. Served
    # bare, an .html/.svg document would execute on the app origin with the
    # auth token one `localStorage` read away. `sandbox` (no allow-* flags)
    # gives the document an opaque origin and disables scripts, so it still
    # previews but cannot reach the token, cookies, or the API. `nosniff`
    # stops a browser from promoting a .txt to HTML on content sniffing.
    return FileResponse(
        file_path,
        media_type=media_type,
        headers={
            "Content-Security-Policy": "sandbox",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "X-File-Mtime": f"{mtime:.6f}",
            "Access-Control-Expose-Headers": "X-File-Mtime",
        },
    )


def _stale(file_path: Path, base_mtime) -> float | None:
    """Current mtime when ``base_mtime`` is stale, else None.

    Optimistic concurrency, opt-in: a caller that omits ``base_mtime`` keeps
    the old last-writer-wins behaviour, so agent tools, curl and older clients
    are unaffected. Only the editor, which knows what it read, sends one.
    """
    if base_mtime is None or not file_path.is_file():
        return None
    try:
        base = float(base_mtime)
    except (TypeError, ValueError):
        return None
    try:
        current = file_path.stat().st_mtime
    except OSError:
        return None
    return current if abs(current - base) > MTIME_TOLERANCE_S else None


@router.put("/workspace/{path:path}")
async def save_workspace_file(path: str, body: dict):
    workspace = Path(settings.workspace_dir).resolve()
    file_path = (workspace / path).resolve()
    if not file_path.is_relative_to(workspace):
        raise HTTPException(403, detail="Path traversal blocked")
    content = body.get("content", "")
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(413, detail=f"Content too large ({len(content)} bytes, max {MAX_UPLOAD_SIZE})")
    changed = _stale(file_path, body.get("base_mtime"))
    if changed is not None:
        return JSONResponse(status_code=409, content={"detail": "changed_on_disk", "mtime": changed})
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content)
    return {"saved": True, "path": path, "bytes": len(content), "mtime": file_path.stat().st_mtime}


@router.delete("/workspace/{path:path}")
async def delete_workspace_entry(path: str):
    workspace = Path(settings.workspace_dir).resolve()
    target = (workspace / path).resolve()
    if not target.is_relative_to(workspace):
        raise HTTPException(403, detail="Path traversal blocked")
    if target == workspace:
        raise HTTPException(400, detail="Cannot delete workspace root")
    if not target.exists():
        raise HTTPException(404, detail="Not found")
    if target.is_file():
        target.unlink()
    elif target.is_dir():
        for dirpath, dirnames, filenames in os.walk(str(target)):
            for name in dirnames + filenames:
                entry = Path(dirpath) / name
                if entry.is_symlink() and not entry.resolve().is_relative_to(workspace):
                    raise HTTPException(
                        400, detail=f"Refusing to delete: external symlink at {entry.relative_to(workspace)}"
                    )
        shutil.rmtree(target)
    else:
        raise HTTPException(400, detail="Not a file or directory")
    return {"deleted": True, "path": path}


_SENSITIVE_FILES = {"settings.json", ".env", "secrets.json", "credentials.json"}


@router.get("/api/datafiles")
async def list_datafiles():
    data_dir = Path("data")
    if not data_dir.exists():
        return {"files": []}
    files = []
    for f in sorted(data_dir.iterdir()):
        if f.is_file() and f.suffix not in (".db", ".db-wal", ".db-shm") and f.name not in _SENSITIVE_FILES:
            files.append({"name": f.name, "size": f.stat().st_size})
    return {"files": files}


@router.post("/api/upload")
async def upload_file(file: UploadFile = File(...), path: str = Form("")):
    """Upload one file into the workspace.

    ``path`` is an optional directory relative to the workspace root — the
    folder the Explorer is currently showing. Omitted, uploads land at the
    root, which is the old behaviour and what every other caller does. It goes
    through the same traversal check as every other workspace route.
    """
    if not file.filename:
        raise HTTPException(400, detail="No filename")

    # Sanitize filename: normalize Unicode, strip control chars, block traversal
    filename = unicodedata.normalize("NFKC", file.filename)
    filename = "".join(c for c in filename if unicodedata.category(c) not in ("Cc", "Cf"))
    filename = filename.replace("/", "_").replace("\\", "_").replace("..", "_")

    # Check extension
    ext = Path(filename).suffix.lower()
    if ext in BLOCKED_EXTENSIONS:
        raise HTTPException(400, detail=f"Extension {ext} not allowed")

    # Read with size limit
    content = await file.read()
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(400, detail=f"File too large ({len(content)} bytes, max {MAX_UPLOAD_SIZE})")

    # Save to workspace
    workspace = Path(settings.workspace_dir)
    workspace.mkdir(parents=True, exist_ok=True)
    workspace = workspace.resolve()

    dest_dir = workspace
    if path:
        candidate = (workspace / path).resolve()
        if not candidate.is_relative_to(workspace):
            raise HTTPException(403, detail="Path traversal blocked")
        dest_dir = candidate
        dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename

    # Handle collision
    if dest.exists():
        stem = dest.stem
        for i in range(1, 101):
            dest = dest_dir / f"{stem}_{i}{ext}"
            if not dest.exists():
                break
        else:
            raise HTTPException(409, detail="Too many filename collisions")

    dest.write_bytes(content)
    rel = dest.relative_to(workspace).as_posix()
    return {"filename": dest.name, "path": rel, "size": len(content)}
