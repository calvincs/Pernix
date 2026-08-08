"""Pernix — Evaluation extension: per-feature QA evaluation."""

from __future__ import annotations

import json
import logging

from config import settings
from db import models as db

logger = logging.getLogger("pernix.ext.evaluation")

EVAL_PROMPT = """You are a strict QA evaluator. Evaluate whether this feature meets its acceptance criteria.

Feature: {title}
Description: {description}

Criteria:
{criteria}

Evidence (workspace files, tool results, code):
{evidence}

For each criterion, score:
- 1.0: fully met, no issues
- 0.7: mostly met, minor gaps
- 0.5: partially met, significant gaps
- 0.3: barely addressed
- 0.0: not met at all

IMPORTANT: Check for "cargo cult" implementations — code that looks right but doesn't actually work.

Output JSON:
{{
  "scores": {{"criterion_text": score, ...}},
  "passed": true/false,
  "feedback": "specific improvement suggestions if failed"
}}

Pass threshold: ALL criteria must score >= {threshold}."""


def evaluate(feature_ids: str = "", _context: dict | None = None) -> str:
    """Evaluate features against their acceptance criteria."""
    ctx = _context or {}
    session_id = ctx.get("session_id", "")
    loop = ctx.get("_loop")
    if not session_id:
        return "Error: No session context"

    # Load features from registry
    from pathlib import Path

    registry_path = Path("data/registry.json")
    if not registry_path.exists():
        return "No features registered. Use add_feature first."

    features = json.loads(registry_path.read_text())
    if feature_ids:
        ids = [fid.strip() for fid in feature_ids.split(",")]
        features = [f for f in features if f["id"] in ids]

    if not features:
        return "No matching features found."

    # Evaluate each; use detailed output when only one feature is targeted
    single = len(features) == 1
    results = []
    for feat in features:
        if feat.get("passes"):
            results.append(f"- {feat['title']}: already passed")
            continue
        result = _evaluate_single(feat, session_id, loop=loop)
        status = "PASS" if result.get("passed") else "FAIL"
        if single:
            output = f"{feat['title']}: {status}\n"
            if result.get("scores"):
                for criterion, score in result["scores"].items():
                    output += f"  [{score}] {criterion}\n"
            if result.get("feedback"):
                output += f"\nFeedback: {result['feedback']}"
            return output.rstrip()
        results.append(f"- {feat['title']}: {status}")
        if result.get("feedback"):
            results.append(f"  Feedback: {result['feedback'][:200]}")

    return "\n".join(results)


def _evaluate_single(feat: dict, session_id: str, loop=None) -> dict:
    """Evaluate a single feature. Returns {passed, scores, feedback}.

    `loop` should be the running event loop captured by the tool executor
    and passed via _context["_loop"]. We're called on a worker thread, so
    asyncio.get_running_loop() raises RuntimeError here — that was the
    bug that surfaced as "Evaluation error: no running event loop" in
    every eval until 2026-04-28.
    """
    import asyncio

    # Collect evidence
    evidence = _collect_evidence(session_id, feat)

    # Build eval prompt
    criteria_text = "\n".join(f"- {c}" for c in feat.get("criteria", []))
    prompt = EVAL_PROMPT.format(
        title=feat.get("title", ""),
        description=feat.get("description", ""),
        criteria=criteria_text,
        evidence=evidence[:30000],
        threshold=settings.eval_threshold,
    )

    # LLM call (sync wrapper). Bridge back to the main event loop via the
    # executor-captured handle.
    try:
        from core.llm.client import get_llm_client

        client = get_llm_client()
        model = settings.llm_model or settings.background_model

        if loop is None:
            # Defensive fallback for callers that don't thread the loop —
            # try the standard accessor (works only when this runs on the
            # loop's thread, e.g. evaluate_single_async).
            loop = asyncio.get_event_loop_policy().get_event_loop()
        future = asyncio.run_coroutine_threadsafe(_eval_llm(client, model, prompt), loop)
        result = future.result(timeout=120)

        return result
    except Exception as e:
        logger.error("Evaluation failed: %s", e)
        return {"passed": False, "feedback": f"Evaluation error: {e}"}


