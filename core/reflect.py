"""Pernix — Reflect: post-execution verification agent.

Runs after each agent turn to verify the user's request was fulfilled.
If not, produces actionable lessons and triggers a retry.

Lifecycle: Scout → Agent → Post-hooks → Reflect → (retry if needed)
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

from config import settings
from db import models as db

logger = logging.getLogger("pernix.reflect")

REFLECT_PROMPT = """You are a Reflect Agent. You review completed agent sessions to verify the user's request was fulfilled.

Given: the user's original message, the agent's final response, any files created, scout's deliverables plan (if any), and a tool execution summary with per-tool call counts, failures, and the last few error messages.

Output a JSON object:
- verdict: "pass" | "retry" | "escalate"
  - pass: task was completed satisfactorily
  - retry: task was NOT completed, and another attempt could succeed
  - escalate: task cannot be completed without user input
- reasoning: 1-2 sentences explaining your verdict
- failure_cause: One of "none" | "scout" | "agent" | "skill" | "task" | "env". Use "none" if verdict is pass. Otherwise attribute the failure: "scout" (plan was wrong/incomplete), "agent" (plan was fine but execution was poor), "skill" (a recommended skill was broken/outdated), "task" (user request was ambiguous or impossible), "env" (network, permissions, rate limit, missing external resource).
- confidence: float 0.0-1.0 — how confident you are in this verdict and failure_cause. Use <0.5 when evidence is ambiguous.
- deliverables: Array of {description, status: "met"|"partial"|"unmet"|"unknown", evidence_ref: string}. If scout provided a deliverables_plan, grade each item. If not, synthesize from the user's ask. evidence_ref should be a file path, task id, worker summary path, or a short quote pointing to what proves the status.
- diagnostic: If retry — root cause analysis. Name the specific failure pattern. Empty string if pass.
- what_worked: If retry — tools/approaches that produced useful results (carry forward). Empty string if pass.
- what_failed: If retry — tools/approaches that failed or wasted time (avoid). Empty string if pass.
- strategy: If retry — concrete instruction for the retry attempt. Must propose a DIFFERENT approach if the same tools failed repeatedly. Empty string if pass.
- missing: If escalate — what specific information or clarification is needed from the user. Empty string otherwise.

