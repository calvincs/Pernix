"""Pernix — Whole-session refine pass.

The system's one session-improvement pass. It is not gated on the reflect
verdict: refine looks at any idle session for any worth-saving signal —
including ones the reflect pass deemed "pass with no deviation", and sessions
with no reflect verdict at all. (It replaced a narrower reflect-gated sibling,
`snooze_reflect`, whose selection overlapped this one's — two LLM calls over
the same sessions for the same artifacts.)

Triggered as the tail-end activity of snooze (Activity 13). Watermarks via
``snooze_state['refined:{sid}']`` so each session is processed at most once.

Hard rule: never auto-applies skill edits. All SKILL.md changes flow through
the existing proposals UI for human approve/deny.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from config import settings
from db import models as db

logger = logging.getLogger("pernix.refine")


# Bar for a paste-ready SKILL.md change. Lessons use no floor (advisory; scout
# already gates on hits).
PROPOSAL_CONFIDENCE_FLOOR: float = 0.6


def _norm_change(text: str) -> str:
    """Normalize a proposed_change for duplicate comparison: collapse
    whitespace, lowercase, first 240 chars."""
    return " ".join(text.split()).lower()[:240]


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


# Path references like `../skills/youtube-whisper/scripts/...` or
# `/app/data/skills/foo/SKILL.md` — the dominant way skills are invoked now
# that the scout pre-digests them into plans (the agent runs the script
# directly and never calls load_skill).
_SKILL_PATH_RE = re.compile(r"skills/([A-Za-z0-9][A-Za-z0-9_-]{1,63})/")


def _identify_active_skill(messages: list[dict]) -> str | None:
    """Best-effort: find the skill the session was operating under.

    Three signals, strongest first:
      1. The most recent `load_skill` tool call (explicit activation).
      2. `skills/<name>/` path references anywhere in the transcript —
         assistant tool_calls, tool results, scout plans. Most-referenced
         registered skill wins.
      3. A registered skill's name appearing verbatim in a scout message
         (the scout recommends skills by name in its plan).

    Signal 1 used to be the ONLY detector, which made proposals impossible
    for every scout-planned session — zero proposals ever reached the table
    on the live box while skills failed and got worked around in plain
    sight. Names are validated against the registry so a stray path can't
    route a proposal to a skill that doesn't exist.
    """
    # Signal 1: explicit load_skill (most recent wins).
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

    from core.skills.registry import get_skill_registry

    try:
        registry = get_skill_registry()
    except Exception:
        return None

    def _known(name: str) -> bool:
        try:
            return bool(registry.exists(name))
        except Exception:
            return False

    # Signal 2: skills/<name>/ path references across the whole transcript.
    path_counts: dict[str, int] = {}
    for m in messages:
        role = m.get("role")
        if role not in ("assistant", "tool", "scout"):
            continue
        blobs = [m.get("content") or ""]
        if role == "assistant" and m.get("tool_calls"):
            blobs.append(str(m["tool_calls"]))
        for blob in blobs:
            for name in _SKILL_PATH_RE.findall(blob):
                path_counts[name] = path_counts.get(name, 0) + 1
    known_paths = {n: c for n, c in path_counts.items() if _known(n)}
    if known_paths:
        return max(sorted(known_paths), key=lambda n: known_paths[n])

    # Signal 3: registered skill names mentioned by the scout's plan.
    scout_text = "\n".join((m.get("content") or "") for m in messages if m.get("role") == "scout")
    if scout_text:
        try:
            all_names = [s.name for s in registry.all_skills()]
        except Exception:
            all_names = []
        mention_counts: dict[str, int] = {}
        for name in all_names:
            n = len(re.findall(rf"(?<![A-Za-z0-9_-]){re.escape(name)}(?![A-Za-z0-9_-])", scout_text))
            if n:
                mention_counts[name] = n
        if mention_counts:
            return max(sorted(mention_counts), key=lambda n: mention_counts[n])

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
            if _is_failure_content(m.get("content") or ""):
                summary[tname]["failures"] = summary[tname].get("failures", 0) + 1

    return summary


def _is_failure_content(content: str) -> bool:
    """Does a tool result read as a failure?

    Beyond the classic "Error" prefix: detached-job status lines
    ("state=failed"), tracebacks, and non-zero exits — the shapes a failing
    skill script actually produces through job_status/job_tail.
    """
    head = content[:400]
    if content.startswith("Error") or "\nError" in head:
        return True
    if "state=failed" in head or "Traceback (most recent call last)" in head:
        return True
    if re.search(r"\bexit=(?!0\b)\d+", head):
        return True
    return False


def _build_failure_arc(messages: list[dict], cap: int = 10) -> tuple[str, int]:
    """Deterministic evidence extraction: every failing tool result, paired
    with the nearest preceding assistant narration (the intent behind it).

    Returns (rendered block, failure count). The old tail-only transcript
    window meant a long session's failure→workaround arc simply fell out of
    the prompt; this pins the failures into evidence regardless of where in
    the session they happened.
    """
    excerpts: list[str] = []
    seen: set[str] = set()
    total = 0
    last_narration = ""
    for m in messages:
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if role == "assistant" and content:
            last_narration = content
        elif role == "tool" and content and _is_failure_content(content):
            # Re-polling a failed job repeats the same status line — count
            # and render each distinct failure once.
            key = re.sub(r"\[as of [^\]]+\]", "", content[:300])
            if key in seen:
                continue
            seen.add(key)
            total += 1
            if len(excerpts) < cap:
                intent = f"  intent: {last_narration[:240]}\n" if last_narration else ""
                excerpts.append(f"- FAILURE {total}:\n{intent}  result: {content[:300]}")
    return ("\n".join(excerpts), total)


def _latest_reflect_verdict(messages: list[dict]) -> dict | None:
    """Return the most recent reflect message parsed as a dict, or None.

    Refine treats the verdict as optional context, not a precondition.
    """
    for m in reversed(messages):
        if m.get("role") != "reflect":
            continue
        try:
            return json.loads(m.get("content") or "{}")
        except (json.JSONDecodeError, TypeError):
            return None
    return None


def _parse_refine_output(raw: str) -> tuple[list[dict], list[dict], list[dict], list[dict], bool]:
    """Parse the LLM JSON into (proposals, lessons, adaptive_edits,
    canary_proposals, nothing_actionable). Tolerates fences. adaptive_edits
    (plan 4d) and canary_proposals (§12.2) ride the same call and the same
    parse — empty arrays while their features are off or nothing qualifies."""
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines and lines[-1].strip() == "```" else lines[1:])
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("refine: could not parse LLM output as JSON: %s\n%s", e, text[:500])
        return [], [], [], [], False
    if not isinstance(data, dict):
        return [], [], [], [], False
    proposals = data.get("proposals", []) or []
    lessons = data.get("lessons", []) or []
    adaptive_edits = data.get("adaptive_edits", []) or []
    canary_proposals = data.get("canary_proposals", []) or []
    if not isinstance(proposals, list):
        proposals = []
    if not isinstance(lessons, list):
        lessons = []
    if not isinstance(adaptive_edits, list):
        adaptive_edits = []
    if not isinstance(canary_proposals, list):
        canary_proposals = []
    # The contract's confidence floor and 2-edit cap, enforced mechanically —
    # prompt prose alone held neither. Edits without a confidence field
    # (older outputs) pass; an explicit low confidence does not.
    kept = []
    for e in adaptive_edits:
        if not isinstance(e, dict):
            continue
        try:
            conf = float(e["confidence"]) if "confidence" in e else None
        except (TypeError, ValueError):
            conf = None
        if conf is not None and conf < PROPOSAL_CONFIDENCE_FLOOR:
            logger.info("refine: adaptive edit below confidence floor (%.2f) dropped", conf)
            continue
        kept.append(e)
    adaptive_edits = kept[:2]
    nothing_actionable = bool(data.get("nothing_actionable"))
    return proposals, lessons, adaptive_edits, canary_proposals, nothing_actionable


def _build_user_content(
    session: dict,
    messages: list[dict],
    reflect_data: dict | None,
    active_skill: str | None,
    skill_body: str | None,
    tool_summary: dict,
) -> str:
    """Assemble the user-content blob fed to the LLM.

    reflect_data is optional — refine sees the whole transcript regardless of
    whether a reflect verdict was ever written.
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

    # The failure arc is assembled independently of the tail window: in a
    # long session the failures happen early and the workaround late, and a
    # tail-only view shows the LLM neither.
    task_head = next(
        (m.get("content", "")[:600] for m in messages if m.get("role") == "user" and (m.get("content") or "").strip()),
        "",
    )
    arc_block, failure_count = _build_failure_arc(messages)
    if failure_count:
        arc_section = (
            f"\nMACHINE SIGNAL: this session recorded {failure_count} failed tool/job "
            "result(s). If the transcript shows the task ultimately succeeding via a "
            "different path, that workaround is exactly what the governing skill is "
            "missing — capture it as a proposal (and a lesson).\n"
            f"\nFailure evidence (chronological, deduplicated):\n{arc_block}\n"
        )
    else:
        arc_section = ""

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
        f"Original task: {task_head}\n"
        f"{reflect_block}"
        f"\nTool summary:\n{tool_block}\n"
        f"{arc_section}"
        f"{skill_block}"
        f"\nConversation transcript (tail, truncated):\n{transcript}\n"
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

        system_prompt = system_prompt + ADAPTIVE_EDITS_PROMPT
    if settings.canary_enabled:
        from core.canary.propose import CANARY_PROPOSALS_PROMPT

        system_prompt = system_prompt + CANARY_PROPOSALS_PROMPT

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

    proposals, lessons, adaptive_edits, canary_proposals, nothing_actionable = _parse_refine_output(raw)
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

    if canary_proposals and settings.canary_enabled:
        try:
            from core.canary.propose import queue_canary_proposals

            stats["canary_proposed"] = queue_canary_proposals(canary_proposals, "refine", session_id=session_id)
        except Exception as e:
            logger.warning("refine: canary proposal queueing failed: %s", e)

    # Persist proposals only when an active skill is identified. Since the
    # watermark re-arms (a session can be refined again after it grows),
    # dedupe against every non-rejected proposal already on file for the
    # skill — the same session revisited must not mint the same change twice.
    if active_skill:
        existing_norms: set[str] = set()
        try:
            for prior in db.list_skill_proposals(skill_name=active_skill, limit=200):
                if prior.get("status") == "rejected":
                    continue
                existing_norms.add(_norm_change(prior.get("proposed_change") or ""))
        except Exception as e:
            logger.debug("refine: proposal dedupe lookup failed: %s", e)

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
            change_norm = _norm_change(str(p.get("proposed_change", "") or ""))
            if not change_norm or change_norm in existing_norms:
                stats["proposals_deduped"] = stats.get("proposals_deduped", 0) + 1
                continue
            existing_norms.add(change_norm)
            try:
                db.add_skill_proposal(
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

    stats["active_skill"] = active_skill
    logger.info(
        "refine: session=%s active_skill=%s nothing_actionable=%s proposals=%d deduped=%d lessons=%d",
        session_id,
        active_skill or "-",
        nothing_actionable,
        stats["proposals_saved"],
        stats.get("proposals_deduped", 0),
        stats["lessons_saved"],
    )
    return stats
