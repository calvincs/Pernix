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
    tags: [coding, debug]  # 'sentinel' = always in the post-batch probe
    covers: [skill:foo, kind:prompt_note]  # change surfaces this canary tests
    flaky: false         # flaky canaries inform, never trip the tripwire
    parked: false        # parked = off the heartbeat; still coverage-run
    max_runs: 0          # probe: auto-retire after N total runs (0 = never)
    expires: ""          # probe: auto-retire after this ISO date
    last_reviewed: 2026-08-06
    ---
    Free-form notes for humans reviewing this canary.

GENERATED CANARIES (trust-loop hardening W5). A canary directory may carry a
``generate.py`` next to its CANARY.md:

    def generate(seed: int) -> dict:
        return {"prompt": str, "files": {relpath: str}, "gates": [{...}]}

The runner picks a fresh random seed per run and takes prompt/files/gates
from that call, so a memorised answer cannot pass a sentinel. Such a file
may omit ``prompt``, ``gates`` and ``files`` — everything else (name,
timeout, tags, flaky, parked, covers, probe fields) is read normally.

Detection is the sibling ``generate.py`` OR the frontmatter flag
``generated: true``. Both, because maintenance rewrites (park, flaky, probe
retirement) revalidate the new text in a bare temp directory where the
sibling file does not exist — without the flag every generated canary would
be frozen out of the maintenance sweep by a parse error.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from config import settings
from core.skills.parser import parse_frontmatter_md

logger = logging.getLogger("pernix.canary")

DEFAULT_TIMEOUT_S = 600
# A canary directory carrying this file builds its fixture per run.
GENERATOR_FILENAME = "generate.py"


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
    # Change surfaces this canary tests, as `<domain>:<name>` strings —
    # `skill:stateful-env-reverse-engineering`, `kind:prompt_note`. Coverage
    # triggers (a skill edit, an adaptive batch) select canaries by these.
    covers: list[str] = field(default_factory=list)
    flaky: bool = False
    # Parked = long-green and off the heartbeat rotation. Still visible,
    # still runs on coverage triggers, full sweeps and manual runs; a red
    # run auto-unparks it. Written by auto-maintenance, editable by hand.
    parked: bool = False
    # Probe fields: a canary with max_runs > 0 (total runs) or a past
    # `expires` date is auto-retired by maintenance with a summary
    # notification — "occasionally test something" without suite residue.
    max_runs: int = 0
    expires: str = ""
    # Legacy (pre-parking cadence demotion). Parsed so old files stay valid
    # and hand-authored values survive rewrites; nothing reads it any more.
    cadence: int = 1
    last_reviewed: str = ""
    body: str = ""
    path: Path | None = None
    # Optional workspace seed files: {relative_path: content}. Written into
    # the run's temp workspace before the prompt is sent, so gates have
    # deterministic fixtures to check (plan §5: fixtures over live URLs).
    files: dict = field(default_factory=dict)
    # Generated fixtures (W5): True when the canary's prompt/files/gates come
    # from a per-run `generate(seed)` call instead of the frontmatter.
    # `generator_path` is the resolved generate.py, or None when the flag is
    # set but the file is not on disk (a maintenance temp copy — parseable,
    # not runnable).
    generated: bool = False
    generator_path: Path | None = None


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

    # A generated canary's task IS the generator: prompt, files and gates are
    # produced per run from a fresh seed, so requiring them in the frontmatter
    # would mean writing down an answer the design exists to withhold.
    generator_path = path.parent / GENERATOR_FILENAME
    generated = bool(fm.get("generated")) or generator_path.is_file()

    prompt = str(fm.get("prompt") or "").strip()
    if not prompt and not generated:
        raise CanaryParseError(f"{path}: missing required field 'prompt'")

    raw_gates = fm.get("gates") or ([] if generated else None)
    if not isinstance(raw_gates, list) or (not raw_gates and not generated):
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

    try:
        cadence = max(1, int(fm.get("cadence") or 1))
    except (TypeError, ValueError):
        cadence = 1  # legacy field, nothing reads it — never fail a file over it

    covers = fm.get("covers") or []
    if isinstance(covers, str):
        covers = [c.strip() for c in covers.split(",") if c.strip()]
    if not isinstance(covers, list):
        raise CanaryParseError(f"{path}: 'covers' must be a list of '<domain>:<name>' strings")

    try:
        max_runs = max(0, int(fm.get("max_runs") or 0))
    except (TypeError, ValueError):
        raise CanaryParseError(f"{path}: 'max_runs' must be an integer (0 = no limit)") from None

    expires = str(fm.get("expires") or "").strip()
    if expires:
        try:
            datetime.fromisoformat(expires)
        except ValueError:
            raise CanaryParseError(f"{path}: 'expires' must be an ISO date, got '{expires}'") from None

    return CanaryDef(
        name=name,
        prompt=prompt,
        gates=gates,
        model=str(fm.get("model") or ""),
        timeout=max(60, timeout),
        tags=[str(t) for t in tags],
        covers=[str(c) for c in covers],
        flaky=bool(fm.get("flaky", False)),
        parked=bool(fm.get("parked", False)),
        max_runs=max_runs,
        expires=expires,
        cadence=cadence,
        last_reviewed=str(fm.get("last_reviewed") or ""),
        body=body,
        path=path,
        files={str(k): str(v) for k, v in files.items()},
        generated=generated,
        generator_path=generator_path if generator_path.is_file() else None,
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
