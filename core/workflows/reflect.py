"""Pernix — Post-workflow reflect: generate skill improvement proposals.

Runs synchronously after all workflow workers complete. Reads each worker's
reflect verdict, identifies steps where failure_cause == "skill", and uses
the background model to propose concrete improvements to those skill files.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from config import settings
from db import models as db

logger = logging.getLogger("pernix.workflows.reflect")


WORKFLOW_REFLECT_PROMPT = """You are a Workflow Reflect Agent. A multi-step workflow has just completed.
Some steps failed. Each failed step is labelled with a failure_cause:
  - "skill"   — the skill's instructions were inadequate / outdated / missing guidance
  - "tool"    — the agent used a tool incorrectly, or the tool's usage wasn't clear from the skill
  - "context" — the instructions were too broad / too long / asked the agent to do too much at once
  - other     — something else; still propose improvements if you can see them

For each failed step provided, propose a concrete improvement to the referenced skill's SKILL.md.

Output a JSON object:
{
  "proposals": [
    {
      "skill_name": "the skill name",
      "section": "which section of SKILL.md to change (e.g. 'Common Failures', 'Pre-flight', 'Usage')",
      "problem": "1-2 sentences describing what went wrong",
      "proposed_change": "the specific text to add or change in the skill instructions",
      "confidence": 0.0-1.0
    }
  ]
}

RULES:
- Tailor the proposal to the failure_cause:
    * "skill"   → add missing guidance, fix inaccurate steps, clarify preconditions
    * "tool"    → add a concrete tool-usage example to the skill
    * "context" → suggest splitting the instruction into smaller sub-steps, or tightening scope
