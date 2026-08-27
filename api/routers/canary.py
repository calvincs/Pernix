"""Pernix — Canary suite endpoints: listing, runs, triggers, and full CRUD.

Fixed-path routes are declared BEFORE the parameterised /{name} routes —
Starlette matches in declaration order (the same precedent skills.py
documents), so "/api/canary/runs" must not be swallowed by "/{name}".
"""

from __future__ import annotations

import asyncio as _asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from config import settings
from db import models as db

router = APIRouter(tags=["canary"])


def _def_payload(d, stats=None) -> dict:
    return {
        "name": d.name,
        "tags": d.tags,
        "covers": d.covers,
        "flaky": d.flaky,
        "parked": d.parked,
        "max_runs": d.max_runs,
        "expires": d.expires,
        "gates": [g["name"] for g in d.gates],
        "timeout": d.timeout,
        "last_reviewed": d.last_reviewed,
        **({"stats": stats} if stats is not None else {}),
    }


@router.get("/api/canary")
async def list_canaries():
    """Suite definitions + per-task pass rates over the retention window."""
    from core.canary import scan_canaries

    defs = await _asyncio.to_thread(scan_canaries)
    runs = await _asyncio.to_thread(db.list_canary_runs, None, None, 500)
    by_task: dict[str, dict] = {}
    for r in runs:
        s = by_task.setdefault(r["task"], {"runs": 0, "passed": 0, "last_run": None})
        s["runs"] += 1
        s["passed"] += 1 if r.get("passed") else 0
        if s["last_run"] is None:
            s["last_run"] = {
                "created_at": r.get("created_at"),
                "passed": bool(r.get("passed")),
                "outcome": r.get("outcome"),
                "trigger": r.get("trigger"),
                "duration_s": r.get("duration_s"),
            }
    return {
        "enabled": settings.canary_enabled,
        "schedule": settings.canary_schedule,
        "heartbeat_per_night": settings.canary_heartbeat_per_night,
        "canaries": [
            _def_payload(d, by_task.get(d.name, {"runs": 0, "passed": 0, "last_run": None})) for d in defs
        ],
    }


@router.get("/api/canary/runs")
async def list_runs(task: str = "", batch_id: str = "", limit: int = 50):
    rows = await _asyncio.to_thread(db.list_canary_runs, task or None, batch_id or None, max(1, min(limit, 500)))
    return {"runs": rows, "count": len(rows)}


@router.post("/api/canary/run")
async def trigger_run(body: dict = {}):
    """Manual trigger: queue one canary (or the whole suite with name='*')."""
    if not settings.canary_enabled:
        raise HTTPException(400, detail="canary_enabled is off")
    from core.canary import load_canary
    from core.extensions.scheduling import enqueue_full_sweep, enqueue_manual_canary

    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, detail="name is required ('*' runs the whole suite)")
    if name == "*":
        # "Run all" means all: a full sweep includes parked canaries, and
        # must_run means a heartbeat in flight defers it instead of eating it.
        if not enqueue_full_sweep("run-all"):
            raise HTTPException(503, detail="scheduler unavailable")
        return {"queued": "*"}
    if load_canary(name) is None:
        raise HTTPException(404, detail=f"no canary named '{name}'")
    if not enqueue_manual_canary(name):
        raise HTTPException(503, detail="scheduler unavailable")
    return {"queued": name}


@router.post("/api/canary")
async def create_canary(body: dict = {}):
    """Create a canary from raw CANARY.md text or a structured spec.

    Gate commands are checked against the auto-admission allowlist proof and
    the verdicts come back as warnings — advisory, never a blocker: a human
    creating a canary by hand is the authority the proof substitutes for.
    """
    from core.canary.parser import CanaryParseError, parse_canary_md
    from core.canary.propose import is_gate_command_safe, materialize_canary, write_canary_md

    raw = str(body.get("raw") or "")
    if raw:
        import shutil
        import tempfile
        from pathlib import Path

        # Parse first (any temp dirname; a name/dir mismatch only warns) so
        # the frontmatter's own name names the directory.
        tmp = Path(tempfile.mkdtemp(prefix="canary-api-")) / "pending" / "CANARY.md"
        tmp.parent.mkdir(parents=True)
        try:
            tmp.write_text(raw, encoding="utf-8")
            parsed = parse_canary_md(tmp)
        except CanaryParseError as e:
            raise HTTPException(400, detail=str(e)) from None
        finally:
            shutil.rmtree(tmp.parent.parent, ignore_errors=True)
        got, err = await _asyncio.to_thread(write_canary_md, parsed.name, raw)
        gates = parsed.gates
    else:
        got, err = await _asyncio.to_thread(materialize_canary, body)
        gates = body.get("gates") or []
    if err:
        raise HTTPException(400, detail=err)
    warnings = []
    for g in gates:
        reason = is_gate_command_safe(str(g.get("command") or ""))
        if reason:
            warnings.append(f"gate '{g.get('name')}': {reason}")
    return {"created": got, "warnings": warnings}


