"""Pernix — Session-origin reflect-improvement extractor.

Mirrors core/workflows/reflect.py for non-workflow sessions. Runs from the
snooze cycle: picks one un-improvement-reviewed session whose final reflect
verdict has an actionable failure_cause, asks the background model to propose
both (a) skill improvements (human-reviewed via the existing proposals UI)
and (b) lessons (workarounds saved to memory as entry_type='lesson' so scout
can recall them when a future request looks similar).

Hard rule: never auto-applies skill edits. Trial-use signals only inform
approval — every change to SKILL.md still requires explicit user action.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from config import settings
from db import models as db

logger = logging.getLogger("pernix.snooze.reflect")


# Subset of core.reflect.FAILURE_CAUSES that warrants a snooze-time pass.
# `scout` and `none` are skipped — they don't tell us anything about a skill
# or about an operational workaround worth preserving.
ACTIONABLE_FAILURE_CAUSES: frozenset = frozenset({"skill", "agent", "task", "env"})

# Subset where a skill-edit proposal is meaningful (vs lesson-only).
PROPOSAL_FAILURE_CAUSES: frozenset = frozenset({"skill", "agent"})

# Confidence floor. Mirrors workflow_reflect's 0.6 floor for proposals.
PROPOSAL_CONFIDENCE_FLOOR: float = 0.6


SESSION_REFLECT_PROMPT = """You are a Session Reflect-Improvement Agent. A regular agent session
just produced a reflect verdict pointing to a real problem OR a pass-with-
noted-deviation (verdict=pass, but reflect populated strategy/diagnostic/
what_failed because the agent took a different path than planned). Read
the session summary, the reflect verdict, the active skill (if any), and
extract two kinds of durable artifacts:

