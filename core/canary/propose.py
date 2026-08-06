"""Pernix — Canary proposal generation (plan §5 "growing the suite", §12.2).

Refine turns real failed turns into CANDIDATE canaries — the regression-test
convention at the behavior level. Nothing reaches data/canaries/ without a
human: proposals ride adaptive_proposals (producer="canary_propose", dict
payload), and APPROVING one materializes the CANARY.md through a validated
round-trip, then enqueues a manual vetting run.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

from db import models as db

logger = logging.getLogger("pernix.canary")

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}$")

CANARY_PROPOSALS_PROMPT = """
ADDITIONALLY output a "canary_proposals" array in the same JSON object
(empty array when nothing qualifies). A canary proposal turns THIS
session's failure into a permanent regression check — a small, offline,
deterministic task with shell-command gates:

  "canary_proposals": [
    {
      "name": "kebab-case-name",
      "prompt": "self-contained task instructions for a fresh agent",
      "gates": [{"name": "g1", "command": "shell command, exit 0 = pass", "watch_paths": []}],
      "files": {"relative/path.txt": "seed fixture content"},
      "rationale": "which failure in this session this canary pins"
    }
  ]

Only propose when the session exposed a REPEATABLE failure class (not a
one-off env problem), the task can run offline against seeded fixture
files, and the gates are deterministic. At most 1 proposal. A human
reviews it before it joins the suite."""


def queue_canary_proposals(proposals: list, producer: str, session_id: str = "") -> int:
    """Validate + store canary proposals for human review. Returns count."""
    stored = 0
    for p in proposals or []:
        if not isinstance(p, dict):
            continue
        err = _validate_spec(p)
        if err:
            logger.info("canary proposal rejected (%s): %s", producer, err)
            continue
        evidence = [f"session:{session_id}"] if session_id else []
        db.adaptive_add_proposal(
            producer="canary_propose",
            payload_json=json.dumps({"canary": p}),
            evidence_json=json.dumps(evidence),
            rationale=f"[new canary '{p['name']}'] {str(p.get('rationale') or '')[:400]} "
            f"(proposed by {producer}; approving writes data/canaries/{p['name']}/CANARY.md "
            f"and queues a vetting run)",
        )
        stored += 1
    return stored


def _validate_spec(p: dict) -> str | None:
    name = str(p.get("name") or "").strip()
    if not _NAME_RE.match(name):
        return f"invalid name {name!r} (kebab-case, 2-49 chars)"
    if not str(p.get("prompt") or "").strip():
        return "prompt is required"
    gates = p.get("gates")
    if not isinstance(gates, list) or not gates:
        return "at least one gate is required"
    for g in gates:
        if not isinstance(g, dict) or not g.get("name") or not g.get("command"):
            return "each gate needs name and command"
    files = p.get("files") or {}
    if not isinstance(files, dict):
        return "files must be a mapping"
    for rel in files:
        if Path(rel).is_absolute() or ".." in Path(rel).parts:
            return f"files key {rel!r} must be workspace-relative"
    return None


def materialize_canary(spec: dict, base: Path | None = None) -> tuple[str | None, str]:
    """Write an approved proposal as data/canaries/<name>/CANARY.md.

    Validated round-trip: render → parse_canary_md on a temp copy → move.
    Returns (name, "") on success or (None, error).
    """
    from core.canary.parser import canaries_dir, parse_canary_md

    err = _validate_spec(spec)
    if err:
        return None, err
    base = base or canaries_dir()
    name = spec["name"]
    target_dir = base / name
    if target_dir.exists():
        return None, f"canary '{name}' already exists"

    fm = {
        "name": name,
        "prompt": spec["prompt"],
        "gates": [
            {
                "name": str(g["name"]),
                "command": str(g["command"]),
                "watch_paths": [str(w) for w in (g.get("watch_paths") or [])],
            }
            for g in spec["gates"]
        ],
        "timeout": int(spec.get("timeout") or 600),
        "tags": [str(t) for t in (spec.get("tags") or ["proposed"])],
        "flaky": False,
        "last_reviewed": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }
    files = spec.get("files") or {}
    if files:
        fm["files"] = {str(k): str(v) for k, v in files.items()}
    body = (
        f"{str(spec.get('rationale') or 'Proposed from a real failed turn.').strip()}\n\n"
        "Machine-proposed, human-approved. Review the gates before trusting\n"
        "this canary's signal; tag `flaky: true` if it proves unstable."
    )
    text = f"---\n{yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)}---\n\n{body}\n"

    tmp_root = Path(tempfile.mkdtemp(prefix="canary-proposal-"))
    try:
        tmp_dir = tmp_root / name
        tmp_dir.mkdir()
        tmp_md = tmp_dir / "CANARY.md"
        tmp_md.write_text(text, encoding="utf-8")
        parse_canary_md(tmp_md)  # raises CanaryParseError on any invariant break
        base.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp_dir), str(target_dir))
    except Exception as e:
        return None, f"materialization failed: {e}"
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
    logger.info("Canary '%s' materialized from approved proposal", name)
    return name, ""
