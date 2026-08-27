"""Pernix — Typed worker kinds (spec Feature 4).

A worker *kind* is a named bundle: role instructions, an exclusive tool
allowlist, a default model, and verification criteria. `spawn_worker(kind=...)`
selects one by name instead of hand-writing the same charter every time, and
the harness re-applies the bundle when a worker is rehydrated after a reap or
restart (spec Feature 5), so a typed worker keeps its shape for its whole life.

Enforcement rides existing machinery on purpose: the allowlist lands on
`AgentSession.tool_allowlist`, which the schema builder intersects and the
executor refuses past (the same two-point enforcement scheduled-job charters
use) — no new enforcement path to audit.

Operators can override built-ins or add new kinds by dropping JSON files in
`data/worker_kinds/<name>.json` with any subset of the WorkerKind fields.
Files are re-read on every resolve (they are tiny), so edits apply to the
next spawn without a restart.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("pernix.ext.orchestration.kinds")

WORKER_KINDS_DIR = Path("data/worker_kinds")

# Tools every kind gets regardless of specialization: reading the workspace,
# finding tools, memory recall, skills, and the parent channel. file_write is
# deliberately here — the worker contract requires writing the
# `.worker_<id>_summary.md` deliverable, so a kind without file_write would
# break every worker it produces.
_COMMON_TOOLS = frozenset(
    {
        "file_read",
        "file_write",
        "glob",
        "grep",
        "discover_tools",
        "get_tool_schema",
        "recall",
        "deep_recall",
        "notify_parent",
        "discover_skills",
        "list_skills",
        "load_skill",
        "read_skill_resource",
    }
)


@dataclass(frozen=True)
class WorkerKind:
    """One named worker bundle. `tool_allowlist` is EXCLUSIVE (same contract
    as scheduled-job charters): tools outside it are dropped from the schema
    and refused by the executor. `model` is "" (inherit session default),
    "background" (resolve settings.background_model at spawn), or a literal
    model id."""

    name: str
    description: str
    role_instructions: str
    tool_allowlist: frozenset = field(default_factory=frozenset)
    model: str = ""
    # Criteria appended to the worker charter AND graded by reflect (reflect
    # reads the system prompt). One judgeable condition per line.
    verification: str = ""


_BUILTIN_KINDS: dict[str, WorkerKind] = {
    k.name: k
    for k in (
        WorkerKind(
            name="research",
            description="Read-only web/memory research; must cite sources.",
            role_instructions=(
                "You are a RESEARCH worker: gather facts, do not modify project "
                "state. Prefer primary sources; note when sources disagree."
            ),
            tool_allowlist=_COMMON_TOOLS | {"search_web", "browse_web", "http_get", "repl", "rlm_process"},
            verification=(
                "Every non-obvious claim in the summary names its source (URL or document).\n"
                "Unanswered sub-questions are listed explicitly, not omitted."
            ),
        ),
        WorkerKind(
            name="code",
            description="Implement/modify code; full file tools + shell.",
            role_instructions=(
                "You are a CODE worker: implement the change end-to-end. Run the "
                "relevant tests or a syntax/import check before reporting done."
            ),
            tool_allowlist=_COMMON_TOOLS
            | {
                "file_edit",
                "multiedit",
                "bash",
                "repl",
                "install_package",
                "http_get",
                "job_start",
                "job_status",
                "job_tail",
                "job_kill",
                "rlm_process",
            },
            verification=(
                "The summary states which check (test run, syntax check, import) was "
                "executed and its actual result.\n"
                "Changed file paths are listed."
            ),
        ),
        WorkerKind(
            name="explore",
            description="Read-only codebase/workspace survey with file:line citations.",
            role_instructions=(
                "You are an EXPLORE worker: locate and map, never modify. Answer "
                "with concrete file:line citations for every finding."
            ),
            tool_allowlist=_COMMON_TOOLS | {"rlm_process"},
            verification=("Every finding in the summary carries a file:line (or file) citation."),
        ),
        WorkerKind(
            name="debug",
            description="Reproduce a failure and state the root cause.",
            role_instructions=(
                "You are a DEBUG worker: reproduce the failure first, then narrow "
                "to a root cause. A fix without a stated root cause is incomplete."
            ),
            tool_allowlist=_COMMON_TOOLS
            | {
                "file_edit",
                "multiedit",
                "bash",
                "repl",
                "install_package",
                "http_get",
                "job_start",
                "job_status",
                "job_tail",
                "job_kill",
                "rlm_process",
            },
            verification=(
                "The summary shows the reproduction (command + observed failure).\n"
                "The root cause is stated as a specific mechanism, not a guess."
            ),
        ),
        WorkerKind(
            name="transform",
            description="Deterministic file transforms; no network.",
            role_instructions=(
                "You are a TRANSFORM worker: produce the output file(s) from the "
                "input(s). Verify the output exists and parses/loads before done."
            ),
            tool_allowlist=_COMMON_TOOLS | {"file_edit", "multiedit", "bash", "repl"},
            verification=("The summary names each output file and the check proving it parses/loads."),
        ),
    )
}

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")


def builtin_kind_names() -> list[str]:
    return sorted(_BUILTIN_KINDS)


def _load_override(name: str) -> dict | None:
    path = WORKER_KINDS_DIR / f"{name}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError) as e:
        logger.warning("worker kind override %s unreadable: %s", path, e)
        return None


def list_kind_names() -> list[str]:
    """Built-ins plus any data-root definitions."""
    names = set(_BUILTIN_KINDS)
    try:
        if WORKER_KINDS_DIR.is_dir():
            for p in WORKER_KINDS_DIR.glob("*.json"):
                if _NAME_RE.match(p.stem):
                    names.add(p.stem)
    except OSError:
        pass
    return sorted(names)


def resolve_kind(name: str) -> WorkerKind | None:
    """Resolve a kind by name: data-root override merged over the built-in.

    Returns None for an unknown name — the caller owns the error message so
    it can list valid kinds. Malformed override fields fall back to the
    built-in's values (or the dataclass defaults for a pure data-root kind).
    """
    name = (name or "").strip().lower()
    if not name:
        return None
    base = _BUILTIN_KINDS.get(name)
    override = _load_override(name) if _NAME_RE.match(name) else None
    if base is None and override is None:
        return None

    def _pick(key: str, default):
        if override is not None and key in override:
            return override[key]
        return getattr(base, key) if base is not None else default

    allow = _pick("tool_allowlist", frozenset())
    if isinstance(allow, (list, tuple, set)):
        allow = frozenset(str(t) for t in allow)
    elif not isinstance(allow, frozenset):
        allow = frozenset()
    return WorkerKind(
        name=name,
        description=str(_pick("description", "")),
        role_instructions=str(_pick("role_instructions", "")),
        tool_allowlist=allow,
        model=str(_pick("model", "")),
        verification=str(_pick("verification", "")),
    )


def resolve_kind_model(kind: WorkerKind) -> str:
    """Translate the kind's model field into a concrete model id ("" = inherit)."""
    if kind.model == "background":
        from config import settings

        return settings.background_model or ""
    return kind.model


