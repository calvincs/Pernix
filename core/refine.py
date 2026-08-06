"""Pernix — Whole-session refine pass.

Sibling of ``core/snooze_reflect.py`` with a broader gate. Where snooze_reflect
only fires when the reflect verdict's failure_cause is actionable and
confidence >= 0.5, refine looks at any idle session for any worth-saving
signal — including ones the reflect pass deemed "pass with no deviation",
and sessions with no reflect verdict at all.

Triggered as the tail-end activity of snooze (Activity 13). Watermarks via
``snooze_state['refined:{sid}']`` so each session is processed at most once.

Hard rule (same as snooze_reflect): never auto-applies skill edits. All
SKILL.md changes flow through the existing proposals UI for human approve/deny.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from config import settings
from core.snooze_reflect import _build_tool_summary, _identify_active_skill
from db import models as db

logger = logging.getLogger("pernix.refine")


# Mirrors snooze_reflect.PROPOSAL_CONFIDENCE_FLOOR — same bar for a paste-ready
# SKILL.md change. Lessons use no floor (advisory; scout already gates on hits).
PROPOSAL_CONFIDENCE_FLOOR: float = 0.6


REFINE_PROMPT = """You are a Session Refine Agent. A regular agent session has gone idle
and is ready for a broader look. Unlike a reflect-driven improvement pass,
you are NOT gated on whether the reflect verdict flagged a failure — read
the whole transcript and decide whether anything is worth crystallizing.

Look for these signals (any one is enough to act):

  - User correction / frustration mid-turn: "stop doing X",
    "don't format like this", "I already told you", "you keep doing Y",
    an explicit "remember this". These are first-class signals even when
    the turn nominally succeeded. The right place to embed the lesson is
    the SKILL.md that governs the task, not just memory.

  - Workflow correction: user redirected the agent's approach or
    sequence of steps. Encode the correction as a pitfall or explicit step.

  - Non-trivial technique, fix, workaround, debugging path, or tool-usage
    pattern that emerged and a future session would benefit from.

  - A skill that was loaded or consulted this session turned out to be
    wrong, missing a step, or outdated. Propose a patch.

Do NOT capture (these become persistent self-imposed constraints that bite
later when the environment changes):

  - Environment-dependent failures: missing binaries, fresh-install errors,
    post-migration path mismatches, "command not found", unconfigured
    credentials, uninstalled packages. The user can fix these — they are
    not durable rules.

  - Negative claims about tools or features ("browser tools do not work",
    "X tool is broken"). These harden into refusals the agent cites
    against itself for months after the actual problem was fixed.

  - Session-specific transient errors that resolved before the conversation
    ended. If retrying worked, the lesson is the retry pattern, not the
    original failure.

  - One-off task narratives. A user asking "summarize today's market" or
    "analyze this PR" is not a class of work that warrants a skill.

If a tool failed because of setup state, capture the FIX (install command,
config step, env var to set) under an existing setup or troubleshooting
skill — never "this tool does not work" as a standalone constraint.

Output a JSON object:
{
  "nothing_actionable": false,
  "proposals": [
    {
      "skill_name": "the active skill",
      "section": "section of SKILL.md (e.g. 'Common Failures', 'Pre-flight', 'Usage')",
      "problem": "1-2 sentences describing what was off",
      "proposed_change": "actionable prose to insert into SKILL.md",
      "confidence": 0.0-1.0
    }
  ],
  "lessons": [
    {
      "tags": "comma,separated,keywords",
      "weight": "high|normal",
      "content": "Self-contained lesson statement (1-3 sentences)",
      "applies_when": "Compact natural-language pattern describing when this lesson is relevant"
    }
  ]
}

RULES:
- If nothing in the session is worth saving, set "nothing_actionable": true
  and return empty proposals/lessons arrays. This is a real and valid
  outcome — don't fabricate signal.
- proposed_change must be concrete, paste-ready prose. Reference real
  section names from the SKILL.md content shown below.
- Proposals only meaningful when an active skill is identified.
- Skip a proposal whose confidence < 0.6.
- Lessons must be self-contained (understandable without the session).
  Include the trigger pattern in `applies_when` so future BM25 search
  can find them.
