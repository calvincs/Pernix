"""RLM run history + live inspection endpoints (read-only).

Runs are created by the rlm_process tool (core/extensions/rlm); these routes
expose the rlm_runs audit index, on-disk manifests, and the per-run
trace.jsonl. The trace endpoint pages by byte offset — the engine appends and
flushes whole lines, so the file tails cleanly while a run is live and the
same endpoint serves post-hoc inspection. Listing works even when
rlm_enabled=false — historical runs remain inspectable after a toggle-off.
"""

import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException

from config import settings
from db import models as db

router = APIRouter()

# Cap on answer.txt bytes inlined into the detail payload.
_ANSWER_INLINE_LIMIT = 200_000


@router.get("/api/rlm/runs")
async def list_rlm_runs(session_id: str = "", limit: int = 20, space_id: str = ""):
    """List RLM runs, newest first. Filter by owning session, or by space
    (v33) — every member session's runs; space_id wins over session_id."""
    limit = max(1, min(int(limit), 100))
    runs = db.list_rlm_runs(session_id=session_id or None, limit=limit, space_id=space_id or None)
    return {"runs": runs, "rlm_enabled": settings.rlm_enabled}


@router.get("/api/rlm/runs/by-session/{ui_session_id}")
async def get_rlm_run_for_session(ui_session_id: str):
    """Resolve a sidebar view session (session_type='rlm') to its run detail."""
    run = db.get_rlm_run_by_ui_session(ui_session_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"No RLM run for session '{ui_session_id}'")
    return await asyncio.to_thread(_run_detail, run)


@router.get("/api/rlm/runs/{run_id}")
async def get_rlm_run(run_id: str):
    """One run's row plus manifest, children, artifacts, and (when finished)
    the full answer text."""
    run = db.get_rlm_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"RLM run '{run_id}' not found")
    return await asyncio.to_thread(_run_detail, run)


@router.get("/api/rlm/runs/{run_id}/trace")
async def get_rlm_run_trace(run_id: str, after: int = 0, limit: int = 500):
    """Parsed trace.jsonl events starting at byte offset `after`.

    Returns complete lines only — a partial tail line mid-append is left for
    the next poll, so `next_offset` always lands on a line boundary and the
    client just passes it back. The run's current status/counters ride along
    so a polling viewer refreshes its header and knows when to stop.
    """
    run = db.get_rlm_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"RLM run '{run_id}' not found")
    trace_path = _run_dir(run) / "trace.jsonl"
    after = max(0, int(after))
    limit = max(1, min(int(limit), 2000))
    events, next_offset = await asyncio.to_thread(_read_trace, trace_path, after, limit)
    return {
        "run_id": run_id,
        "status": run["status"],
        "iterations": run["iterations"],
        "subcalls": run["subcalls"],
        "events": events,
        "next_offset": next_offset,
        "running": run["status"] == "running",
    }


def _run_dir(run: dict) -> Path:
    # run_dir is workspace-relative and comes from our own DB row (never from
    # the caller), so joining it under the workspace is safe; the containment
    # check is belt-and-braces against a corrupted row.
    workspace = Path(settings.workspace_dir).resolve()
    run_dir = (workspace / run["run_dir"]).resolve()
    if not run_dir.is_relative_to(workspace):
        raise HTTPException(status_code=400, detail="run_dir escapes the workspace")
    return run_dir


def _read_trace(path: Path, offset: int, limit: int) -> tuple[list[dict], int]:
    events: list[dict] = []
    consumed = offset
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            for raw in fh:
                if not raw.endswith(b"\n"):
                    break  # partial tail line mid-append — re-read next poll
                consumed += len(raw)
                try:
                    events.append(json.loads(raw))
                except (ValueError, UnicodeDecodeError):
                    continue
                if len(events) >= limit:
                    break
    except OSError:
        pass  # no trace yet (or unreadable) — empty page at the same offset
    return events, consumed


def _run_detail(run: dict) -> dict:
    run_dir = _run_dir(run)
    manifest = {}
    manifest_path = run_dir / "manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = {"error": "manifest unreadable"}

    answer = None
    answer_path = run_dir / "answer.txt"
    if run["status"] != "running" and answer_path.exists():
        try:
            answer = answer_path.read_text(encoding="utf-8", errors="replace")[:_ANSWER_INLINE_LIMIT]
        except OSError:
            answer = None

    children = [
        {k: c.get(k) for k in ("run_id", "status", "iterations", "subcalls", "task", "created_at", "finished_at")}
        for c in db.list_rlm_run_children(run["run_id"])
    ]

    return {
        **run,
        "manifest": manifest,
        "children": children,
        "answer": answer,
        "has_trace": (run_dir / "trace.jsonl").exists(),
        "trace_path": f"{run['run_dir']}/trace.jsonl",
        "answer_path": f"{run['run_dir']}/answer.txt" if answer_path.exists() else None,
    }