def kind_charter_block(kind: WorkerKind) -> str:
    """The text spawn_worker appends to the worker's system prompt."""
    lines = [f"\nWorker kind: {kind.name} — {kind.description}", kind.role_instructions]
    if kind.verification:
        lines.append(
            "Before writing your summary file, verify each of these (the "
            "quality gate grades against them):\n" + kind.verification
        )
    return "\n".join(line for line in lines if line) + "\n"


# Cheap deterministic per-kind output checks, used by get_worker_result to
# prefix an honest warning when a kind's core contract is visibly unmet.
# Deliberately narrow: reflect remains the real quality gate; these only catch
# the unambiguous misses (a research summary with zero sources named).
_URL_OR_DOC_RE = re.compile(r"https?://|(?:[A-Za-z0-9_./-]+\.(?:md|pdf|html?|txt|py|rst|json))\b")
_FILE_LINE_RE = re.compile(r"[\w./-]+\.[A-Za-z0-9]{1,8}(?::\d+)?")


def kind_gate_warning(kind_name: str | None, summary_text: str) -> str | None:
    """Return a one-line warning when the kind's deterministic gate fails."""
    if not kind_name or not summary_text:
        return None
    text = summary_text[:6000]
    if kind_name == "research" and not _URL_OR_DOC_RE.search(text):
        return "# KIND GATE (research): no source URL/document named in the summary — treat claims as uncited.\n"
    if kind_name == "explore" and not _FILE_LINE_RE.search(text):
        return "# KIND GATE (explore): no file citations found in the summary — findings are unanchored.\n"
    return None
