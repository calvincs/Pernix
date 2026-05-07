"""Pernix — Planning extension: feature registry for build tasks."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("pernix.ext.planning")

REGISTRY_PATH = Path("data/registry.json")
ARCHIVE_PATH = Path("data/registry_archive.json")


def _load_registry() -> list[dict]:
    if not REGISTRY_PATH.exists():
        return []
    try:
        return json.loads(REGISTRY_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def _save_registry(features: list[dict]) -> None:
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(json.dumps(features, indent=2))


def add_feature(title: str, description: str, criteria: str, parent_id: str = "", _context: dict | None = None) -> str:
    """Add a feature with acceptance criteria to the registry."""
    ctx = _context or {}
    session_id = ctx.get("session_id", "")

    feature = {
        "id": uuid.uuid4().hex[:8],
        "title": title,
        "description": description,
        "criteria": [c.strip() for c in criteria.split("\n") if c.strip()],
        "passes": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "passed_at": None,
        "session_id": session_id,
        "parent_id": parent_id or None,
    }

    features = _load_registry()
    features.append(feature)
    _save_registry(features)

    return f"Feature added: {feature['id']} — {title} ({len(feature['criteria'])} criteria)"


def mark_feature_passed(feature_id: str, _context: dict | None = None) -> str:
    """Mark a feature as passed."""
    features = _load_registry()
    for f in features:
        if f["id"] == feature_id:
            if f["passes"]:
                return f"Feature {feature_id} already passed."
            f["passes"] = True
            f["passed_at"] = datetime.now(timezone.utc).isoformat()
            _save_registry(features)
            return f"Feature {feature_id} marked as PASSED: {f['title']}"
    return f"Feature {feature_id} not found."


def list_features(_context: dict | None = None) -> str:
    """List all features with their status."""
    features = _load_registry()
    if not features:
        return "No features registered."

    lines = []
    passed = 0
    for f in features:
        status = "PASS" if f["passes"] else "pending"
        if f["passes"]:
            passed += 1
        criteria_count = len(f.get("criteria", []))
        lines.append(f"- [{status}] {f['id']} {f['title']} ({criteria_count} criteria)")

    header = f"Features: {passed}/{len(features)} passed\n"
    return header + "\n".join(lines)


def register(reg) -> None:
    common = {"category": "planning", "source": "extension"}
    tags = ["feature", "plan", "task", "track", "requirement", "criteria"]

    reg.register(
        name="add_feature",
        func=add_feature,
        description=(
            "Register acceptance criteria for a deliverable BEFORE you implement it. "
            "Use ONLY for BUILD/IMPLEMENT tasks where success is subjective "
            "(e.g. 'writes idiomatic Python', 'produces a clean report'). "
            "DO NOT use for operational requests (fetch, transcribe, run, look up, etc.) "
            "or to log work already done — that creates registry noise and triggers an "
            "unneeded auto-eval round. Criteria are newline-separated; one judgeable condition per line."
        ),
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Feature title"},
                "description": {"type": "string", "description": "Feature description"},
                "criteria": {"type": "string", "description": "Acceptance criteria (one per line)"},
                "parent_id": {"type": "string", "description": "Parent feature ID (optional)"},
            },
            "required": ["title", "description", "criteria"],
        },
        tags=tags + ["add", "create", "new"],
        timeout=30,
        parallel_safe=False,
        safety_level="safe",
        **common,
    )
    reg.register(
        name="mark_feature_passed",
        func=mark_feature_passed,
        description="Mark a feature as passed (immutable — cannot undo).",
        parameters={
            "type": "object",
            "properties": {"feature_id": {"type": "string", "description": "Feature ID"}},
            "required": ["feature_id"],
        },
        tags=tags + ["pass", "done", "complete"],
        timeout=15,
        parallel_safe=False,
        safety_level="safe",
        **common,
    )
    reg.register(
        name="list_features",
        func=list_features,
        description="List all registered features with pass/pending status.",
        parameters={"type": "object", "properties": {}},
        tags=tags + ["list", "status", "progress"],
        timeout=15,
        parallel_safe=True,
        **common,
    )
