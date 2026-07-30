"""RLM run artifacts: run dirs, manifest, and the rlm_runs audit rows.

Layout (workflow_runs precedent — relative path in the DB, heavy data on disk):

    <workspace>/rlm/<run_id>/
        manifest.json   run parameters + terminal status
        context/        staged copies of the source material
        trace.jsonl     every root turn, cell, and sub-call
        child.log       the child process's real stdout/stderr
        answer.txt      the final answer
        sub/<id>/       nested rlm_query child runs (depth >= 1)

Everything here is transient working data: the durable output is the tool
result in the session transcript. Snooze retention purges dir + rows after
settings.rlm_run_retention_days.
"""

import json
import logging
import secrets
import time
from pathlib import Path

from config import settings
from core.extensions.rlm.types import RLMRunResult
from db import models as db

logger = logging.getLogger(__name__)


def mint_run_dir(parent_run_dir: Path | None = None) -> tuple[str, Path, str]:
    """Create a fresh run dir. Returns (run_id, absolute dir, workspace-relative dir)."""
    run_id = secrets.token_hex(4)
    if parent_run_dir is not None:
        run_dir = Path(parent_run_dir).resolve() / "sub" / run_id
    else:
        # Absolute, always: workspace_dir defaults to a relative path, and the
        # child process (cwd = run dir) must see socket/context paths that
        # don't re-resolve against itself.
        run_dir = (Path(settings.workspace_dir) / "rlm" / run_id).resolve()
    run_dir.mkdir(parents=True, exist_ok=False)
    try:
        run_rel = str(run_dir.resolve().relative_to(Path(settings.workspace_dir).resolve()))
    except ValueError:
        run_rel = str(run_dir)
    return run_id, run_dir, run_rel


def record_start(
    run_id: str,
    run_dir: Path,
    run_rel: str,
    *,
    session_id: str,
    task: str,
    source_desc: str,
    root_model: str,
    sub_model: str,
    input_chars: int,
    parent_run_id: str | None = None,
    depth: int = 0,
) -> None:
    manifest = {
        "run_id": run_id,
        "session_id": session_id,
        "parent_run_id": parent_run_id,
        "depth": depth,
        "task": task,
        "source": source_desc,
        "root_model": root_model,
        "sub_model": sub_model,
        "input_chars": input_chars,
        "status": "running",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    _write_manifest(run_dir, manifest)
    try:
        db.create_rlm_run(
            run_id=run_id,
            session_id=session_id,
            task=task,
            source_desc=source_desc,
            root_model=root_model,
            sub_model=sub_model,
            input_chars=input_chars,
            run_dir=run_rel,
            parent_run_id=parent_run_id,
            depth=depth,
        )
    except Exception:
        logger.exception("failed to record rlm_runs start row for %s", run_id)


def record_finish(run_id: str, run_dir: Path, result: RLMRunResult) -> None:
    try:
        db.finish_rlm_run(
            run_id=run_id,
            status=result.status,
            iterations=result.iterations,
            subcalls=result.subcalls,
            answer_preview=result.answer[:500],
            error=result.error,
        )
    except Exception:
        logger.exception("failed to record rlm_runs finish row for %s", run_id)
    manifest_path = run_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        manifest = {"run_id": run_id}
    manifest.update(
        {
            "status": result.status,
            "iterations": result.iterations,
            "subcalls": result.subcalls,
            "duration_seconds": round(result.duration, 1),
            "error": result.error,
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
    )
    _write_manifest(run_dir, manifest)


def _write_manifest(run_dir: Path, manifest: dict) -> None:
    try:
        (run_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    except OSError:
        logger.warning("failed to write RLM manifest in %s", run_dir)