- Output valid JSON only. No markdown fences, no commentary. /no_think"""


def _latest_reflect_verdict(messages: list[dict]) -> dict | None:
    """Return the most recent reflect message parsed as a dict, or None.

    Local copy rather than importing snooze_reflect's: refine treats the
    verdict as optional context, not a precondition, so keeping the helper
    here makes the call site read straightforwardly.
    """
    for m in reversed(messages):
        if m.get("role") != "reflect":
            continue
        try:
            return json.loads(m.get("content") or "{}")
        except (json.JSONDecodeError, TypeError):
            return None
    return None


def _parse_refine_output(raw: str) -> tuple[list[dict], list[dict], list[dict], bool]:
    """Parse the LLM JSON into (proposals, lessons, adaptive_edits,
    nothing_actionable). Tolerates fences. adaptive_edits (plan 4d) rides
    the same call and the same parse — an empty array while the adaptive
    layer is off or the model has nothing durable."""
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines and lines[-1].strip() == "```" else lines[1:])
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("refine: could not parse LLM output as JSON: %s\n%s", e, text[:500])
        return [], [], [], False
    if not isinstance(data, dict):
        return [], [], [], False
    proposals = data.get("proposals", []) or []
    lessons = data.get("lessons", []) or []
    adaptive_edits = data.get("adaptive_edits", []) or []
    if not isinstance(proposals, list):
        proposals = []
    if not isinstance(lessons, list):
        lessons = []
    if not isinstance(adaptive_edits, list):
        adaptive_edits = []
    nothing_actionable = bool(data.get("nothing_actionable"))
    return proposals, lessons, adaptive_edits, nothing_actionable


def _build_user_content(
    session: dict,
    messages: list[dict],
    reflect_data: dict | None,
    active_skill: str | None,
    skill_body: str | None,
    tool_summary: dict,
) -> str:
    """Assemble the user-content blob fed to the LLM.

    Like snooze_reflect._build_user_content but reflect_data is optional —
    refine sees the whole transcript regardless of whether a reflect verdict
    was ever written.
    """
    transcript_lines: list[str] = []
    for m in messages:
        role = m.get("role", "")
        content = m.get("content", "") or ""
        if role in ("user", "assistant") and content:
            transcript_lines.append(f"[{role}] {content[:600]}")
        elif role == "tool" and content:
            transcript_lines.append(f"[tool_result] {content[:300]}")
    transcript = "\n".join(transcript_lines)[-8000:]

    tool_lines = [
        f"  - {name}: calls={info.get('calls', 0)}, failures={info.get('failures', 0)}"
        for name, info in tool_summary.items()
    ]
    tool_block = "\n".join(tool_lines) if tool_lines else "  (no tool calls)"

    skill_block = (
        f"\nCurrent SKILL.md content for '{active_skill}':\n{(skill_body or '')[:3000]}\n"
        if active_skill
        else "\n(no active skill identified — proposals impossible, lessons only)\n"
    )

    if reflect_data:
        reflect_block = (
            "\nReflect verdict (context only — do not treat as gating signal):\n"
            f"  verdict: {reflect_data.get('verdict')}\n"
            f"  failure_cause: {reflect_data.get('failure_cause')}\n"
            f"  confidence: {reflect_data.get('confidence')}\n"
            f"  reasoning: {reflect_data.get('reasoning', '')}\n"
            f"  diagnostic: {reflect_data.get('diagnostic', '')}\n"
            f"  what_worked: {reflect_data.get('what_worked', '')}\n"
            f"  what_failed: {reflect_data.get('what_failed', '')}\n"
            f"  strategy: {reflect_data.get('strategy', '')}\n"
        )
    else:
        reflect_block = "\n(no reflect verdict was recorded — judge from transcript)\n"

    return (
        f"--- SESSION: {session.get('id')} ---\n"
        f"Title: {session.get('title', '?')}\n"
        f"Active skill: {active_skill or '(none)'}\n"
        f"{reflect_block}"
        f"\nTool summary:\n{tool_block}\n"
        f"{skill_block}"
        f"\nConversation transcript (truncated):\n{transcript}\n"
    )


async def run_for_session(session_id: str) -> dict[str, Any]:
    """Run a whole-session refine pass.

    Returns a stats dict: {proposals_saved, lessons_saved, nothing_actionable,
    skipped_reason}. Caller (``snooze._do_cycle``) is responsible for setting
    the ``refined:{sid}`` watermark after this returns — failures stamp the
    watermark too, matching the mark-on-failure pattern used by
    ``snooze._catchup_distill`` so a broken session never retry-storms.
    """
    stats: dict[str, Any] = {
        "proposals_saved": 0,
        "lessons_saved": 0,
        "nothing_actionable": False,
        "skipped_reason": None,
    }

    session = db.get_session(session_id)
    if not session:
        stats["skipped_reason"] = "session_not_found"
        return stats

    if session.get("session_type") == "worker":
        stats["skipped_reason"] = "worker_session"
        return stats

    messages = db.get_messages(session_id)
    if not messages:
        stats["skipped_reason"] = "no_messages"
        return stats

    # Belt-and-suspenders: refine needs a real exchange. The selection query
    # in db.get_unrefined_sessions enforces this, but direct callers might not.
    has_user = any(m.get("role") == "user" and (m.get("content") or "").strip() for m in messages)
    has_assistant = any(m.get("role") == "assistant" and (m.get("content") or "").strip() for m in messages)
    if not (has_user and has_assistant):
        stats["skipped_reason"] = "insufficient_exchange"
        return stats

    reflect_data = _latest_reflect_verdict(messages)  # optional — None is fine

    active_skill = _identify_active_skill(messages)
    skill_body: str | None = None
    if active_skill:
        from core.skills.registry import get_skill_registry

        skill_body = get_skill_registry().load_instructions(active_skill)
        if skill_body is None:
            # Skill was renamed/removed — proposals impossible, lessons still useful.
            active_skill = None

    tool_summary = _build_tool_summary(messages)

    user_content = _build_user_content(
        session,
        messages,
        reflect_data,
        active_skill,
        skill_body,
        tool_summary,
    )

    model = settings.background_model or settings.llm_model
    if not model:
        stats["skipped_reason"] = "no_model_configured"
        return stats

    from core.llm.client import get_llm_client

    client = get_llm_client()

    system_prompt = REFINE_PROMPT
    if settings.adaptive_enabled:
        from core.adaptive.contract import ADAPTIVE_EDITS_PROMPT

        system_prompt = REFINE_PROMPT + ADAPTIVE_EDITS_PROMPT

    try:
        response = await client.chat(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            model=model,
            max_tokens=2048,
        )
        raw = (response.content or "").strip()
    except Exception as e:
        logger.warning("refine: LLM call failed for %s: %s", session_id, e)
        stats["skipped_reason"] = f"llm_error:{type(e).__name__}"
        return stats

    proposals, lessons, adaptive_edits, nothing_actionable = _parse_refine_output(raw)
    stats["nothing_actionable"] = nothing_actionable

    if adaptive_edits:
        from core.adaptive.contract import queue_producer_edits

        q = queue_producer_edits(
            adaptive_edits,
            "refine",
            session_id=session_id,
            rationale=f"refine pass on session {session_id[:12]} ({session.get('title', '?')[:40]})",
        )
        stats["adaptive_queued"] = q["queued"]
        stats["adaptive_gated"] = q["gated"]

    # Persist proposals only when an active skill is identified.
    if active_skill:
        for p in proposals:
            if not isinstance(p, dict):
                continue
            try:
                p_conf = float(p.get("confidence", 0.0) or 0.0)
            except (TypeError, ValueError):
                continue
            if p_conf < PROPOSAL_CONFIDENCE_FLOOR:
                continue
            p_skill = str(p.get("skill_name", "") or "").strip() or active_skill
            if p_skill != active_skill:
                logger.debug(
                    "refine: dropping proposal for unknown skill %r (active=%r)",
                    p_skill,
                    active_skill,
                )
                continue
            try:
                db.add_skill_proposal(
                    workflow_name=None,
                    run_id=None,
                    skill_name=p_skill,
                    section=str(p.get("section", "") or "").strip(),
                    problem=str(p.get("problem", "") or "").strip(),
                    proposed_change=str(p.get("proposed_change", "") or "").strip(),
                    confidence=p_conf,
                    source_origin="refine",
                    session_id=session_id,
                )
                stats["proposals_saved"] += 1
            except Exception as e:
                logger.warning("refine: could not persist proposal: %s", e)

    if lessons:
        from core.memory.store import get_memory_store

        store = get_memory_store()
        if store:
            for lesson in lessons:
                if not isinstance(lesson, dict):
                    continue
                content = str(lesson.get("content", "") or "").strip()
                if not content:
                    continue
                applies_when = str(lesson.get("applies_when", "") or "").strip()
                full_content = f"{content}\n\nApplies when: {applies_when}" if applies_when else content
                if store.is_duplicate(full_content):
                    continue
                base_tags = str(lesson.get("tags", "") or "").strip()
                tag_parts = ["lesson", "refine"]
                if active_skill:
                    tag_parts.append(active_skill)
                if base_tags:
                    tag_parts.append(base_tags)
                tags = ",".join(t for t in tag_parts if t)
                weight = lesson.get("weight", "normal")
                if weight not in ("high", "normal", "low"):
                    weight = "normal"
                try:
                    await asyncio.to_thread(
                        store.add_entry,
                        content=full_content,
                        entry_type="lesson",
                        tags=tags,
                        weight=weight,
                        source="refine",
                    )
                    stats["lessons_saved"] += 1
                except Exception as e:
                    logger.warning("refine: could not save lesson: %s", e)

    logger.info(
        "refine: session=%s nothing_actionable=%s proposals=%d lessons=%d",
        session_id,
        nothing_actionable,
        stats["proposals_saved"],
        stats["lessons_saved"],
    )
    return stats
