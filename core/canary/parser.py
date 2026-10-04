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
    covers: [skill:foo]  # change surfaces this canary tests (informational)
    flaky: false         # flaky canaries inform, never count as failures
    serve: [article.html]  # seed files served over HTTP instead of written
    tools: [http_get]    # extra read-only web tools (see DECLARABLE_TOOLS)
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
timeout, tags, flaky, covers) is read normally.

Detection is the sibling ``generate.py`` OR the frontmatter flag
``generated: true``. Both, because frontmatter rewrites (the "reviewed"
button) revalidate the new text in a bare temp directory where the sibling
file does not exist — without the flag every generated canary would fail
that rewrite with a parse error.

SERVED FIXTURES. ``serve:`` names seed files (from ``files:`` or from
``generate()``) that the runner publishes under
``<workspace_dir>/.canary-serve/<run-token>/`` for the length of the run
instead of writing them into the task workspace. ``{{SERVE_BASE}}`` in the
prompt and in gate commands becomes the URL of that directory on Pernix's
own server, so a canary can test fetching a page without the internet.

SCOPED TOOLS. ``tools:`` adds tools to the run's allowlist, but only from
DECLARABLE_TOOLS — read-only web reads. ``search_web`` is not one of them
(an external, metered call whose results change daily). Nothing machine-
written emits this key: the create API's structured spec and the skill
verify-sync render fixed key sets.

LEGACY KEYS. ``parked``, ``max_runs``, ``expires`` and ``cadence`` belonged
to the suite auto-maintenance retired in 3.2. They still parse, so old files
stay valid, and nothing reads them.
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
# Tag marking a canary the learning loop may never see: not quoted into a
# refine or dream prompt, not the target of a proposal-derived edit, and not
# something a proposal may re-describe. A holdout is the suite's honest
# ground: a task the system cannot have trained itself against.
HOLDOUT_TAG = "holdout"
# The only tools a CANARY.md may add to the run's allowlist with `tools:`.
DECLARABLE_TOOLS = frozenset({"http_get", "browse_web"})
# Placeholder the runner swaps for the served-fixture URL.
SERVE_PLACEHOLDER = "{{SERVE_BASE}}"


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
    # `skill:youtube-whisper`. Informational: shown in the Canary tab.
    covers: list[str] = field(default_factory=list)
    flaky: bool = False
    # Legacy keys of the auto-maintenance retired in 3.2 (parking, one-off
    # probes, cadence demotion). Parsed so old files stay valid and
    # hand-authored values survive rewrites; nothing reads them any more.
    parked: bool = False
    max_runs: int = 0
    expires: str = ""
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
    # Served fixtures: names (keys of `files`) published over HTTP for the
    # run instead of written into the workspace.
    serve: list[str] = field(default_factory=list)
    # Extra allowlisted tools, a subset of DECLARABLE_TOOLS.
    tools: list[str] = field(default_factory=list)

    @property
    def holdout(self) -> bool:
        """True for a canary the learning loop must never touch (W5)."""
        return HOLDOUT_TAG in self.tags


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

    serve = _str_list(fm.get("serve"), path, "serve")
    for rel in serve:
        if Path(rel).is_absolute() or ".." in Path(rel).parts:
            raise CanaryParseError(f"{path}: serve entry '{rel}' must be a relative file name")
    tools = _str_list(fm.get("tools"), path, "tools")
    undeclarable = sorted(set(tools) - DECLARABLE_TOOLS)
    if undeclarable:
        raise CanaryParseError(f"{path}: tools {undeclarable} cannot be declared; allowed: {sorted(DECLARABLE_TOOLS)}")

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
        serve=serve,
        tools=tools,
    )


def _str_list(raw, path: Path, key: str) -> list[str]:
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        raw = [t.strip() for t in raw.split(",") if t.strip()]
    if not isinstance(raw, list):
        raise CanaryParseError(f"{path}: '{key}' must be a list")
    return [str(t) for t in raw]


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