async def _eval_llm(client, model: str, prompt: str) -> dict:
    resp = await client.chat(
        messages=[
            {"role": "system", "content": "You are a strict QA evaluator. Output JSON only."},
            {"role": "user", "content": prompt},
        ],
        model=model,
        max_tokens=1500,
    )
    text = resp.content.strip()
    if text.startswith("```"):
        text = "\n".join(text.split("\n")[1:])
        if text.endswith("```"):
            text = text[:-3]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"passed": False, "feedback": f"Failed to parse eval response: {text[:200]}"}


async def evaluate_single_async(feat: dict, session_id: str) -> dict:
    """Async version of _evaluate_single for use in async hooks."""
    evidence = _collect_evidence(session_id, feat)
    criteria_text = "\n".join(f"- {c}" for c in feat.get("criteria", []))
    prompt = EVAL_PROMPT.format(
        title=feat.get("title", ""),
        description=feat.get("description", ""),
        criteria=criteria_text,
        evidence=evidence[:30000],
        threshold=settings.eval_threshold,
    )
    try:
        from core.llm.client import get_llm_client

        client = get_llm_client()
        model = settings.llm_model or settings.background_model
        return await _eval_llm(client, model, prompt)
    except Exception as e:
        logger.error("Evaluation failed: %s", e)
        return {"passed": False, "feedback": f"Evaluation error: {e}"}


def _collect_evidence(session_id: str, feat: dict) -> str:
    """Collect evidence for evaluation: workspace files and messages."""
    parts = []

    # Workspace files (recent, non-hidden)
    from pathlib import Path

    workspace = Path(settings.workspace_dir)
    if workspace.exists():
        candidates = [
            f
            for f in workspace.rglob("*")
            if f.is_file() and not any(p.startswith(".") for p in f.relative_to(workspace).parts)
        ]
        candidates.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        for fpath in candidates[:10]:
            try:
                content = fpath.read_text(errors="replace")[:5000]
                rel = str(fpath.relative_to(workspace))
                parts.append(f"=== File: {rel} ===\n{content}")
            except Exception:
                pass

    # Recent assistant messages (implementation details)
    messages = db.get_messages(session_id)
    for m in messages[-10:]:
        if m["role"] == "assistant" and m.get("content"):
            parts.append(f"=== Agent response ===\n{m['content'][:2000]}")

    evidence = "\n\n".join(parts)
    if len(evidence) > 30000:
        evidence = evidence[:15000] + "\n[...truncated...]\n" + evidence[-15000:]
    return evidence


def add_gate(
    name: str,
    command: str,
    watch_paths: str = "",
    cwd: str = "",
    scope: str = "session",
    _context: dict | None = None,
) -> str:
    """Register a deterministic gate for this session."""
    from config import settings as _settings

    if not _settings.gates_enabled:
        return "Error: gates are disabled (settings.gates_enabled)."
    session_id = (_context or {}).get("session_id", "")
    if not session_id:
        return "Error: add_gate requires a session context."
    if not name or not command:
        return "Error: both name and command are required."
    scope = (scope or "session").strip().lower()
    if scope not in ("session", "goal"):
        return "Error: scope must be 'session' or 'goal'."

    # A gate is shell that runs unattended at every turn end, so it is held to
    # the same policy as the bash tool — checked here so the agent gets the
    # rejection while it can still fix the command, and again in gates._run_one
    # so a row that reached the table by any other route is still checked.
    from core.gates import check_gate_command, check_gate_cwd
    from core.tools.paths import workspace as _workspace

    command = command.strip()
    blocked = check_gate_command(command)
    if blocked:
        return f"{blocked} (gate '{name}' not registered)"
    cwd = cwd.strip()
    bad_cwd = check_gate_cwd(cwd, _workspace())
    if bad_cwd:
        return f"{bad_cwd} (gate '{name}' not registered)"

    from db import models as db

    paths = [p.strip() for p in watch_paths.split(",") if p.strip()] if watch_paths else []
    db.add_gate(
        session_id,
        name.strip(),
        command,
        watch_paths=paths,
        cwd=cwd or None,
        scope=scope,
    )
    guard = (
        f" watch_paths={paths} (unchanged paths reuse a prior failure on later retries)"
        if paths
        else " (no watch_paths — the gate re-runs every attempt)"
    )
    return (
        f"Gate '{name}' registered (scope={scope}): `{command}`.{guard} "
        f"It runs before Reflect at every turn end; a non-zero exit blocks a pass verdict. "
        f"A passing gate verifies only what it checks."
    )