- Be specific: reference real section names from the provided SKILL.md content.
- proposed_change must be actionable prose the user can paste into the skill file.
- If confidence < 0.6, omit the proposal entirely.
- If there is nothing useful to propose, return {"proposals": []}.
- Output valid JSON only. No markdown, no explanation outside the JSON. /no_think"""


# Failure causes we consider actionable for skill-improvement proposals. Anything
# else is ignored — a missing reflect / a cancelled worker doesn't tell us
# anything useful about the skill itself.
_ACTIONABLE_FAILURE_CAUSES = frozenset({"skill", "tool", "context"})


@dataclass
class SkillProposal:
    skill_name: str
    section: str
    problem: str
    proposed_change: str
    confidence: float
    source_step_id: str
    source_worker_id: str


def workflow_reflect(
    manifest_path: Path,
    wf,  # WorkflowDef
    ctx: dict | None = None,
) -> int:
    """Generate skill improvement proposals for failed workflow steps.

    Returns the number of proposals persisted to the DB.
    Only generates proposals for steps where failure_cause == "skill".
    Uses the background_model for cost efficiency.
    """
    ctx = ctx or {}
    model = settings.background_model or settings.llm_model
    if not model:
        logger.warning("workflow_reflect: no model configured, skipping")
        return 0

    # Read manifest
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("workflow_reflect: could not read manifest %s: %s", manifest_path, e)
        return 0

    workflow_name = manifest.get("workflow", "")
    run_id = manifest.get("run_id", "")

    # Find failed steps with actionable failure causes. We now include "tool"
    # and "context" failures in addition to "skill" — the LLM can often propose
    # a useful skill edit for any of those (e.g. a tool-usage example, a scope
    # tightening). Only steps that point at a skill are eligible, since that's
    # the file we'd edit.
    failed_skill_steps = []
    for step in wf.steps:
        if not step.skill:
            continue
        step_info = manifest.get("steps", {}).get(step.id, {})
        # Only consider steps whose final status was a true failure, not
        # skipped (upstream failure) or complete.
        status = step_info.get("status")
        if status not in ("failed", "escalated"):
            continue
        worker_id = step_info.get("worker_id")
        if not worker_id:
            continue
        reflect_data = _get_reflect_for_worker(worker_id)
        if not reflect_data:
            continue
        failure_cause = reflect_data.get("failure_cause", "none")
        if failure_cause not in _ACTIONABLE_FAILURE_CAUSES:
            continue
        failed_skill_steps.append(
            {
                "step_id": step.id,
                "skill_name": step.skill,
                "worker_id": worker_id,
                "reflect": reflect_data,
            }
        )

    if not failed_skill_steps:
        logger.debug("workflow_reflect: no skill failures found in run %s", run_id)
        return 0

    # Build prompt input with skill content
    from core.skills.registry import get_skill_registry

    skill_reg = get_skill_registry()

    step_contexts: list[str] = []
    for entry in failed_skill_steps:
        skill_name = entry["skill_name"]
        reflect_data = entry["reflect"]
        skill_body = skill_reg.load_instructions(skill_name) or "(no instructions found)"
        # Truncate to avoid context bloat
        skill_body_truncated = skill_body[:3000]

        ctx_text = (
            f"--- STEP: {entry['step_id']} ---\n"
            f"Skill: {skill_name}\n"
            f"Failure cause: {reflect_data.get('failure_cause', 'skill')}\n"
            f"Reflect diagnostic: {reflect_data.get('diagnostic', '')}\n"
            f"What failed: {reflect_data.get('what_failed', '')}\n"
            f"Reflect reasoning: {reflect_data.get('reasoning', '')}\n"
            f"\nCurrent SKILL.md content:\n{skill_body_truncated}\n"
        )
        step_contexts.append(ctx_text)

    user_content = "\n\n".join(step_contexts)

    # Call the LLM. Preferred path: the tool executor's event loop supplied via
    # ctx["_loop"] — we're on a worker thread, so we need a loop we can schedule
    # the async chat() call on. Fallback: asyncio.run() in this thread, which
    # creates a fresh loop. The old code silently returned 0 when no loop was
    # present, which meant the feature effectively vanished in any non-standard
    # call path (tests, scripts, future refactors). Prefer a visible error.
    loop = ctx.get("_loop")

    start_ms = int(time.time() * 1000)
    try:
        from core.llm.client import get_llm_client

        client = get_llm_client()

        chat_coro = client.chat(
            messages=[
                {"role": "system", "content": WORKFLOW_REFLECT_PROMPT},
                {"role": "user", "content": user_content},
            ],
            model=model,
            max_tokens=2048,
        )

        if loop is not None and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(chat_coro, loop)
            response = future.result(timeout=120)
        else:
            # No external loop — run the coroutine synchronously in a fresh loop.
            response = asyncio.run(chat_coro)

        raw = (response.content or "").strip()
    except Exception as e:
        logger.warning("workflow_reflect: LLM call failed: %s", e)
        return 0

    latency_ms = int(time.time() * 1000) - start_ms
    logger.debug("workflow_reflect: LLM responded in %dms", latency_ms)

    # Parse proposals
    proposals = _parse_proposals(raw, failed_skill_steps)
    if not proposals:
        return 0

    # Persist to DB
    saved = 0
    for proposal in proposals:
        try:
            db.add_skill_proposal(
                workflow_name=workflow_name,
                run_id=run_id,
                skill_name=proposal.skill_name,
                section=proposal.section,
                problem=proposal.problem,
                proposed_change=proposal.proposed_change,
                confidence=proposal.confidence,
                source_step_id=proposal.source_step_id,
                source_worker_id=proposal.source_worker_id,
            )
            saved += 1
        except Exception as e:
            logger.warning("workflow_reflect: could not persist proposal: %s", e)

    logger.info(
        "workflow_reflect: %d proposal(s) generated for workflow '%s' run %s",
        saved,
        workflow_name,
        run_id,
    )
    return saved


def _get_reflect_for_worker(worker_id: str) -> dict | None:
    """Return the worker's most recent reflect verdict as a dict."""
    try:
        messages = db.get_messages(worker_id)
    except Exception as e:
        logger.warning("workflow_reflect: could not read messages for worker %s: %s", worker_id[:8], e)
        return None
    for m in reversed(messages):
        if m.get("role") == "reflect":
            try:
                return json.loads(m.get("content") or "{}")
            except (json.JSONDecodeError, TypeError):
                return None
    return None


def _parse_proposals(raw: str, failed_steps: list[dict]) -> list[SkillProposal]:
    """Parse the LLM JSON output into SkillProposal objects."""
    # Strip markdown fences if present
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("workflow_reflect: could not parse LLM output as JSON: %s\n%s", e, text[:500])
        return []

    raw_proposals = data.get("proposals", [])
    if not isinstance(raw_proposals, list):
        return []

    # Build lookup: skill_name → (step_id, worker_id) from failed steps
    skill_to_step: dict[str, tuple[str, str]] = {}
    for entry in failed_steps:
        skill_to_step[entry["skill_name"]] = (entry["step_id"], entry["worker_id"])

    proposals: list[SkillProposal] = []
    for raw_p in raw_proposals:
        if not isinstance(raw_p, dict):
            continue
        skill_name = str(raw_p.get("skill_name", "")).strip()
        confidence = float(raw_p.get("confidence", 0.0))

        # Drop low-confidence proposals and proposals for unrecognized skills
        if confidence < 0.6:
            continue
        if skill_name not in skill_to_step:
            logger.debug("workflow_reflect: ignoring proposal for unknown skill '%s'", skill_name)
            continue

        step_id, worker_id = skill_to_step[skill_name]
        proposals.append(
            SkillProposal(
                skill_name=skill_name,
                section=str(raw_p.get("section", "")).strip(),
                problem=str(raw_p.get("problem", "")).strip(),
                proposed_change=str(raw_p.get("proposed_change", "")).strip(),
                confidence=confidence,
                source_step_id=step_id,
                source_worker_id=worker_id,
            )
        )

    return proposals
