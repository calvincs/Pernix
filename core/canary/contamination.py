"""Pernix — post-run contamination scan (trust-loop hardening W5, plan §5.3).

Isolation is enforced at three points (tool schema, scout filtering, executor
backstop), which is exactly the kind of claim that quietly stops being true.
A new tool ships without `denied_session_types`, an MCP server exposes a
memory-shaped verb, a skill body tells the agent to `cat` its way out of the
workspace — and the suite keeps reporting green while measuring something
other than the pipeline.

So every canary run is read back afterwards and asked three questions:

  1. Did it call a memory tool? (It has none on its allowlist. If one
     answered, the fence has a hole.)
  2. Did it touch an absolute path outside its temp workspace? Toolchain
     paths (/usr, /bin, …), the skills directory (a canary may load the
     skill it tests) and the served-fixture directory do not count —
     reading Pernix's own data directory, another session's files, or the
     repository does. A single-segment token (`/ERROR`, `/1000`, `/512`) is
     prose or arithmetic far more often than a path, so it only counts when
     it names a known data root.
  3. Does the transcript point at `data/canaries` or at another canary's
     directory? That is the suite reading its own answer key. Another
     canary's bare NAME is not a finding: generated canaries share words
     (`gen-grep-count` contains `grep-count`), and naming a task is not
     reading its answer.

A hit sets ``canary_runs.outcome = 'contaminated'``. The scored `passed`
value is preserved exactly as the gates returned it — the run is not
rewritten, it is disqualified: it is counted apart from passes and failures,
so a compromised run cannot vouch for the pipeline. It is a RECORD, not an
alarm (3.2): the finding lands on the run row and in the Canary tab, and no
notification is raised. On the reference box almost every contaminated run
was a false positive of this heuristic, and each one was a notice.

This is detection, not prevention. Bash is on the canary allowlist because
the seed tasks need it, so the workspace is a fence and not a jail; the scan
is what makes the fence observable.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger("pernix.canary")

# Every memory verb in the codebase, plus the scout's own. None of these are
# on CANARY_TOOL_ALLOWLIST — one appearing in a transcript IS the finding.
MEMORY_TOOL_NAMES = frozenset(
    {
        "remember",
        "recall",
        "deep_recall",
        "ingest",
        "update_memory",
        "forget",
        "search_memory",
        "memory_search",
    }
)

# Absolute paths that are toolchain, not knowledge. A canary that runs
# /usr/bin/python3 has not learned anything about this deployment.
SYSTEM_PREFIXES = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/opt", "/proc", "/sys", "/dev", "/etc", "/run")

# An absolute path token. The lookbehind keeps arithmetic ("$1/2"), URLs
# ("https://x/y") and already-matched separators from reading as paths, and
# at least one path character must follow the slash so a bare "/" in prose
# is not a finding.
_ABS_PATH_RE = re.compile(r"(?<![\w=:/])/[\w.\-+@]+(?:/[\w.\-+@]+)*")

# The canary directory itself, however it is spelled.
_SUITE_DIR_RE = re.compile(r"data[/\\]canaries")

# Single-segment absolute tokens only count when they name one of these.
# Everything else ("/ERROR", "/1000") is prose, a regex or arithmetic.
_DATA_ROOT_MARKERS = ("/app/data", "data/memories", "sessions.db")

# Runner-written served fixtures live under this workspace subdirectory
# (core/canary/runner.py); fetching or reading them is the task.
SERVE_DIRNAME = ".canary-serve"


def _tool_calls(row: dict) -> list[dict]:
    raw = row.get("tool_calls")
    if not raw:
        return []
    try:
        calls = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [c for c in calls if isinstance(c, dict)] if isinstance(calls, list) else []


def _exempt_roots() -> tuple[str, ...]:
    """Absolute directories a canary may legitimately read: the skills dir."""
    try:
        from config import settings

        return (str(Path(settings.skills_dir).resolve()).rstrip("/") + "/",)
    except Exception:
        return ()


def _is_outside_path(raw: str, workspace: str, exempt: tuple[str, ...]) -> bool:
    if workspace and (raw == workspace or raw.startswith(workspace.rstrip("/") + "/")):
        return False
    if raw.startswith(SYSTEM_PREFIXES):
        return False
    if f"/{SERVE_DIRNAME}/" in raw + "/":
        return False
    if any((raw.rstrip("/") + "/").startswith(root) for root in exempt):
        return False
    if len([seg for seg in raw.split("/") if seg]) >= 2:
        return True
    return any(marker in raw for marker in _DATA_ROOT_MARKERS)


def _outside_workspace(text: str, workspace: str) -> list[str]:
    """Absolute paths in `text` that are neither workspace, toolchain, the
    skills directory nor a served fixture — and that look like paths."""
    exempt = _exempt_roots()
    return [raw for raw in _ABS_PATH_RE.findall(text or "") if _is_outside_path(raw, workspace, exempt)]


def _names_canary_dir(name: str, text: str) -> bool:
    """True when `text` points at another canary's DIRECTORY
    (`canaries/<name>`, any separator) — not when it merely says the name."""
    return re.search(rf"canaries[/\\]+{re.escape(name)}(?![\w-])", text or "") is not None


def _other_canary_names(current: str, known: list[str] | None) -> list[str]:
    if known is None:
        try:
            from core.canary.parser import scan_canaries

            known = [c.name for c in scan_canaries()]
        except Exception as e:  # a scan problem must never fail a run
            logger.debug("Contamination scan: canary listing failed: %s", e)
            known = []
    return [n for n in known if n and n != current]


def scan_session(
    session_id: str,
    workspace: str,
    canary_name: str,
    known_canaries: list[str] | None = None,
    messages: list[dict] | None = None,
) -> list[str]:
    """Read one finished canary session back. Returns the findings, newest
    concern first; an empty list means the run is clean.

    Never raises: a scan problem must not turn a good run into a bad row.
    """
    try:
        if messages is None:
            from db import models as db

            messages = db.get_messages(session_id)
    except Exception as e:
        logger.warning("Contamination scan could not read session %s: %s", session_id, e)
        return []

    ws = str(Path(workspace).resolve()) if workspace else ""
    findings: list[str] = []
    memory_tools: set[str] = set()
    outside: list[str] = []

    for row in messages or []:
        for call in _tool_calls(row):
            try:
                from core.llm.types import extract_tool_call_fields

                _id, name, arguments = extract_tool_call_fields(call)
            except Exception:
                name, arguments = str(call.get("name") or ""), str(call.get("arguments") or "")
            if name in MEMORY_TOOL_NAMES:
                memory_tools.add(name)
            outside.extend(_outside_workspace(str(arguments), ws))

    if memory_tools:
        findings.append(f"memory tool called: {', '.join(sorted(memory_tools))}")
    if outside:
        uniq = sorted(set(outside))[:5]
        findings.append(f"read outside the workspace: {', '.join(uniq)}")

    # The transcript itself — assistant prose, tool results, the reflect row —
    # plus the tool arguments: a path into the suite is a finding wherever it
    # appears.
    blob = "\n".join(str(m.get("content") or "") for m in messages or [])
    blob += "\n" + "\n".join(str(c.get("arguments") or c) for m in messages or [] for c in _tool_calls(m))
    if _SUITE_DIR_RE.search(blob):
        findings.append("transcript references data/canaries")
    named = [other for other in _other_canary_names(canary_name, known_canaries) if _names_canary_dir(other, blob)]
    if named:
        findings.append(f"transcript points at other canaries' directories: {', '.join(sorted(named)[:5])}")

    return findings


def contamination_record(findings: list[str]) -> dict:
    """The row appended to gate_results_json so the Self-checks tab and the
    canary_status tool can say WHY a run was disqualified."""
    detail = "; ".join(findings)
    return {
        "kind": "contamination",
        "name": "isolation",
        "command": "post-run contamination scan",
        "passed": False,
        "output_tail": detail[:1500],
        "findings": findings,
    }
