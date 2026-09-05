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
     paths (/usr, /bin, …) do not count — reading Pernix's own data
     directory, another session's files, or the repository does.
  3. Does the transcript name another canary, or `data/canaries`? That is
     the suite reading its own answer key, whichever way it got there.

A hit sets ``canary_runs.outcome = 'contaminated'``. The scored `passed`
value is preserved exactly as the gates returned it — the run is not
rewritten, it is disqualified: the tripwire drops contaminated rows from both
testimony and baselines, so a compromised run can neither flag a batch nor
vouch for one. One notification per contaminated run; silence is how the last
version of this failure lasted for the life of the feature.

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


def _tool_calls(row: dict) -> list[dict]:
    raw = row.get("tool_calls")
    if not raw:
        return []
    try:
        calls = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [c for c in calls if isinstance(c, dict)] if isinstance(calls, list) else []


def _outside_workspace(text: str, workspace: str) -> list[str]:
    """Absolute paths in `text` that are neither workspace nor toolchain."""
    hits = []
    for raw in _ABS_PATH_RE.findall(text or ""):
        if workspace and (raw == workspace or raw.startswith(workspace.rstrip("/") + "/")):
            continue
        if raw.startswith(SYSTEM_PREFIXES):
            continue
        hits.append(raw)
    return hits


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

    # The transcript itself — assistant prose, tool results, the reflect row.
    # Content, not just tool args: a canary that was *told* another canary's
    # name is contaminated whether or not it acted on it.
    blob = "\n".join(str(m.get("content") or "") for m in messages or [])
    if _SUITE_DIR_RE.search(blob):
        findings.append("transcript references data/canaries")
    named = [
        other
        for other in _other_canary_names(canary_name, known_canaries)
        if re.search(rf"(?<![\w-]){re.escape(other)}(?![\w-])", blob)
    ]
    if named:
        findings.append(f"transcript names other canaries: {', '.join(sorted(named)[:5])}")

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


def notify(canary_name: str, session_id: str, findings: list[str]) -> None:
    """One notification per contaminated run."""
    from db import models as db

    try:
        db.add_notification(
            title=f"Canary run contaminated: {canary_name}",
            body=(
                f"Session {session_id[:12]} broke canary isolation, so the run was "
                f"recorded as outcome='contaminated' and is excluded from tripwire "
                f"testimony and baselines. Findings: {'; '.join(findings)[:400]}. "
                "The scored gate result is kept as-is — check whether a tool lost its "
                "canary denial, or whether a skill body is steering the agent out of "
                "its workspace."
            ),
            urgency="normal",
        )
    except Exception as e:
        logger.warning("Contamination notification failed for '%s': %s", canary_name, e)