def list_gates(_context: dict | None = None) -> str:
    session_id = (_context or {}).get("session_id", "")
    if not session_id:
        return "Error: list_gates requires a session context."
    from db import models as db

    rows = db.get_gates(session_id, enabled_only=False)
    if not rows:
        return "No gates registered for this session."
    lines = []
    for r in rows:
        state = "enabled" if r.get("enabled") else "disabled"
        watch = f" watch={r['watch_paths']}" if r.get("watch_paths") else ""
        lines.append(f"- {r['name']} [{state}] ({r.get('scope', 'session')}): `{r['command']}`{watch}")
    return "\n".join(lines)


def remove_gate(name: str, _context: dict | None = None) -> str:
    session_id = (_context or {}).get("session_id", "")
    if not session_id:
        return "Error: remove_gate requires a session context."
    from db import models as db

    if db.remove_gate(session_id, name):
        return f"Gate '{name}' removed."
    return f"Error: no gate named '{name}' in this session."


def register(reg) -> None:
    common = {"category": "evaluation", "source": "extension"}
    tags = ["evaluate", "test", "verify", "validate", "check", "qa", "quality", "assess"]

    from config import settings as _settings

    if _settings.gates_enabled:
        reg.register(
            name="add_gate",
            func=add_gate,
            description=(
                "Register a deterministic gate: a shell command that runs at every turn end "
                "before Reflect. A non-zero exit mechanically blocks a pass verdict — use for "
                "tests, builds, linters, or any host-observable completion check. Optional "
                "watch_paths (comma-separated, relative to the workspace) scope an unchanged-"
                "files guard so a stale failure isn't pointlessly re-run on later retries. "
                "scope='goal' marks the gate as a completion criterion for the session's goal "
                "(goal_complete is refused while it fails); scope='session' (default) is a "
                "plain per-turn check."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Short gate name (e.g. 'tests')"},
                    "command": {"type": "string", "description": "Shell command; exit 0 = pass"},
                    "watch_paths": {
                        "type": "string",
                        "description": "Optional comma-separated files/dirs the gate depends on",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Optional working directory, must be inside the workspace (default: workspace)",
                    },
                    "scope": {
                        "type": "string",
                        "enum": ["session", "goal"],
                        "description": "'session' (default) or 'goal' — a goal-scoped gate also blocks goal_complete",
                    },
                },
                "required": ["name", "command"],
            },
            tags=tags + ["gate", "deterministic", "ci", "build"],
            timeout=30,
            parallel_safe=False,
            # Registers shell that then runs unattended at EVERY turn end, for
            # the life of the session — a single approved call buys repeated
            # execution the user never sees again. That persistence is what
            # separates it from a one-shot `bash` call.
            safety_level="dangerous",
            **common,
        )
        reg.register(
            name="list_gates",
            func=list_gates,
            description="List this session's deterministic gates.",
            parameters={"type": "object", "properties": {}},
            tags=tags + ["gate", "list"],
            timeout=30,
            parallel_safe=True,
            safety_level="safe",
            **common,
        )
        reg.register(
            name="remove_gate",
            func=remove_gate,
            description="Remove a deterministic gate by name.",
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Gate name to remove"}},
                "required": ["name"],
            },
            tags=tags + ["gate", "remove"],
            timeout=30,
            parallel_safe=False,
            safety_level="safe",
            **common,
        )

    reg.register(
        name="evaluate",
        func=evaluate,
        description=(
            "Evaluate features against acceptance criteria. "
            "Pass a single feature ID for a detailed per-criterion score breakdown. "
            "Pass comma-separated IDs or leave empty to evaluate all pending features (brief summary). "
            "Runs QA evaluation with cargo-cult detection."
        ),
        parameters={
            "type": "object",
            "properties": {
                "feature_ids": {
                    "type": "string",
                    "description": "Feature ID, comma-separated IDs, or empty for all pending",
                },
            },
        },
        tags=tags + ["all", "single", "features"],
        timeout=300,
        parallel_safe=False,
        safety_level="safe",
        **common,
    )
