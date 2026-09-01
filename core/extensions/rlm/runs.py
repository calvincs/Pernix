"""RLM run artifacts: run dirs, manifest, and the rlm_runs audit rows.

Layout (relative path in the DB, heavy data on disk):

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
from core.extensions.rlm.types import RLMCaps, RLMRunResult
from db import models as db

logger = logging.getLogger(__name__)


def mint_run_dir(parent_run_dir: Path | None = None, base_dir: Path | None = None) -> tuple[str, Path, str]:
    """Create a fresh run dir. Returns (run_id, absolute dir, workspace-relative dir).

    base_dir (v33): a space session's runs nest under its workspace home
    (spaces/<slug>/rlm/<id>) so run artifacts live with the space's files.
    run_rel stays relative to the GLOBAL workspace root either way — every
    consumer (purge, retention, continue_from) resolves it against that root.
    """
    run_id = secrets.token_hex(4)
    if parent_run_dir is not None:
        run_dir = Path(parent_run_dir).resolve() / "sub" / run_id
    else:
        # Absolute, always: workspace_dir defaults to a relative path, and the
        # child process (cwd = run dir) must see socket/context paths that
        # don't re-resolve against itself.
        root = Path(base_dir).resolve() if base_dir is not None else Path(settings.workspace_dir).resolve()
        run_dir = (root / "rlm" / run_id).resolve()
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
    ui_session_id: str | None = None,
    caps: RLMCaps | None = None,
    source_sha256: str | None = None,
    continued_from: str | None = None,
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
    if source_sha256:
        # continue_from compares against this so a continuation never builds
        # on findings derived from different source material.
        manifest["source_sha256"] = source_sha256
    if continued_from:
        manifest["continued_from"] = continued_from
    if ui_session_id:
        manifest["ui_session_id"] = ui_session_id
    if caps is not None:
        # The viewer needs the denominators ("iteration 7 of 20") — caps are
        # settings at run time, so the manifest is the only durable record.
        manifest["caps"] = {
            "max_iterations": caps.max_iterations,
            "max_subcalls": caps.max_subcalls,
            "timeout_seconds": caps.timeout_seconds,
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
            ui_session_id=ui_session_id,
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