1. PROPOSALS — concrete edits to the active skill's SKILL.md so this
   failure mode is addressed for future invocations. Tailor by failure_cause:
     * "skill" → add missing guidance, fix inaccurate steps, clarify preconditions
     * "agent" → add a concrete tool-usage example or pre-flight check
   Skip proposals if failure_cause is "task", "env", or "none" (those aren't skill issues).
   Skip if no active skill is identified.
   For pass-with-deviation (failure_cause="none" but verdict=pass with non-empty
   strategy/diagnostic/what_failed), DO NOT emit proposals — emit lessons only.

2. LESSONS — operational workarounds and recovery patterns worth recalling
   in similar future situations. Always extract at least one lesson when
   the verdict shows what worked or what was avoided. For pass-with-deviation
   sessions, the lesson should capture the deviation pattern (what was planned
   vs. what was done, and why the alternative still worked) so future scout
   plans can account for it.

Output a JSON object:
{
  "proposals": [
    {
      "skill_name": "the active skill",
      "section": "section of SKILL.md (e.g. 'Common Failures', 'Pre-flight', 'Usage')",
      "problem": "1-2 sentences describing what went wrong",
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
- proposed_change must be concrete, paste-ready prose. Reference real section names from the SKILL.md content.
- Lessons must be self-contained (understandable without the session). Include the trigger pattern in `applies_when` so future BM25 search can find them.
- If proposal confidence < 0.6, omit the proposal.
- If you have nothing useful, return {"proposals": [], "lessons": []}.
- Output valid JSON only. No markdown fences, no commentary. /no_think"""


def _latest_reflect_verdict(messages: list[dict]) -> dict | None:
    """Return the most recent reflect message parsed as a dict, or None."""
    for m in reversed(messages):
        if m.get("role") != "reflect":
            continue
        try:
            return json.loads(m.get("content") or "{}")
        except (json.JSONDecodeError, TypeError):
            return None
    return None


def _identify_active_skill(messages: list[dict]) -> str | None:
    """Best-effort: find the skill the session was operating under.

    Scans assistant tool_calls in reverse for the most recent successful
    `load_skill` invocation. Returns the skill name, or None.
    """
    for m in reversed(messages):
        if m.get("role") != "assistant":
            continue
        raw = m.get("tool_calls")
        if not raw:
            continue
        try:
            calls = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(calls, list):
            continue
        for call in calls:
            fn = (call or {}).get("function") or {}
            if fn.get("name") != "load_skill":
                continue
            args_raw = fn.get("arguments")
            if isinstance(args_raw, str):
                try:
                    args = json.loads(args_raw)
                except (json.JSONDecodeError, TypeError):
                    continue
            elif isinstance(args_raw, dict):
                args = args_raw
            else:
                continue
            name = args.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
    return None


def _build_tool_summary(messages: list[dict]) -> dict[str, dict[str, Any]]:
    """Reconstruct {tool_name -> {calls, failures}} from session messages.

    Reflect already builds this for live sessions but doesn't persist it, so
    we rebuild it from the message log. Failures = tool messages whose
    content starts with "Error" or contains a known failure marker.
    """
    summary: dict[str, dict[str, Any]] = {}
    pending_calls: dict[str, str] = {}

    for m in messages:
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            try:
                calls = json.loads(m["tool_calls"])
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(calls, list):
                continue
            for call in calls:
                fn = (call or {}).get("function") or {}
                tname = fn.get("name")
                tid = (call or {}).get("id")
                if not tname:
                    continue
                bucket = summary.setdefault(tname, {"calls": 0, "failures": 0})
                bucket["calls"] += 1
                if tid:
                    pending_calls[tid] = tname
        elif role == "tool":
            tid = m.get("tool_call_id")
            tname = pending_calls.pop(tid, None) if tid else None
            if not tname:
                continue
            content = m.get("content") or ""
            if content.startswith("Error") or "\nError" in content[:200]:
                summary[tname]["failures"] = summary[tname].get("failures", 0) + 1

    return summary


def _build_user_content(
    session: dict,
    messages: list[dict],
    reflect_data: dict,
    active_skill: str | None,
    skill_body: str | None,
    tool_summary: dict,
) -> str:
    """Assemble the user-content blob fed to the LLM."""
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
        else "\n(no active skill identified — emit lessons only)\n"
    )

    return (
        f"--- SESSION: {session.get('id')} ---\n"
        f"Title: {session.get('title', '?')}\n"
        f"Active skill: {active_skill or '(none)'}\n"
        f"\nReflect verdict:\n"
        f"  verdict: {reflect_data.get('verdict')}\n"
        f"  failure_cause: {reflect_data.get('failure_cause')}\n"
        f"  confidence: {reflect_data.get('confidence')}\n"
        f"  reasoning: {reflect_data.get('reasoning', '')}\n"
        f"  diagnostic: {reflect_data.get('diagnostic', '')}\n"
        f"  what_worked: {reflect_data.get('what_worked', '')}\n"
        f"  what_failed: {reflect_data.get('what_failed', '')}\n"
        f"  strategy: {reflect_data.get('strategy', '')}\n"
        f"\nTool summary:\n{tool_block}\n"
        f"{skill_block}"
        f"\nConversation transcript (truncated):\n{transcript}\n"
    )


def _parse_output(raw: str) -> tuple[list[dict], list[dict], list[dict]]:
    """Parse the LLM JSON into (proposals, lessons, adaptive_edits).
    Tolerates fences. adaptive_edits: same producer contract as refine
    (plan 4d) — same call, same parse."""
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines and lines[-1].strip() == "```" else lines[1:])
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("snooze_reflect: could not parse LLM output as JSON: %s\n%s", e, text[:500])
        return [], [], []
    if not isinstance(data, dict):
        return [], [], []
    proposals = data.get("proposals", []) or []
    lessons = data.get("lessons", []) or []
    adaptive_edits = data.get("adaptive_edits", []) or []
    if not isinstance(proposals, list):
        proposals = []
    if not isinstance(lessons, list):
        lessons = []
    if not isinstance(adaptive_edits, list):
        adaptive_edits = []
    return proposals, lessons, adaptive_edits


async def run_for_session(session_id: str) -> dict:
    """Extract proposals + lessons for one session.

    Returns a stats dict: {proposals_saved, lessons_saved, skipped_reason}.
    Always idempotent w.r.t. snooze_state — caller is responsible for setting
    `proposal_reviewed:{sid}` after this returns (so a failure here can be
    retried on the next snooze cycle if desired, or marked-and-skipped to
    avoid retry storms — we follow _catchup_distill's mark-on-failure pattern).
    """
    stats = {"proposals_saved": 0, "lessons_saved": 0, "skipped_reason": None}

    session = db.get_session(session_id)
    if not session:
        stats["skipped_reason"] = "session_not_found"
        return stats

    if session.get("session_type") == "worker":
        stats["skipped_reason"] = "worker_session"  # workflows already self-improve
        return stats

    messages = db.get_messages(session_id)
    reflect_data = _latest_reflect_verdict(messages)
    if not reflect_data:
        stats["skipped_reason"] = "no_reflect_verdict"
        return stats

    failure_cause = reflect_data.get("failure_cause", "none")
    verdict = reflect_data.get("verdict", "pass")
    # "Pass with deviation": reflect picked verdict=pass but populated retry-shaped
    # fields (strategy/diagnostic/what_failed). The deliverable shipped, so we
    # don't flip the verdict, but the LLM still flagged something worth carrying
    # forward — admit it into the lesson-extraction path. Proposals are still
    # gated on PROPOSAL_FAILURE_CAUSES below, so a pass-with-deviation produces
    # lessons only.
    pass_with_deviation = verdict == "pass" and any(
        (reflect_data.get(f) or "").strip() for f in ("strategy", "diagnostic", "what_failed")
    )
    if failure_cause not in ACTIONABLE_FAILURE_CAUSES and not pass_with_deviation:
        stats["skipped_reason"] = f"non_actionable_cause:{failure_cause}"
        return stats

    confidence = float(reflect_data.get("confidence", 0.0) or 0.0)
    if confidence < 0.5:
        stats["skipped_reason"] = "low_reflect_confidence"
        return stats

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

    system_prompt = SESSION_REFLECT_PROMPT
    if settings.adaptive_enabled:
        from core.adaptive.contract import ADAPTIVE_EDITS_PROMPT

        system_prompt = SESSION_REFLECT_PROMPT + ADAPTIVE_EDITS_PROMPT

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
        logger.warning("snooze_reflect: LLM call failed for %s: %s", session_id, e)
        stats["skipped_reason"] = f"llm_error:{type(e).__name__}"
        return stats

    proposals, lessons, adaptive_edits = _parse_output(raw)

    if adaptive_edits:
        from core.adaptive.contract import queue_producer_edits

        q = queue_producer_edits(
            adaptive_edits,
            "snooze_reflect",
            session_id=session_id,
            rationale=f"snooze_reflect on session {session_id[:12]}",
        )
        stats["adaptive_queued"] = q["queued"]
        stats["adaptive_gated"] = q["gated"]

    # Persist proposals (only if we have an active skill and cause is in PROPOSAL_FAILURE_CAUSES).
    if active_skill and failure_cause in PROPOSAL_FAILURE_CAUSES:
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
            # Only accept proposals targeting the identified skill — a hallucinated
            # name shouldn't get to write into a different skill's review queue.
            if p_skill != active_skill:
                logger.debug(
                    "snooze_reflect: dropping proposal for unknown skill %r (active=%r)",
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
                    source_origin="session",
                    session_id=session_id,
                )
                stats["proposals_saved"] += 1
            except Exception as e:
                logger.warning("snooze_reflect: could not persist proposal: %s", e)

    # Persist lessons regardless of whether a proposal was extracted.
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
                tag_parts = ["lesson", failure_cause]
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
                        source="snooze_reflect",
                    )
                    stats["lessons_saved"] += 1
                except Exception as e:
                    logger.warning("snooze_reflect: could not save lesson: %s", e)

    logger.info(
        "snooze_reflect: session=%s cause=%s proposals=%d lessons=%d",
        session_id,
        failure_cause,
        stats["proposals_saved"],
        stats["lessons_saved"],
    )
    return stats