@router.get("/api/canary/{name}")
async def get_canary(name: str):
    """Full definition plus the raw CANARY.md for editing."""
    from core.canary import load_canary

    d = await _asyncio.to_thread(load_canary, name)
    if d is None or d.path is None:
        raise HTTPException(404, detail=f"no canary named '{name}'")
    payload = _def_payload(d)
    payload["prompt"] = d.prompt
    payload["body"] = d.body
    payload["raw_content"] = d.path.read_text(encoding="utf-8")
    return payload


@router.put("/api/canary/{name}")
async def update_canary(name: str, body: dict = {}):
    """Replace a canary's CANARY.md wholesale (validated round-trip)."""
    from core.canary import load_canary
    from core.canary.propose import write_canary_md

    if await _asyncio.to_thread(load_canary, name) is None:
        raise HTTPException(404, detail=f"no canary named '{name}'")
    raw = str(body.get("raw") or body.get("content") or "")
    if not raw.strip():
        raise HTTPException(400, detail="raw CANARY.md content is required")
    got, err = await _asyncio.to_thread(write_canary_md, name, raw, None, True)
    if err:
        raise HTTPException(400, detail=err)
    return {"updated": got}


@router.patch("/api/canary/{name}")
async def patch_canary(name: str, body: dict = {}):
    """Park or unpark (mirrors the skills PATCH enable/disable idiom)."""
    from core.canary import load_canary
    from core.canary.maintain import _rewrite_frontmatter

    d = await _asyncio.to_thread(load_canary, name)
    if d is None or d.path is None:
        raise HTTPException(404, detail=f"no canary named '{name}'")
    if "parked" not in body:
        raise HTTPException(400, detail="body needs {'parked': true|false}")
    parked = bool(body["parked"])
    if parked == d.parked:
        return {"name": name, "parked": parked, "changed": False}
    ok = await _asyncio.to_thread(_rewrite_frontmatter, d.path, {"parked": parked})
    if not ok:
        raise HTTPException(500, detail="frontmatter rewrite failed — see logs")
    return {"name": name, "parked": parked, "changed": True}


@router.post("/api/canary/{name}/reviewed")
async def mark_reviewed(name: str):
    """Bump last_reviewed to today (answers the staleness nudge)."""
    from core.canary import load_canary
    from core.canary.maintain import _rewrite_frontmatter

    d = await _asyncio.to_thread(load_canary, name)
    if d is None or d.path is None:
        raise HTTPException(404, detail=f"no canary named '{name}'")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ok = await _asyncio.to_thread(_rewrite_frontmatter, d.path, {"last_reviewed": today})
    if not ok:
        raise HTTPException(500, detail="frontmatter rewrite failed — see logs")
    return {"name": name, "last_reviewed": today}


@router.delete("/api/canary/{name}")
async def delete_canary(name: str):
    """Retire a canary: moved to .retired/ with a marker, purged for good
    only after canary_purge_after_days — reversible for the whole window."""
    from core.canary import load_canary
    from core.canary.maintain import retire_canary

    d = await _asyncio.to_thread(load_canary, name)
    if d is None:
        raise HTTPException(404, detail=f"no canary named '{name}'")
    from core.canary.parser import canaries_dir

    ok = await _asyncio.to_thread(retire_canary, d, canaries_dir(), "deleted via API", "user")
    if not ok:
        raise HTTPException(500, detail="retirement failed — see logs")
    return {
        "retired": name,
        "note": f"kept in .retired/ for {settings.canary_purge_after_days} days; move it back to restore",
    }
