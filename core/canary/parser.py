"""Pernix — CANARY.md parser: one directory per canary under data/canaries/.

Format (mirrors SKILL.md, reusing the same frontmatter helper):

    ---
    name: fix-failing-test
    prompt: |
      The test in tests/test_math.py fails. Find the bug and fix it.
    gates:
      - name: pytest
        command: python -m pytest tests/test_math.py -q
        watch_paths: [src/]
    model: ""            # optional model override
    timeout: 600         # optional per-run wall clock (seconds)
    tags: [coding, debug]
    flaky: false         # flaky canaries inform, never trip the tripwire
    last_reviewed: 2026-08-06
    ---
    Free-form notes for humans reviewing this canary.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from config import settings
from core.skills.parser import parse_frontmatter_md

logger = logging.getLogger("pernix.canary")

DEFAULT_TIMEOUT_S = 600


class CanaryParseError(Exception):
    """Raised when a CANARY.md file cannot be parsed."""


@dataclass
class CanaryDef:
    name: str
    prompt: str
    gates: list[dict]  # [{name, command, watch_paths?}]
    model: str = ""
    timeout: int = DEFAULT_TIMEOUT_S
    tags: list[str] = field(default_factory=list)
    flaky: bool = False
    last_reviewed: str = ""
    body: str = ""
    path: Path | None = None
    # Optional workspace seed files: {relative_path: content}. Written into
    # the run's temp workspace before the prompt is sent, so gates have
    # deterministic fixtures to check (plan §5: fixtures over live URLs).
    files: dict = field(default_factory=dict)


def canaries_dir() -> Path:
    return Path(settings.canaries_dir)


def parse_canary_md(path: Path) -> CanaryDef:
    """Parse one CANARY.md. Raises CanaryParseError on invalid files."""
    fm, body = parse_frontmatter_md(path, error_cls=CanaryParseError)

    name = str(fm.get("name") or "").strip()
    if not name:
        raise CanaryParseError(f"{path}: missing required field 'name'")
    if name != path.parent.name:
        logger.warning("Canary name '%s' doesn't match directory '%s'", name, path.parent.name)

    prompt = str(fm.get("prompt") or "").strip()
    if not prompt:
        raise CanaryParseError(f"{path}: missing required field 'prompt'")

    raw_gates = fm.get("gates")
    if not isinstance(raw_gates, list) or not raw_gates:
        raise CanaryParseError(f"{path}: 'gates' must be a non-empty list — a canary without gates cannot be scored")
    gates: list[dict] = []
    for i, g in enumerate(raw_gates):
        if not isinstance(g, dict) or not g.get("name") or not g.get("command"):
            raise CanaryParseError(f"{path}: gates[{i}] needs 'name' and 'command'")
        wp = g.get("watch_paths") or []
        if isinstance(wp, str):
            wp = [wp]
        gates.append({"name": str(g["name"]), "command": str(g["command"]), "watch_paths": [str(p) for p in wp]})

    tags = fm.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]

    files = fm.get("files") or {}
    if not isinstance(files, dict):
        raise CanaryParseError(f"{path}: 'files' must be a mapping of relative_path -> content")
    for rel in files:
        if Path(rel).is_absolute() or ".." in Path(rel).parts:
            raise CanaryParseError(f"{path}: files key '{rel}' must be a workspace-relative path")

    try:
        timeout = int(fm.get("timeout") or DEFAULT_TIMEOUT_S)
    except (TypeError, ValueError):
        raise CanaryParseError(f"{path}: 'timeout' must be an integer (seconds)") from None

    return CanaryDef(
        name=name,
        prompt=prompt,
        gates=gates,
        model=str(fm.get("model") or ""),
        timeout=max(60, timeout),
        tags=[str(t) for t in tags],
        flaky=bool(fm.get("flaky", False)),
        last_reviewed=str(fm.get("last_reviewed") or ""),
        body=body,
        path=path,
        files={str(k): str(v) for k, v in files.items()},
    )


def scan_canaries(base: Path | None = None) -> list[CanaryDef]:
    """All valid canaries under base (default data/canaries). Invalid files
    log a warning and are skipped — one bad canary must not sink a sweep."""
    base = base or canaries_dir()
    if not base.is_dir():
        return []
    out: list[CanaryDef] = []
    for d in sorted(base.iterdir()):
        md = d / "CANARY.md"
        if not d.is_dir() or not md.is_file():
            continue
        try:
            out.append(parse_canary_md(md))
        except CanaryParseError as e:
            logger.warning("Skipping invalid canary: %s", e)
    return out


def load_canary(name: str, base: Path | None = None) -> CanaryDef | None:
    """Load a single canary by name; None when absent or invalid."""
    base = base or canaries_dir()
    md = base / name / "CANARY.md"
    if not md.is_file():
        return None
    try:
        return parse_canary_md(md)
    except CanaryParseError as e:
        logger.warning("Invalid canary '%s': %s", name, e)
        return None
