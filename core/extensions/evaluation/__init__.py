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
        model = settings.background_model or settings.llm_model

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
        model = settings.background_model or settings.llm_model
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


def register(reg) -> None:
    common = {"category": "evaluation", "source": "extension"}
    tags = ["evaluate", "test", "verify", "validate", "check", "qa", "quality", "assess"]

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