RULES:
- Be strict: if the user asked for a file/deliverable and none was created, that's a retry.
- Be strict: if the user asked to use a specific tool or approach and the agent used something else, that's a retry.
- Be fair: if the agent produced a reasonable answer even without creating files, that can be a pass.
- Be concise: the retry strategy must be actionable, not vague.
- Verdict "pass" should be the default for conversational exchanges, questions answered, simple requests fulfilled.
- Do NOT retry for partial success — only for clear failures to meet the user's core request.
- TRUST THE PLAN: If the evidence includes ACTIVE SKILL / PLANNED APPROACH / TOOL RATIONALE, treat those as the contract the agent was given. If the agent followed the planned approach using the planned tools, do NOT call hallucination just because the tools look "generic" (e.g. browse_web). Skills routinely mandate generic tools — that's expected, not a failure. Only flag hallucination when the agent invented data with no supporting tool calls AND the plan called for a tool that wasn't run.
- Use the TOOL EXECUTION SUMMARY to identify failure patterns. If a tool failed 2+ times with the same error, the retry strategy MUST suggest a different tool or approach.
- TOOL CALL FACTS ARE NOT NEGOTIABLE. The TOOL EXECUTION SUMMARY is observed truth, not interpretation. Before claiming the agent did NOT use a tool, verify: if the tool name appears in the summary with calls > 0, the agent DID call it. Do NOT write "agent did not call X" or "agent failed to use X" or "X was skipped" if X.calls > 0 in the summary. If you believe the call was *ineffective* (tool ran but didn't produce the expected result), say that — but do not deny the call happened. Hallucinating absence of a call that the summary records as present is a verifier-side correctness failure.
- TOOL EXHAUSTION: If the summary shows a high number of calls across many different tools, the agent may have run out of tool rounds rather than used the wrong approach. Prefer "pass" with partial results if real progress was made — UNLESS the user named a specific deliverable (file, report, message sent, workflow result) and that deliverable does not exist. The deliverable-missing rule above ALWAYS overrides this exhaustion clause; "we made progress" is not a substitute for "we produced what was asked for."
- WORKFLOW RUNS: If the WORKFLOW RUNS section is present, treat its `status` field as authoritative for whether the named workflow finished. status='running' or status='failed' both mean the workflow did NOT produce its terminal output, regardless of how many intermediate scratch files were written to the workspace — those are inputs to later steps, not deliverables. status='partial'/'failed'/'running' for a user-named workflow → retry (or escalate if the same failure has already occurred). status='complete' AND the terminal-step output_file is listed under WORKSPACE FILES → pass.
- THRASHING → ESCALATE: If the summary shows ≥4 distinct tools used with no forward progress (empty outputs repeated, same files re-read, error count growing, or the agent drifting across unrelated checks), the agent is thrashing, not pursuing a coherent-but-wrong strategy. Prefer "escalate" with a clear "missing" field over "retry" — another round of the same thrashing will not help. A single failing tool being tried with sibling tools is NOT thrashing; reserve "escalate" for cases where the agent lost the thread.
- For failure_cause attribution: if tools were hallucinated or the plan jumped straight to the wrong approach, lean "scout". If the plan was sensible but the agent used tools incorrectly or gave up early, lean "agent". Don't guess — use "none" with low confidence if unclear.

EVIDENCE QUALITY (outcome vs. execution):
- Distinguish "execution evidence" (a tool call returned exit 0, a file was written, a worker was dispatched) from "outcome evidence" (the file's contents satisfy the request, the side effect is observable, the worker reported success).
- For deliverables that imply an observable real-world side effect — playing media, sending a message, deploying, scheduling, casting to a device, starting a background process — execution evidence is NOT sufficient. Status must be "partial" or "unknown" (not "met") and confidence ≤ 0.7 unless the agent also performed a verification step (status query, content read-back, follow-up tool call confirming the effect).
- Background launches without verification: bash commands ending in `&`, scripts that fork a watcher (e.g. "Watcher PID: 12345"), or any "fire and forget" pattern set a deliverable's status to AT MOST "partial". Note this in evidence_ref (e.g. "watcher PID returned, no follow-up status check").
- A deliverable whose only evidence is "tool exited 0 with no readable output" should be "unknown" with low confidence — not "met".
- FORMAT CONSTRAINTS: When the user or an active skill specifies output format rules (e.g., "no markdown", "plain text only", "no bold"), these constraints apply to the *deliverable artifact content* — the file written, the post produced, the data returned. They do NOT apply to the agent's own status updates, headers, or explanatory text in its final chat response, unless the user's request explicitly says so (e.g., "don't use markdown in your reply"). If a file on disk satisfies the format constraint but the agent's response summary uses headers or bold, that is NOT a violation. When evaluating a format constraint, check WORKSPACE FILES for the deliverable path and treat its content as the ground truth, not the agent's prose wrapper.
- Output valid JSON only. No markdown fences, no explanation outside the JSON. /no_think"""


@dataclass
class Deliverable:
    """One item from the session's deliverables list.

    Used by the post-mortem artifact so downstream consumers (eval harness,
    snooze) can check completion without re-reading the transcript.
    """

    description: str = ""
    status: str = "unknown"  # met | partial | unmet | unknown
    evidence_ref: str = ""  # path, task_id, worker_id, or free-form pointer


# Valid values for ReflectResult.failure_cause.
# Kept as a frozen set so callers can check membership without importing Literal.
FAILURE_CAUSES: frozenset = frozenset(
    {
        "none",  # no failure — verdict was pass
        "scout",  # scout's plan was wrong or incomplete
        "agent",  # agent executed the plan poorly
        "skill",  # a recommended skill was broken/outdated
        "task",  # task was ambiguous or impossible as stated
        "env",  # environmental issue (network, permissions, missing files, rate limit)
    }
)


@dataclass
class ReflectResult:
    """Result from Reflect verification.

    Schema extended with structured-attribution fields (failure_cause,
    confidence, deliverables, artifact_id) as groundwork for the feedback
    loop (Phase 2c writes these into post_mortems; Phase 3 snooze consumes).
    Older consumers continue reading verdict/reasoning/lessons as before.
    """

    verdict: str = "pass"  # pass | retry | escalate
    reasoning: str = ""
    diagnostic: str = ""  # root cause: approach vs environmental problem
    what_worked: str = ""  # carry forward on retry
    what_failed: str = ""  # avoid on retry
    strategy: str = ""  # retry instruction
    missing: str = ""  # escalate: what's needed from user
    reflect_model: str = ""
    reflect_latency_ms: int = 0

    # Structured attribution (populated from reflect output; defaults are safe).
    failure_cause: str = "none"
    confidence: float = 0.0  # 0.0–1.0
    deliverables: list = field(default_factory=list)  # list[Deliverable]
    artifact_id: str = ""  # set by post_mortems writer


def build_lessons_context(
    result: ReflectResult, attempt: int, max_attempts: int, tool_summary: dict | None = None
) -> str:
    """Build a lessons-learned string for injection into the next scout/agent cycle."""
    parts = [f"[REFLECT — Retry #{attempt} of {max_attempts}]"]
    parts.append(f"Previous attempt did not complete the task: {result.reasoning}")
    if result.diagnostic:
        parts.append(f"Root cause diagnosis: {result.diagnostic}")
    if result.what_worked:
        parts.append(f"What worked (carry forward): {result.what_worked}")
    if result.what_failed:
        parts.append(f"What failed (DO NOT repeat): {result.what_failed}")
    if result.strategy:
        parts.append(f"Strategy for this attempt: {result.strategy}")

    # Tell the scout and agent that all prior turn tool outputs are still live in
    # the conversation context — they must NOT re-read files or re-run searches
    # that are already visible in the message history.
    parts.append(
        "CONTEXT CARRYOVER: The prior turn's complete message history — including all "
        "file reads and tool results — is still present in the agent's conversation "
        "context. Do NOT re-read files or repeat web searches that were already "
        "executed. The agent can reference those results directly."
    )

    # Append a concrete list of tools already invoked so the scout can suppress
    # redundant steps in its approach guidance.
    if tool_summary:
        used = sorted(
            (name for name, stats in tool_summary.items() if stats.get("calls", 0) > 0),
        )
        if used:
            parts.append(f"Tools already used in prior turn: {', '.join(used)}")

    return "\n".join(parts)


def _format_message(msg: dict) -> str:
    """Format a single message as a readable transcript line."""
    role = msg.get("role", "unknown")
    content = msg.get("content") or ""

    if role == "user":
        return f"[USER]\n{content}"
    elif role == "assistant":
        parts = []
        if content:
            parts.append(f"[ASSISTANT]\n{content}")
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            try:
                tcs = json.loads(tool_calls) if isinstance(tool_calls, str) else tool_calls
                for tc in (tcs if isinstance(tcs, list) else []):
                    fn = tc.get("function", {})
                    name = tc.get("name") or fn.get("name", "?")
                    args = tc.get("arguments") or fn.get("arguments", {})
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except (json.JSONDecodeError, ValueError):
                            pass
                    args_str = json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else str(args)
                    parts.append(f"[TOOL CALL: {name}]\nArguments: {args_str}")
            except (json.JSONDecodeError, TypeError):
                pass
        return "\n".join(parts) if parts else "[ASSISTANT]\n(empty)"
    elif role == "tool":
        return f"[TOOL RESULT]\n{content}"
    elif role == "system":
        return f"[SYSTEM]\n{content}"
    elif role == "reflect":
        return f"[REFLECT]\n{content}"
    else:
        return f"[{role.upper()}]\n{content}"


# Default reflect context budget (tokens). If conversation exceeds this,
# older messages are summarized to fit.
_REFLECT_CONTEXT_BUDGET = 100_000


def _build_compact_evidence(
    session_id: str,
    user_request: str,
    messages: list[dict],
    attempt: int,
    tool_summary: dict | None,
    scout_report=None,
) -> str:
    """Build a compact evidence blob for reflect.

    Drops the mid-conversation transcript — reflect rarely needs it, and when
    it does the tool execution summary (already capturing errors) carries the
    signal. Includes: user ask, final assistant message, workspace files,
    scout deliverables_plan (if any), and the structured tool summary.
    """
    from pathlib import Path

    # Last assistant message — the final answer the user actually sees.
    final_assistant = ""
    for msg in reversed(messages):
        if msg["role"] == "assistant" and msg.get("content"):
            final_assistant = msg["content"]
            break

    parts: list[str] = []
    if attempt > 1:
        parts.append(f"REFLECT CONTEXT: attempt #{attempt}. If same issues persist, " "prefer 'escalate' over 'retry'.")

    # Workspace files (top 20 by mtime)
    workspace = Path(settings.workspace_dir)
    ws_files = []
    if workspace.exists():
        candidates = [
            f
            for f in workspace.rglob("*")
            if f.is_file() and not any(p.startswith(".") for p in f.relative_to(workspace).parts)
        ]
        candidates.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        ws_files = [str(f.relative_to(workspace)) for f in candidates[:20]]

    parts.append("WORKSPACE FILES:\n" + ("\n".join(f"- {f}" for f in ws_files) if ws_files else "(none)"))

    # Scout's deliverables_plan — gives reflect a concrete checklist
    if scout_report is not None and getattr(scout_report, "deliverables_plan", None):
        deliv_lines = ["SCOUT DELIVERABLES PLAN (grade each):"]
        for i, d in enumerate(scout_report.deliverables_plan, 1):
            deliv_lines.append(f"  {i}. {d.description} [hint: {d.execution_hint}]")
        parts.append("\n".join(deliv_lines))

    # Scout's planned approach — what tools/skills the agent was *supposed*
    # to use. Without this, reflect grades the agent's tool choice against its
    # own priors, which produces false-negative retries when a skill mandates
    # a "generic" tool (e.g. nws-weather-forecast skill says use browse_web —
    # reflect previously called this hallucination).
    if scout_report is not None:
        approach_lines = []
        skill_name = getattr(scout_report, "injected_skill_name", "") or ""
        if skill_name:
            approach_lines.append(f"ACTIVE SKILL: {skill_name}")
        approach_text = (getattr(scout_report, "approach_guidance", "") or "").strip()
        if approach_text:
            approach_lines.append(f"PLANNED APPROACH (from scout):\n{approach_text}")
        rationale = (getattr(scout_report, "tool_rationale", "") or "").strip()
        if rationale:
            approach_lines.append(f"TOOL RATIONALE: {rationale}")
        if approach_lines:
            parts.append("\n\n".join(approach_lines))

    # Tool execution summary with last few errors per tool
    if tool_summary:
        summary_lines = ["TOOL EXECUTION SUMMARY:"]
        for tool_name, stats in sorted(tool_summary.items()):
            summary_lines.append(
                f"- {tool_name}: {stats['calls']} call(s), "
                f"{stats['failures']} failure(s), {stats['total_latency_ms']}ms total"
            )
            for err in stats.get("errors", [])[:5]:
                summary_lines.append(f"    ERROR: {err}")
        parts.append("\n".join(summary_lines))

    # Workflow runs invoked during this session. Reflect uses workflow_runs.status
    # as the authoritative answer to "did the workflow finish?" — without this,
    # the LLM can be misled by intermediate scratch files in the workspace and
    # mark the turn 'pass' for a workflow that orphaned mid-flight.
    wf_lines = _collect_workflow_runs_for_session(messages)
    if wf_lines:
        parts.append("WORKFLOW RUNS:\n" + "\n".join(wf_lines))

    # User's ask (echoed) and the agent's final response
    parts.append(f"USER REQUEST:\n{user_request}")
    parts.append(f"AGENT FINAL RESPONSE:\n{final_assistant or '(no final assistant message)'}")

    return "\n\n".join(parts)


_WF_TOOL_RUN_ID_RE = __import__("re").compile(r"Workflow\s+'([^']+)'\s+run\s+([0-9a-f]{8})")


def _collect_workflow_runs_for_session(messages: list[dict]) -> list[str]:
    """Pull workflow run rows that this session triggered.

    Walks the session's tool-message responses (each `run_workflow` call's
    return value embeds the run_id in its summary header) and looks each one
    up in the workflow_runs table to read authoritative status. Returns
    formatted lines suitable for inline injection into reflect evidence.

    Empty list if the session never called run_workflow. Errors swallowed —
    this is best-effort enrichment, not a correctness gate.
    """
    run_ids: list[tuple[str, str]] = []
    seen: set[str] = set()
    for msg in messages:
        if msg.get("role") != "tool":
            continue
        content = msg.get("content") or ""
        m = _WF_TOOL_RUN_ID_RE.search(content)
        if not m:
            continue
        wf_name, rid = m.group(1), m.group(2)
        if rid in seen:
            continue
        seen.add(rid)
        run_ids.append((wf_name, rid))

    if not run_ids:
        return []

    lines: list[str] = []
    for wf_name, rid in run_ids:
        try:
            row = db.get_workflow_run(rid)
        except Exception:
            row = None
        if not row:
            lines.append(f"- {wf_name} run {rid}: <row not found in DB>")
            continue
        status = row.get("status", "?")
        passed = row.get("steps_passed", 0)
        failed = row.get("steps_failed", 0)
        total = row.get("step_count", 0)
        completed = row.get("completed_at") or "<not finalized>"
        lines.append(
            f"- {wf_name} run {rid}: status={status}, "
            f"steps_passed={passed}/{total}, steps_failed={failed}, "
            f"completed_at={completed}"
        )
    return lines


def _build_evidence(
    session_id: str,
    attempt: int = 1,
    tool_summary: dict | None = None,
    scout_report=None,
    turn_user_msg_id: int | None = None,
) -> tuple[str, str]:
    """Build evidence for reflect verification.

    Default path (reflect_full_transcript=False): compact — user ask,
    final agent message, workspace files, deliverables plan, tool summary.
    Escape hatch (reflect_full_transcript=True): full transcript with
    summarization of older messages when over budget.

    `turn_user_msg_id` scopes the evidence to the user message that triggered
    THIS turn — without it, reflect would grade against the latest user
    message, which can be a queued one for a future turn.

    Returns (user_request, evidence_summary).
    """
    from core.context.tokens import get_estimator

    messages = db.get_messages(session_id)
    if not messages:
        return "", ""

    # Find the user message being verified. Prefer the explicit id (this turn's
    # user message). Fall back to the latest user message overall.
    user_request = ""
    if turn_user_msg_id is not None:
        for msg in messages:
            if msg.get("id") == turn_user_msg_id and msg["role"] == "user":
                user_request = msg.get("content", "")
                break
    if not user_request:
        for msg in reversed(messages):
            if msg["role"] == "user":
                user_request = msg.get("content", "")
                break

    if not user_request:
        return "", ""

    # Compact path (default) — no transcript, signal from tool summary + deliverables.
    if not settings.reflect_full_transcript:
        evidence = _build_compact_evidence(
            session_id,
            user_request,
            messages,
            attempt,
            tool_summary,
            scout_report,
        )
        return user_request, evidence

    # Below: legacy full-transcript path, kept as opt-in for debugging.

    # Check for workspace files created during this session
    from pathlib import Path

    workspace = Path(settings.workspace_dir)
    ws_files = []
    if workspace.exists():
        # List recent non-hidden files (top 20 by mtime)
        candidates = [
            f
            for f in workspace.rglob("*")
            if f.is_file() and not any(p.startswith(".") for p in f.relative_to(workspace).parts)
        ]
        candidates.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        ws_files = [str(f.relative_to(workspace)) for f in candidates[:20]]

    # Build preamble
    preamble_parts = []
    if attempt > 1:
        preamble_parts.append(
            f"REFLECT CONTEXT: This is attempt #{attempt}. Previous attempt(s) "
            "did not complete the task. If same issues persist, consider verdict "
            "'escalate' instead of 'retry'."
        )

    if ws_files:
        preamble_parts.append("WORKSPACE FILES:\n" + "\n".join(f"- {f}" for f in ws_files))
    else:
        preamble_parts.append("WORKSPACE FILES: None")

    # Inject structured tool execution summary for diagnostic analysis
    if tool_summary:
        summary_lines = ["TOOL EXECUTION SUMMARY:"]
        for tool_name, stats in sorted(tool_summary.items()):
            line = f"- {tool_name}: {stats['calls']} call(s), {stats['failures']} failure(s), {stats['total_latency_ms']}ms total"
            summary_lines.append(line)
            # Show each distinct error message (up to 5) on its own line for readability
            for err in stats.get("errors", [])[:5]:
                summary_lines.append(f"    ERROR: {err}")
        preamble_parts.append("\n".join(summary_lines))

    preamble = "\n\n".join(preamble_parts)

    # Format all messages as a conversation transcript
    # Exclude scout/compaction metadata and audit notices — reflect doesn't need them
    substantive = [m for m in messages if m["role"] not in ("compaction", "scout", "notice")]
    transcript_lines = [_format_message(m) for m in substantive]
    transcript = "\n\n---\n\n".join(transcript_lines)

    evidence = f"{preamble}\n\n{'=' * 60}\nFULL CONVERSATION TRANSCRIPT\n{'=' * 60}\n\n{transcript}"

    # Check if evidence fits within budget
    estimator = get_estimator()
    evidence_tokens = estimator.count(evidence)

    if evidence_tokens <= _REFLECT_CONTEXT_BUDGET:
        return user_request, evidence

    # Conversation too large — summarize older messages, keep recent ones in full.
    # Strategy: keep the last N messages in full (where N messages fit in ~60% of
    # budget), summarize everything before that.
    recent_budget = int(_REFLECT_CONTEXT_BUDGET * 0.6)
    preamble_tokens = estimator.count(preamble)

    # Walk backwards to find how many recent messages fit
    recent_messages = []
    recent_tokens = 0
    for msg in reversed(substantive):
        formatted = _format_message(msg)
        msg_tokens = estimator.count(formatted)
        if recent_tokens + msg_tokens > recent_budget:
            break
        recent_messages.insert(0, formatted)
        recent_tokens += msg_tokens

    # Everything not in recent gets summarized as a condensed overview
    older_count = len(substantive) - len(recent_messages)
    older_messages = substantive[:older_count]

    # Build a condensed summary of older messages
    older_summary_parts = []
    for msg in older_messages:
        role = msg.get("role", "unknown")
        content = msg.get("content") or ""
        if role == "user":
            older_summary_parts.append(f"- [USER] {content[:300]}")
        elif role == "assistant":
            preview = content[:200] if content else ""
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                try:
                    tcs = json.loads(tool_calls) if isinstance(tool_calls, str) else tool_calls
                    names = [
                        tc.get("name") or tc.get("function", {}).get("name", "?")
                        for tc in (tcs if isinstance(tcs, list) else [])
                    ]
                    older_summary_parts.append(
                        f"- [ASSISTANT] called: {', '.join(names)}" + (f" | text: {preview}" if preview else "")
                    )
                except (json.JSONDecodeError, TypeError):
                    older_summary_parts.append(f"- [ASSISTANT] {preview}")
            elif preview:
                older_summary_parts.append(f"- [ASSISTANT] {preview}")
        elif role == "tool":
            # Include first 300 chars of tool results in summary
            older_summary_parts.append(f"- [TOOL RESULT] {content[:300]}")
        elif role == "system":
            older_summary_parts.append(f"- [SYSTEM] {content[:200]}")

    older_summary = "\n".join(older_summary_parts)
    recent_transcript = "\n\n---\n\n".join(recent_messages)

    evidence = (
        f"{preamble}\n\n"
        f"{'=' * 60}\n"
        f"EARLIER CONVERSATION ({older_count} messages, condensed)\n"
        f"{'=' * 60}\n\n"
        f"{older_summary}\n\n"
        f"{'=' * 60}\n"
        f"RECENT CONVERSATION (full detail)\n"
        f"{'=' * 60}\n\n"
        f"{recent_transcript}"
    )

    return user_request, evidence


def _try_repair_json(raw: str) -> dict | None:
    """Attempt to repair truncated or malformed JSON from reflect output.

    Handles common issues: truncated strings, missing closing braces,
    trailing commas.
    """
    import re

    text = raw.strip()
    if not text:
        return None

    # Strip markdown fences if present
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    # Strip thinking tags if model included them
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

    # If it doesn't start with {, try to find the JSON object
    start = text.find("{")
    if start == -1:
        return None
    text = text[start:]

    # Try as-is first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Truncated string: find last complete key-value, close the string and object
    # Walk backwards to find the last complete value
    for trim_pos in range(len(text) - 1, 0, -1):
        candidate = text[:trim_pos].rstrip().rstrip(",")
        # If we're in a truncated string, close it
        # Count unescaped quotes after the last key
        if candidate.count('"') % 2 != 0:
            candidate += '"'
        # Ensure object is closed
        open_braces = candidate.count("{") - candidate.count("}")
        candidate += "}" * open_braces
        try:
            result = json.loads(candidate)
            if isinstance(result, dict) and "verdict" in result:
                logger.info("Repaired truncated reflect JSON (trimmed %d chars)", len(text) - trim_pos)
                return result
        except json.JSONDecodeError:
            continue

    return None


def _result_from_data(data: dict, model: str, latency_ms: int) -> ReflectResult:
    """Build ReflectResult from parsed JSON data."""
    result = ReflectResult(
        verdict=data.get("verdict", "pass"),
        reasoning=data.get("reasoning", ""),
        diagnostic=data.get("diagnostic", ""),
        what_worked=data.get("what_worked", ""),
        what_failed=data.get("what_failed", ""),
        strategy=data.get("strategy", ""),
        missing=data.get("missing", ""),
        reflect_model=model,
        reflect_latency_ms=latency_ms,
    )
    if result.verdict not in ("pass", "retry", "escalate"):
        # Coerce invalid verdict — but to "retry", NOT "pass". Defaulting to
        # pass on garbage was historically dangerous: workflow run e8c94b86
        # (2026-04-27) had reflect emit verdict='fail' (not in the schema)
        # while the reasoning correctly noted the deliverable was missing,
        # and the coercion silently flipped it to pass. The orchestrator's
        # pass-but-no-output check then had to clean up; for callers that
        # don't have that guard, the workflow would have shipped a fake-pass.
        # "retry" is the correct default for malformed verdicts: the model
        # tried to say something other than pass, so don't pretend it said
        # pass. The retry budget will catch genuinely-broken cases.
        logger.warning(
            "Invalid reflect verdict %r (%s), coercing to 'retry'",
            result.verdict,
            (result.reasoning or "")[:120],
        )
        result.verdict = "retry"

    # Structured attribution (optional — reflect prompt will be updated to emit
    # these in Phase 2. For now, accept them if present and default otherwise).
    cause = data.get("failure_cause", "none")
    if cause not in FAILURE_CAUSES:
        logger.debug("Unknown failure_cause %r from reflect, defaulting to none", cause)
        cause = "none"
    # If verdict says failure but cause defaulted to none, mark unknown via "env"
    # fallback? No — keep strict; downstream treats "none" on non-pass as missing.
    result.failure_cause = cause

    try:
        conf = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    result.confidence = max(0.0, min(1.0, conf))

    raw_deliv = data.get("deliverables") or []
    if isinstance(raw_deliv, list):
        for d in raw_deliv:
            if not isinstance(d, dict):
                continue
            status = d.get("status", "unknown")
            if status not in ("met", "partial", "unmet", "unknown"):
                status = "unknown"
            result.deliverables.append(
                Deliverable(
                    description=str(d.get("description", ""))[:500],
                    status=status,
                    evidence_ref=str(d.get("evidence_ref", ""))[:500],
                )
            )

    return result


def _has_pass_with_lessons(result: ReflectResult) -> bool:
    """True when verdict=pass but the model populated retry-shaped fields.

    The reflect prompt requires strategy/diagnostic/what_failed to be empty on
    a pass verdict, so a non-empty value here means the model contradicted
    itself: it noticed something worth carrying forward, but still let the
    turn pass. Snooze uses this signal to extract a lesson.
    """
    if result.verdict != "pass":
        return False
    return any((getattr(result, f) or "").strip() for f in ("strategy", "diagnostic", "what_failed"))


def _write_post_mortem(
    session_id: str,
    attempt: int,
    result: ReflectResult,
    scout_report,
    tool_summary: dict | None,
    extra_payload: dict | None = None,
) -> None:
    """Persist a post-mortem artifact for this reflect invocation (Phase 2c).

    Always called — pass, retry, escalate, parse-failure. Never raises.
    The artifact is what snooze consumes; a missing row means silent signal
    loss, so we log failures but never let them propagate.

    extra_payload merges into the JSON payload (e.g. parse_error markers).
    """
    try:
        payload = {
            "verdict": result.verdict,
            "reasoning": result.reasoning,
            "diagnostic": result.diagnostic,
            "what_worked": result.what_worked,
            "what_failed": result.what_failed,
            "strategy": result.strategy,
            "missing": result.missing,
            "failure_cause": result.failure_cause,
            "confidence": result.confidence,
            "deliverables": [
                {"description": d.description, "status": d.status, "evidence_ref": d.evidence_ref}
                for d in result.deliverables
            ],
            "tool_summary": tool_summary or {},
        }
        if extra_payload:
            payload.update(extra_payload)
        scout_viability = None
        execution_mode = None
        if scout_report is not None:
            scout_viability = getattr(scout_report, "viability", None)
            execution_mode = getattr(scout_report, "execution_mode", None)
            payload["scout_summary"] = {
                "viability": scout_viability,
                "viability_notes": list(getattr(scout_report, "viability_notes", []) or []),
                "execution_mode": execution_mode,
                "recommended_tools": list(scout_report.recommended_tools or []),
                "recommended_skills": list(scout_report.recommended_skills or []),
                "deliverables_plan": [
                    {"description": d.description, "execution_hint": d.execution_hint}
                    for d in (scout_report.deliverables_plan or [])
                ],
                "from_cache": bool(getattr(scout_report, "from_cache", False)),
                "from_fallback": bool(getattr(scout_report, "from_fallback", False)),
            }

        pm_id = db.add_post_mortem(
            session_id=session_id,
            attempt=attempt,
            verdict=result.verdict,
            failure_cause=result.failure_cause,
            confidence=result.confidence,
            reflect_model=result.reflect_model,
            reflect_latency_ms=result.reflect_latency_ms,
            scout_viability=scout_viability,
            execution_mode=execution_mode,
            payload_json=json.dumps(payload, ensure_ascii=False),
        )
        result.artifact_id = pm_id
    except Exception as e:
        logger.warning("Failed to persist post-mortem for session %s: %s", session_id, e)


async def reflect_on_session(
    session_id: str,
    emit=None,
    attempt: int = 1,
    tool_summary: dict | None = None,
    scout_report=None,
    extra_evidence: str = "",
    turn_user_msg_id: int | None = None,
) -> ReflectResult:
    """Analyze a completed session turn and decide if the task was fulfilled.

    Args:
        session_id: Session to verify
        emit: Optional callback for SSE events
        attempt: Current attempt number (1 = first try, 2+ = retry)
        tool_summary: Aggregate tool execution stats from the agent loop
        scout_report: Optional ScoutReport from the turn (for deliverables_plan)
        extra_evidence: Optional supplementary text appended to the evidence
            blob (e.g. recall of past lessons, trial-hint proposals when stuck).
        turn_user_msg_id: Id of the user message that triggered this turn. When
            provided, reflect grades against THIS turn's request, not the latest
            user message in the DB (which may be a queued message for a future
            turn during rapid traffic).

    Returns:
        ReflectResult with verdict and optional lessons
    """
    user_request, evidence = _build_evidence(
        session_id,
        attempt=attempt,
        tool_summary=tool_summary,
        scout_report=scout_report,
        turn_user_msg_id=turn_user_msg_id,
    )
    if not user_request or not evidence:
        r = ReflectResult(verdict="pass", reasoning="No user request found to verify")
        _write_post_mortem(session_id, attempt, r, scout_report, tool_summary)
        return r

    if extra_evidence:
        evidence = f"{evidence}\n\n{extra_evidence}"

    if emit:
        emit({"type": "reflect.start"})

    model = settings.reflect_model or settings.background_model or settings.scout_model or settings.llm_model
    start = time.monotonic()

    # Inner retry budget for the reflect LLM call itself. A parse failure here
    # is a verifier-side problem; re-running scout + the agent loop (~5-10 min)
    # to recover from a malformed JSON response is a large mismatch in cost,
    # so we retry the reflect call locally before falling back to a soft pass.
    INNER_REPROMPT_LIMIT = 1
    raw = ""
    last_parse_err: Exception | None = None

    try:
        from core.llm.client import get_llm_client

        client = get_llm_client()

        for inner_attempt in range(INNER_REPROMPT_LIMIT + 1):
            messages = [
                {"role": "system", "content": REFLECT_PROMPT},
                {"role": "user", "content": evidence},
            ]
            if inner_attempt > 0:
                # Stricter reprompt: forbid prose, demand single-object JSON only,
                # and pass the prior raw output back so the model can self-correct.
                messages.append({"role": "assistant", "content": raw[:1500]})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Your previous response could not be parsed as JSON. "
                            "Respond with ONE valid JSON object that matches the schema "
                            "exactly. No markdown fences, no <think> tags, no prose "
                            "outside the JSON. Required keys: verdict, reasoning, "
                            "failure_cause, confidence, deliverables. /no_think"
                        ),
                    }
                )

            response = await client.chat(
                messages=messages,
                model=model,
                max_tokens=2048,
            )

            latency_ms = int((time.monotonic() - start) * 1000)
            raw = response.content.strip()

            # Strip markdown fences if present
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

            try:
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                last_parse_err = e
                # Try the structural repair before re-prompting the model.
                repaired = _try_repair_json(raw)
                if repaired:
                    data = repaired
                    logger.info("Reflect JSON repaired for session %s (inner_attempt=%d)", session_id, inner_attempt)
                else:
                    if inner_attempt < INNER_REPROMPT_LIMIT:
                        logger.warning(
                            "Reflect JSON parse failed for session %s (inner_attempt=%d): %s — reprompting",
                            session_id,
                            inner_attempt,
                            e,
                        )
                        continue
                    # Out of inner retries — fall through to soft-pass below.
                    raise

            result = _result_from_data(data, model, latency_ms)

            logger.info(
                "Reflect verdict=%s for session %s (%dms, inner=%d): %s",
                result.verdict,
                session_id,
                latency_ms,
                inner_attempt,
                result.reasoning,
            )

            if emit:
                reasoning = (
                    f"(repaired) {result.reasoning}" if inner_attempt > 0 or last_parse_err else result.reasoning
                )
                emit(
                    {
                        "type": "reflect.done",
                        "verdict": result.verdict,
                        "reasoning": reasoning,
                        "latency_ms": latency_ms,
                        "reflect_model": model,
                    }
                )

            extra_payload = None
            if _has_pass_with_lessons(result):
                # The model picked verdict=pass but populated retry-shaped fields
                # (strategy/diagnostic/what_failed). Tag the post-mortem so snooze
                # can still extract a lesson without us flipping the verdict, and
                # so metrics can surface this self-inconsistent case.
                extra_payload = {"pass_with_lessons": True}
            _write_post_mortem(session_id, attempt, result, scout_report, tool_summary, extra_payload=extra_payload)
            return result

        # Loop fell through without returning — defensive only; should be unreachable.
        raise RuntimeError("reflect inner loop exited without verdict")

    except json.JSONDecodeError as e:
        # All inner retries (including repair) exhausted. A parse failure on the
        # verifier side must NOT trigger a full turn retry — that's a 5-10 min
        # re-execution of an already-finished turn. Soft-pass with a clear
        # reasoning string and low confidence so dashboards/eval can flag it.
        latency_ms = int((time.monotonic() - start) * 1000)
        logger.warning(
            "Reflect parse exhausted for session %s after %d inner attempt(s): %s — soft-passing",
            session_id,
            INNER_REPROMPT_LIMIT + 1,
            e,
        )
        r = ReflectResult(
            verdict="pass",
            reasoning=f"Reflect parse failed (soft-pass): {e}",
            reflect_model=model,
            reflect_latency_ms=latency_ms,
            failure_cause="none",
            confidence=0.0,
        )
        if emit:
            emit(
                {
                    "type": "reflect.done",
                    "verdict": r.verdict,
                    "reasoning": r.reasoning,
                    "latency_ms": latency_ms,
                    "parse_error": True,
                    "reflect_model": model,
                }
            )
        _write_post_mortem(
            session_id,
            attempt,
            r,
            scout_report,
            tool_summary,
            extra_payload={"parse_error": True, "raw_response_excerpt": (raw or "")[:500]},
        )
        return r

    except Exception as e:
        logger.warning("Reflect failed for session %s: %s", session_id, e)
        r = ReflectResult(verdict="pass", reasoning=f"Reflect error: {e}")
        _write_post_mortem(session_id, attempt, r, scout_report, tool_summary)
        return r
