"""Pernix — Reflect: post-execution verification agent.

Runs after each agent turn to verify the user's request was fulfilled.
If not, produces actionable lessons and triggers a retry.

Lifecycle: Scout → Agent → Post-hooks → Reflect → (retry if needed)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from config import settings
from db import models as db

logger = logging.getLogger("pernix.reflect")

REFLECT_PROMPT = """You are a Reflect Agent. You review completed agent sessions to verify the user's request was fulfilled.

Given: the user's original message, the full transcript of the current attempt (from scout's start to the agent's final response — every tool call, tool result, and assistant message in order), workspace files created, scout's deliverables plan (if any), and a tool execution summary with per-tool call counts, failures, and the last few error messages.

Output a JSON object — emit fields in THIS order so the verdict is committed before any narrative could bias it:
- verdict: "pass" | "retry" | "escalate"
  - pass: task was completed satisfactorily
  - retry: task was NOT completed, and another attempt could succeed
  - escalate: task cannot be completed without user input
- reasoning: 1-2 sentences explaining your verdict
- failure_cause: One of "none" | "scout" | "agent" | "skill" | "task" | "env". Use "none" if verdict is pass. Otherwise attribute the failure: "scout" (plan was wrong/incomplete), "agent" (plan was fine but execution was poor), "skill" (a recommended skill was broken/outdated), "task" (user request was ambiguous or impossible), "env" (network, permissions, rate limit, missing external resource).
- confidence: float 0.0-1.0 — how confident you are in this verdict and failure_cause. Use <0.5 when evidence is ambiguous.
- deliverables: Array of {description, status: "met"|"partial"|"unmet"|"unknown", evidence_ref: string}. If scout provided a deliverables_plan, grade each item. If not, synthesize from the user's ask. evidence_ref should be a file path, task id, worker summary path, or a short quote pointing to what proves the status. Grading a plan item "unmet" does NOT by itself justify a non-pass verdict: a plan item the USER's own request does not require is the planner's elaboration (see the plan-literalism rules below) — an inline answer that satisfies the user's actual ask passes even when a plan-invented artifact was never produced.
- diagnostic: If retry — root cause analysis. Name the specific failure pattern. Empty string if pass.
- what_worked: If retry — tools/approaches that produced useful results (carry forward). Empty string if pass.
- what_failed: If retry — tools/approaches that failed or wasted time (avoid). Empty string if pass.
- strategy: If retry — concrete instruction for the retry attempt. Must propose a DIFFERENT approach if the same tools failed repeatedly. Empty string if pass.
- retry_without_tools: OPTIONAL, only meaningful with verdict "retry" — array of tool names (e.g. ["spawn_worker"]) that the agent misused this attempt and must NOT be allowed to call on the retry. Use when the failure was caused by reaching for a tool against the plan (e.g. delegating to workers when told to work inline). The harness enforces this mechanically — the listed tools are disabled for the retry attempt — so name a tool only when its absence would force the correct approach. Omit or empty array otherwise.
- cited_policies: OPTIONAL — when the evidence includes ACTIVE ADAPTIVE POLICIES, array of the [id] values (max 5) whose guidance demonstrably shaped this turn or was demonstrably violated in a way that caused the outcome. Omit (or empty array) when no policy visibly mattered — that is the honest default and the common case; never cite a policy just because it exists.
- missing: If escalate — what specific information or clarification is needed from the user. Empty string otherwise.
- turn_digest: REQUIRED when verdict is "retry" or "escalate"; optional on "pass" (you may omit the key entirely). When emitted, structure exactly as:
    {
      "scout_plan_summary": "1-2 sentences capturing what scout planned",
      "tool_calls": [
        {"tool": "<name>", "args": "<short args summary>", "outcome": "success"|"error"|"partial", "result_excerpt": "<verbatim text from the tool result, NOT paraphrased — max 2000 chars per call>"}
      ],
      "agent_final_response": "<verbatim final assistant message, may be truncated to ~1500 chars>",
      "key_findings": ["bullet 1", "bullet 2"],
      "what_was_tried": "1-3 sentences on the approach taken"
    }
  Include only tool calls that produced evidence informing your verdict — skip pure bookkeeping calls (memory writes, task admin, switch_*, approve_*). On retry, the next scout will read this digest as the carry-forward record of what was tried. result_excerpt MUST be verbatim text from the actual tool result so the next scout sees real evidence, not your interpretation. If a result was very long, take the most relevant ~2000 chars and note "[+N chars truncated]" at the end. Note: some large results appear as a head/tail stub with a "[full result bound as `tool_result_N` in the session kernel …]" pointer — the stub IS the verbatim record in that case; quote from it and mention the binding.
- experience: REQUIRED on every verdict, pass included — the intangible record of how this interaction went for the USER, which no other field captures. Structure exactly as:
    {
      "user_sentiment": "satisfied" | "neutral" | "frustrated" | "unknown",
      "clarification_loop": true|false,
      "first_response_sufficient": true|false,
      "friction": ["short_snake_case_labels"],
      "user_observations": ["self-contained fact about the user evident from THIS turn"],
      "note": "1-2 sentences explaining the experience"
    }
  Field semantics:
  - user_sentiment: how the user visibly reacted (their words, tone shifts, corrections) — "unknown" when they haven't reacted yet. Never infer satisfaction from task success alone.
  - clarification_loop: true if the user had to rephrase, repeat themselves, or correct the agent's understanding of what they wanted.
  - first_response_sufficient: true if the first substantive response would have satisfied the request without further prodding from the user.
  - friction: what degraded the experience, as reusable labels — e.g. "misread_intent", "over_delivered", "under_delivered", "tone_mismatch", "slow_progress", "tool_noise", "excessive_questions", "ignored_constraint". Empty array when the interaction was smooth. Coin a new label when none fits.
  - user_observations: max 3 durable facts about the user this turn actually revealed — a stated preference, an expertise signal, a communication-style pattern, a correction they made. Each must be self-contained (readable without the transcript). Empty array when the turn revealed nothing new about them.
  - note: why the interaction went well or poorly FOR THE USER and what would have made it better. This is about the interaction, not task completion — do not restate the verdict or diagnostic.
  Ground every field in transcript evidence: the user's actual words and reactions, not your judgment of the work's quality.

RULES:
- MATERIALITY BAR FOR NON-PASS VERDICTS: A retry is warranted only when a concrete user-facing deliverable is missing, incomplete, or factually false — or when a success claim (file written, job scheduled, memory saved, command executed) is unsupported by verifiable evidence. Plan-literalism (deviating from the scout plan when the outcome was delivered), tone/length mismatches, and defensible judgment calls are PASS — record them in the lessons fields and in `experience`, never as a retry. A non-pass verdict MUST name a concrete, checkable failure_cause; if you cannot name one, the verdict is "pass".
- COMPLETED WORK IS NEVER RETRIED FOR PROCESS VIOLATIONS: when everything the user's request requires is verifiably done, violations of efficiency or procedure rules — call/budget caps exceeded, forbidden re-reads, redundant tool calls, a stray formatting artifact in otherwise-correct output — are a PASS with the violation recorded in what_failed and experience. A retry re-executes finished work, and the retry attempt has nothing left to do (field case: an attempt-2 with zero tool calls was then failed for "not verifying" prior work its charter forbade it from re-reading — an unwinnable trap the first verdict created).
- ESCALATE GRADES THE TURN, NOT THE SITUATION: escalate means THIS turn's deliverable cannot exist without user input. If the turn's work is complete, the verdict is pass even when a question for the user remains (a stalled loop, a pending decision, a follow-up worth raising) — note the question in experience.note; the agent has its own channels for surfacing questions. Field case: a cron run the verdict itself described as "delivered cleanly" was escalated to flag a stale thread — a failure statistic for a turn that never failed.
- YOUR OWN BLINDNESS IS NOT THE AGENT'S FAILURE: when you cannot verify a claim because the evidence is invisible TO YOU — a tool result body elided from the evidence blob, image bytes you cannot render, output truncation, a session that ran in a sandboxed or temporary workspace — grade what you CAN see and lower confidence below 0.5 with a note in reasoning. Never issue retry or escalate whose only nameable failure is that the verifier could not see the evidence.
- Be strict: if the user asked for a file/deliverable and none was created, that's a retry.
- Be strict: if the USER named a specific tool or approach and the agent used something else, that's a retry. This covers the user's own instruction only — scout's plan is the agent's planning artifact, not a contract with the user, so departing from SCOUT DELIVERABLES PLAN / PLANNED APPROACH while still delivering the outcome is a pass.
- Be fair: if the agent produced a reasonable answer even without creating files, that can be a pass.
- Be concise: the retry strategy must be actionable, not vague.
- Verdict "pass" should be the default for conversational exchanges, questions answered, simple requests fulfilled.
- Do NOT retry for partial success — only for clear failures to meet the user's core request.
- TURN SCOPE OVERRIDES THE PLAN: If the evidence includes a TURN SCOPE block (e.g. "ask_user answer"), that block defines the deliverable for this turn. It supersedes the SCOUT DELIVERABLES PLAN, PLANNED APPROACH, and any session-wide goal. An ask_user answer turn passes when the agent honors the answer (applies the approved change, declines cleanly, uses the value provided) — even if scout's plan listed wider work, that work is out of scope for this verdict.
- TRUST THE PLAN: If the evidence includes ACTIVE SKILL / PLANNED APPROACH / TOOL RATIONALE, treat those as the contract the agent was given. If the agent followed the planned approach using the planned tools, do NOT call hallucination just because the tools look "generic" (e.g. browse_web). Skills routinely mandate generic tools — that's expected, not a failure. Only flag hallucination when the agent invented data with no supporting tool calls AND the plan called for a tool that wasn't run.
- Use the TOOL EXECUTION SUMMARY to identify failure patterns. If a tool failed 2+ times with the same error, the retry strategy MUST suggest a different tool or approach.
- SELF-REPORTED COUNTS ARE NOT A RUBRIC INPUT: the agent cannot see the TOOL EXECUTION SUMMARY and, on a retry, cannot see prior attempts' totals — so a mismatch between the agent's own stated tool count and the summary is NOT, by itself, dishonesty or a retry cause. Grade what the agent DID against the summary and transcript; ignore its arithmetic about itself. (Actual misuse of a forbidden tool remains gradeable — from the summary, not from the agent's count.)
- TOOL CALL FACTS ARE NOT NEGOTIABLE. The TOOL EXECUTION SUMMARY is observed truth, not interpretation. Before claiming the agent did NOT use a tool, verify: if the tool name appears in the summary with calls > 0, the agent DID call it. Do NOT write "agent did not call X" or "agent failed to use X" or "X was skipped" if X.calls > 0 in the summary. If you believe the call was *ineffective* (tool ran but didn't produce the expected result), say that — but do not deny the call happened. Hallucinating absence of a call that the summary records as present is a verifier-side correctness failure.
- REFUSALS ARE NOT FAILURES: "policy refusal(s)" and REFUSED lines in the summary are the harness declining a call (job allow-list, retry exclusion, disabled tool, approval gate). They say nothing about the tool's reliability and must never be written up as "the tool failed" or as an env problem. They DO show the agent called a tool it was told not to use: grade that as a rule breach only when the user or the job charter forbade it; otherwise note it in what_failed and move on.
- A REQUIREMENT ATTRIBUTED TO THE USER MUST QUOTE THE USER: before a non-pass verdict says "the user asked for X" or "the user explicitly called out X", find X in USER REQUEST (or a TURN SCOPE block) and quote it in reasoning. A requirement that appears only in SCOUT DELIVERABLES PLAN, PLANNED APPROACH or TOOL RATIONALE is the planner's, not the user's — by the plan-literalism rule above it cannot justify retry or escalate. Field case: a deferred escalate on a correct reply cited two memory entries "the user explicitly called out" that appeared only in the scout plan.
- EVIDENCE PRIMACY. The transcript's tool RESULTS are ground truth. When a tool result body in the transcript supports or contradicts a claim in the agent's final response, that body outranks your priors about the topic. If the agent fetched URL X and the transcript shows the fetch returned real content, do NOT call hallucination just because your training data doesn't recognize X. Verify against what the tools actually returned, not what you "know" about the topic. The verifier-side failure mode this prevents: dismissing a fact as fabricated when the session contains real evidence for it.
- GROUNDING CHECK IS A FLAG, NOT A VERDICT: when the evidence ends with a GROUNDING CHECK section, it lists (a) identifiers the final response cites that appear in NO tool result this attempt and not in the user's message, and (b) markdown table rows that pair an identifier with a name or id that no SINGLE tool result shows together. A factual table or id→content mapping whose rows are flagged under (b) is "factually false" under the materiality bar — verdict retry, failure_cause agent, and quote the flagged rows verbatim in what_failed — UNLESS the response itself labels those cells as inferred / not retrieved. One incidental token under (a) (an example, a typo, a name the agent coined) is NOT a retry; name it in what_failed. Never grade a reconstructed mapping above confidence 0.6 while any of its rows is flagged.
- TOOL EXHAUSTION: If the summary shows a high number of calls across many different tools, the agent may have run out of tool rounds rather than used the wrong approach. Prefer "pass" with partial results if real progress was made — UNLESS the user named a specific deliverable (file, report, message sent) and that deliverable does not exist. The deliverable-missing rule above ALWAYS overrides this exhaustion clause; "we made progress" is not a substitute for "we produced what was asked for."
- CEILING LOOP → ESCALATE: If TERMINATION HISTORY shows the same blocking reason (e.g. round_ceiling, budget_exhausted, compaction_failed) on the current turn AND at least one prior turn, the agent is hitting the same hard wall — another retry will hit it again. Verdict MUST be 'escalate'. In `missing`, name the wall: e.g. 'round_ceiling on consecutive attempts; agent cannot finish within max_tool_rounds — split the task or raise the limit.'
- THRASHING → ESCALATE: When a CURRENT ATTEMPT TOOL CALLS section is present (retry attempts), judge thrashing from THAT section only — the cumulative summary includes prior attempts' tools, and a focused retry must not inherit a scattered first attempt's diversity. If the (scoped) summary shows ≥4 distinct tools used with no forward progress (empty outputs repeated, same files re-read, error count growing, or the agent drifting across unrelated checks), the agent is thrashing, not pursuing a coherent-but-wrong strategy. Prefer "escalate" with a clear "missing" field over "retry" — another round of the same thrashing will not help. A single failing tool being tried with sibling tools is NOT thrashing; reserve "escalate" for cases where the agent lost the thread.
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

    # Turn digest — structured record of what happened during the attempt.
    # Populated when verdict in {"retry","escalate"} so the next scout can
    # plan around what was tried; optional on "pass" (controlled by
    # settings.reflect_emit_digest_on_pass). Empty dict when absent.
    # Schema documented in REFLECT_PROMPT.
    turn_digest: dict = field(default_factory=dict)

    # Mechanical lesson effector (audit P1f): tools reflect wants disabled on
    # the retry attempt. Advisory-prose lessons demonstrably don't stop the
    # agent from repeating a tool misuse (field case 2072ab68cfd4: ten retries,
    # each spawning workers against explicit instructions); this is enforced by
    # the executor and the active-tool filter instead.
    retry_without_tools: list = field(default_factory=list)

    # Adaptive entries (by id) whose guidance demonstrably shaped or blocked
    # this turn — the per-entry usage signal's reflect-side source. Empty is
    # the honest default.
    cited_policies: list = field(default_factory=list)

    # Experience read — the intangibles of the interaction (sentiment,
    # friction, per-turn user observations). Emitted on EVERY verdict: pass
    # turns are exactly where no other signal captures how the exchange went.
    # Schema documented in REFLECT_PROMPT; sanitized by _sanitize_experience.
    experience: dict = field(default_factory=dict)

    # Mechanical grounding flags (advisory): identifiers the final response
    # cites that no tool result this attempt contains, and table rows pairing
    # identifiers no single tool result shows together. Computed in Python
    # from the transcript, never by the model — see _grounding_check. Field
    # case dce9a6de7f81: a 5-row id→policy table with every cell real and
    # every pairing invented passed at 0.95 because nothing checked pairings.
    grounding: dict = field(default_factory=dict)


# Tools whose successful execution has one-shot, externally visible effects —
# re-running them on a reflect retry duplicates the action (a second push
# notification, a duplicate cron job, an extra worker fleet). Read tools,
# searches, and file ops are excluded: re-running those is wasteful but safe.
SIDE_EFFECT_TOOLS: frozenset[str] = frozenset(
    {
        "notify_user",
        "schedule_job",
        "update_scheduled_job",
        "remove_scheduled_job",
        "spawn_worker",
        "notify_parent",
        "message_worker",
    }
)


def build_retry_context(
    result: ReflectResult, attempt: int, max_attempts: int, tool_summary: dict | None = None
) -> str:
    """Build the retry-context string injected into the next scout invocation.

    On retry, scout-N reads this block to plan around what the prior attempt
    tried. Includes:

    - Reflect's verdict reasoning, diagnostic, what_worked/what_failed, strategy.
    - The prior attempt's turn_digest (when reflect emitted one) — gives scout
      the structured record of tool calls, args, outcomes, and verbatim
      result excerpts so it doesn't have to interpret a free-form summary.
    - A list of tools already used.
    - A context-carryover note so the agent doesn't re-read files / re-run
      searches that are already in conversation history.
    """
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
    flagged = (result.grounding or {}).get("ungrounded") or []
    rows = (result.grounding or {}).get("unverified_rows") or []
    if flagged or rows:
        parts.append(
            "GROUNDING FLAGS FROM PRIOR ATTEMPT (mechanical): "
            + (f"identifiers no tool result contained: {', '.join(flagged[:12])}. " if flagged else "")
            + (f"table rows no single tool result supported: {'; '.join(rows[:8])}. " if rows else "")
            + "This attempt must take each of these from a tool result verbatim, or state "
            "'not retrieved — would need <call>' instead of reconstructing it."
        )

    # Prior turn_digest — the structured record reflect built from the previous
    # attempt's transcript. Scout reads this to know what was actually tried
    # and what the tools returned, instead of grading reflect's free-form
    # summary against its own priors.
    if result.turn_digest:
        parts.append("PRIOR ATTEMPT DIGEST (from reflect):")
        parts.append(_format_digest_for_scout(result.turn_digest))

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

        # HARD guard for one-shot externally-visible actions. Reflect is
        # deliberately biased toward retry when a side effect can't be
        # verified — without this, the retry attempt re-sends the
        # notification / re-schedules the job / spawns more workers,
        # double-firing the very action that already succeeded.
        fired = sorted(
            name
            for name, stats in tool_summary.items()
            if name in SIDE_EFFECT_TOOLS and stats.get("calls", 0) > stats.get("failures", 0)
        )
        if fired:
            parts.append(
                "ALREADY EXECUTED — DO NOT REPEAT: the prior attempt successfully ran "
                f"{', '.join(fired)}. These have observable external effects (messages sent, "
                "jobs scheduled, workers spawned). Repeating them would duplicate the action. "
                "Treat them as DONE; this retry is only for the parts that did not complete."
            )

    return "\n".join(parts)


def _format_digest_for_scout(digest: dict) -> str:
    """Render a turn_digest dict as a readable block for scout's input.

    Kept compact — scout already has its own context budget. We pass through
    the structured fields; if a tool_call result_excerpt is long, it will have
    been trimmed by ``_sanitize_turn_digest`` already.
    """
    lines: list[str] = []
    plan = digest.get("scout_plan_summary", "")
    if plan:
        lines.append(f"  Prior scout plan: {plan}")
    tried = digest.get("what_was_tried", "")
    if tried:
        lines.append(f"  What was tried: {tried}")
    findings = digest.get("key_findings") or []
    if findings:
        lines.append("  Key findings:")
        for f in findings:
            lines.append(f"    - {f}")
    calls = digest.get("tool_calls") or []
    if calls:
        lines.append("  Prior tool calls (verbatim excerpts):")
        for i, tc in enumerate(calls, 1):
            lines.append(
                f"    [{i}] {tc.get('tool', '?')} "
                f"args={tc.get('args', '')[:200]} → outcome={tc.get('outcome', '?')}"
            )
            excerpt = tc.get("result_excerpt", "") or ""
            if excerpt:
                # Indent the excerpt so it's clearly a sub-block.
                indented = "\n".join(f"        {ln}" for ln in excerpt.splitlines())
                lines.append(indented)
    final = digest.get("agent_final_response", "")
    if final:
        lines.append(f"  Prior agent final response: {final[:500]}")
    return "\n".join(lines) if lines else "  (digest empty)"


def _format_message(msg: dict, tool_result_char_cap: int | None = None) -> str:
    """Format a single message as a readable transcript line.

    When ``tool_result_char_cap`` is set, tool result bodies longer than the
    cap are truncated with a ``[+N chars truncated]`` marker. Reflect's
    transcript-mode default uses a generous cap so result bodies are still
    legible (a 5000-char window typically covers the meaningful header of a
    fetched page or the relevant section of a long file), but a single
    multi-megabyte result can't drown the budget.
    """
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
        if tool_result_char_cap is not None and len(content) > tool_result_char_cap:
            truncated = len(content) - tool_result_char_cap
            return f"[TOOL RESULT]\n{content[:tool_result_char_cap]}\n[+{truncated} chars truncated]"
        return f"[TOOL RESULT]\n{content}"
    elif role == "system":
        return f"[SYSTEM]\n{content}"
    elif role == "scout":
        return f"[SCOUT]\n{content}"
    elif role == "reflect":
        return f"[REFLECT]\n{content}"
    else:
        return f"[{role.upper()}]\n{content}"


# Default reflect context budget (tokens). If conversation exceeds this,
# older messages are summarized to fit.
_REFLECT_CONTEXT_BUDGET = 100_000

# Per-tool-result body cap when formatting the attempt transcript. Bigger than
# the legacy 300-char preview so reflect can actually verify claims against
# what the tool returned (e.g., a fetched page body), but bounded so a single
# 200kb result can't dominate the budget.
_PER_TOOL_RESULT_CHAR_CAP = 5000


_GROUNDING_ID_PATTERNS = (
    re.compile(r"#\d{2,}"),  # proposal / issue / message numbers
    re.compile(r"\bab-[0-9a-f]{12}\b"),  # adaptive batch ids
    re.compile(r"(?<![\w-])[0-9a-f]{12}(?![\w-])"),  # session / hypothesis ids (not a batch id's tail)
)
_GROUNDING_SLUG_PATTERN = re.compile(r"`([^`\n]{6,80})`")  # backticked names
_GROUNDING_MAX_LISTED = 12


def _grounding_tokens(text: str) -> tuple[list[str], list[str]]:
    """(id-like tokens, slug-like tokens) cited in `text`, first-seen order, deduped."""
    ids: list[str] = []
    slugs: list[str] = []
    seen: set[str] = set()
    for pat in _GROUNDING_ID_PATTERNS:
        for m in pat.finditer(text):
            tok = m.group(0)
            if tok not in seen:
                seen.add(tok)
                ids.append(tok)
    for m in _GROUNDING_SLUG_PATTERN.finditer(text):
        tok = m.group(1).strip()
        # Names, paths, slugs — not code snippets or prose in backticks.
        if " " in tok or not re.search(r"[-_./]", tok) or tok in seen:
            continue
        seen.add(tok)
        slugs.append(tok)
    return ids, slugs


def _token_positions(tok: str, haystack: str) -> list[int]:
    if tok.startswith("#"):
        return [m.start() for m in re.finditer(rf"(?<![\w.]){re.escape(tok[1:])}(?![\w.])", haystack)]
    return [m.start() for m in re.finditer(re.escape(tok), haystack)]


def _token_in(tok: str, haystack: str) -> bool:
    return bool(_token_positions(tok, haystack))


# How close two identifiers must sit inside ONE tool result to count as
# "shown together". A proposal row in the API JSON spans a few hundred chars;
# a 16K-char file dump that happens to contain "189" in one paragraph and a
# policy name in another is not evidence they belong in the same table row
# (on the live replay that coincidence hid one of five invented rows).
_GROUNDING_PAIR_WINDOW = 1500


def _pair_in(a: str, b: str, haystack: str) -> bool:
    pa = _token_positions(a, haystack)
    if not pa:
        return False
    pb = _token_positions(b, haystack)
    return any(abs(x - y) <= _GROUNDING_PAIR_WINDOW for x in pa for y in pb)


def _grounding_check(final_assistant: str, attempt_msgs: list[dict], user_request: str) -> dict:
    """Mechanical grounding flags for the final response. Advisory by design.

    Two checks, both against what this attempt's tools actually returned
    (plus the user's own message, which is a legitimate source):

    * ``ungrounded`` — identifiers the response cites that appear in no
      source at all (an invented batch id, a file that was never listed).
    * ``unverified_rows`` — markdown table rows pairing an identifier with a
      name/other identifier that no SINGLE source contains together (within
      ``_GROUNDING_PAIR_WINDOW`` chars of each other). This
      is the shape of the live failure: every token real, every pairing
      reconstructed (#189 ↔ `hard-stop-after-write`, when #189 was a memory
      correction the agent never fetched).

    Returns {"checked": n, "ungrounded": [...], "unverified_rows": [...]}.
    The rubric decides what the flags mean; a model cannot be overruled by
    a regex, only informed by it.
    """
    if not final_assistant:
        return {"checked": 0, "ungrounded": [], "unverified_rows": []}
    # Tool results and the user's own message are evidence. The scout's plan
    # is not — it routinely repeats the ids it was asked about ("enumerate
    # #189-#194"), which would ground exactly the rows the check exists for.
    sources: list[str] = [user_request or ""]
    for m in attempt_msgs:
        if m.get("role") == "tool":
            sources.append(m.get("content") or "")
    everything = "\n".join(sources)

    ids, slugs = _grounding_tokens(final_assistant)
    ungrounded = [t for t in ids + slugs if not _token_in(t, everything)]

    unverified_rows: list[str] = []
    for line in final_assistant.splitlines():
        if not line.lstrip().startswith("|") or line.count("|") < 3:
            continue
        if re.fullmatch(r"[\s|:\-]+", line):
            continue  # header separator
        row_ids, row_slugs = _grounding_tokens(line)
        tokens = row_ids + row_slugs
        if len(tokens) < 2:
            continue
        # The row's claim is its first pairing: the id and the name beside
        # it. Secondary tokens in a description cell (`file_write`, a path)
        # co-occur with ids everywhere and must not vouch for the row.
        anchor, partner = tokens[0], tokens[1]
        if any(_pair_in(anchor, partner, src) for src in sources):
            continue
        unverified_rows.append(f"{anchor} ↔ {partner}")
    return {
        "checked": len(ids) + len(slugs),
        "ungrounded": ungrounded[:_GROUNDING_MAX_LISTED],
        "unverified_rows": unverified_rows[:_GROUNDING_MAX_LISTED],
    }


def _format_grounding(g: dict) -> str:
    checked = g.get("checked", 0)
    if not checked:
        return ""
    lines = ["GROUNDING CHECK (mechanical, advisory — a flag, not a verdict):"]
    ung = g.get("ungrounded") or []
    rows = g.get("unverified_rows") or []
    if ung:
        lines.append(
            f"- {len(ung)} of {checked} identifiers in the final response appear in NO tool result "
            f"and not in the user's message: {', '.join(ung)}"
        )
    if rows:
        lines.append(
            f"- {len(rows)} table row(s) pair identifiers that no single tool result shows together: " + "; ".join(rows)
        )
    if not ung and not rows:
        lines.append(
            f"- all {checked} cited identifiers appear in a tool result or the user's message; no table row flagged"
        )
    return "\n".join(lines)


def _effective_workspace(session_id: str) -> tuple[str, bool]:
    """(workspace_root, is_override) for a session. workspace_override is an
    in-memory AgentSession field (job test-runs, canary sandboxes) — never a
    DB column — so it must come from the live session object; a missing or
    ended session falls back to the shared workspace."""
    try:
        from sessions.manager import get_manager

        live = get_manager().get(session_id)
        override = getattr(live, "workspace_override", None) if live else None
        if override:
            return str(override), True
    except Exception:
        pass
    return settings.workspace_dir, False


def _messages_since_attempt_start(messages: list[dict], turn_user_msg_id: int | None = None) -> list[dict]:
    """Slice messages to the current attempt's transcript.

    Walks the message list end→start to find the most recent ``scout`` role
    marker (written when scout finishes for an attempt — see
    ``sessions/manager.py``). Returns everything from that marker forward.

    Fallbacks, in order:
    - No scout marker found → slice from ``turn_user_msg_id`` forward.
    - Neither marker available → return all messages unchanged.

    The scout marker is the natural per-attempt boundary: on a retry, scout-N
    runs first and writes a new scout message, so the slice from "latest scout"
    captures exactly what happened during attempt N.
    """
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "scout":
            return messages[i:]

    if turn_user_msg_id is not None:
        for i, msg in enumerate(messages):
            if msg.get("id") == turn_user_msg_id and msg.get("role") == "user":
                return messages[i:]

    return list(messages)


def _build_attempt_transcript_section(
    messages: list[dict],
    budget_tokens: int,
    turn_user_msg_id: int | None = None,
) -> str:
    """Format the current attempt's transcript with verbatim tool results.

    Slices via `_messages_since_attempt_start` so we never include messages
    from earlier attempts (those are carried forward to scout via the prior
    turn_digest, not re-included here).

    Budget handling: try at progressively tighter per-tool-result caps; if
    still over, drop oldest non-final messages with a one-line elision marker.
    """
    from core.context.tokens import get_estimator

    estimator = get_estimator()
    attempt_msgs = _messages_since_attempt_start(messages, turn_user_msg_id)
    drop_roles = ("notice", "eval", "model_divider", "compaction")
    substantive = [m for m in attempt_msgs if m.get("role") not in drop_roles]

    if not substantive:
        return ""

    for cap in (_PER_TOOL_RESULT_CHAR_CAP, 1500, 500):
        formatted = [_format_message(m, tool_result_char_cap=cap) for m in substantive]
        transcript = "\n\n---\n\n".join(formatted)
        if estimator.count(transcript) <= budget_tokens:
            return transcript

    # Still over budget — keep most recent verbatim (cap=500), elide older.
    kept: list[str] = []
    used = 0
    for msg in reversed(substantive):
        formatted_msg = _format_message(msg, tool_result_char_cap=500)
        msg_tokens = estimator.count(formatted_msg)
        if used + msg_tokens > budget_tokens - 200:  # leave room for marker
            break
        kept.insert(0, formatted_msg)
        used += msg_tokens

    dropped = len(substantive) - len(kept)
    if dropped > 0:
        marker = f"[earlier {dropped} message(s) of this attempt elided to fit budget]"
        return marker + "\n\n---\n\n" + "\n\n---\n\n".join(kept)
    return "\n\n---\n\n".join(kept)


def _build_compact_evidence(
    session_id: str,
    user_request: str,
    messages: list[dict],
    attempt: int,
    tool_summary: dict | None,
    scout_report=None,
    tool_summary_attempts: list | None = None,
    termination_reason: str | None = None,
    prior_termination_reasons: list[str] | None = None,
    turn_user_msg_id: int | None = None,
    grounding_out: dict | None = None,
) -> str:
    """Build the evidence blob for reflect.

    Includes the current attempt's transcript (from the most recent scout-role
    marker forward — earlier attempts are not re-included), workspace files,
    scout's deliverables_plan and approach (if any), tool execution summary,
    and the user's request paired with the agent's final
    response. Tool result bodies are kept verbatim up to ``_PER_TOOL_RESULT_CHAR_CAP``
    chars so reflect can verify claims against what tools actually returned.
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

    # Turn-scope marker — when the user's message is an ask_user answer, the
    # deliverable for THIS turn is to honor that answer. Scout's plan and any
    # broader session goals are out of scope. Without this, reflect grades
    # the narrow approval turn against the session's wider narrative and
    # falsely flags the agent for "not producing the deliverable" the user
    # never asked for on this turn.
    if user_request.startswith("[User answered your question]"):
        parts.append(
            "TURN SCOPE: ask_user answer.\n"
            "The user's message is a reply to an `ask_user` prompt the agent issued. "
            "The deliverable for THIS turn is to honor that answer "
            "(apply the change they approved, decline cleanly if they refused, "
            "or use the value they provided). "
            "Broader session goals and any SCOUT DELIVERABLES PLAN below are stale "
            "carry-overs from prior turns — do NOT grade against them on this turn. "
            "Verdict = pass iff the agent took the action implied by the user's answer."
        )

    # Workspace files (top 20 by mtime) — from the session's EFFECTIVE
    # workspace. A workspace_override session (job test-runs, canaries)
    # works in its own sandbox; listing the shared workspace here made
    # reflect grade against files the agent could never see (2026-08-27
    # audit: a job test-run was escalated for "ignoring" the shared
    # workspace's ~20 files while it correctly organized its temp mount).
    ws_root, ws_overridden = _effective_workspace(session_id)
    workspace = Path(ws_root)
    ws_files = []
    if workspace.exists():
        candidates = [
            f
            for f in workspace.rglob("*")
            if f.is_file() and not any(p.startswith(".") for p in f.relative_to(workspace).parts)
        ]
        candidates.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        ws_files = [str(f.relative_to(workspace)) for f in candidates[:20]]

    ws_label = (
        "WORKSPACE FILES (session-scoped sandbox — the shared workspace is out of scope for this session):"
        if ws_overridden
        else "WORKSPACE FILES:"
    )
    parts.append(ws_label + "\n" + ("\n".join(f"- {f}" for f in ws_files) if ws_files else "(none)"))

    # Termination history — lets reflect detect ceiling-loops (same hard wall
    # hit on multiple consecutive turns). When present, the prompt's CEILING
    # LOOP rule fires: same reason ≥2 times in a row → escalate.
    if termination_reason or prior_termination_reasons:
        history = list(prior_termination_reasons or [])
        if termination_reason and (not history or history[0] != termination_reason):
            history = [termination_reason] + history
        if history:
            parts.append("TERMINATION HISTORY (newest first): " + ", ".join(history))

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

    # Active adaptive policies, by id — so the grade's cited_policies field
    # has something real to cite. Ids and titles only: the full content is
    # in the agent's prompt, and reflect only needs to say WHICH policies
    # demonstrably shaped or blocked the outcome. This is the per-entry
    # usage signal's second source (scout's used_hints is the first).
    if settings.adaptive_enabled:
        try:
            from db import models as _db

            pol = [
                e
                for e in _db.adaptive_list_entries(kind="policy") + _db.adaptive_list_entries(kind="prompt_note")
                if e.get("scope") in ("global", f"session:{session_id}")
            ]
            if pol:
                parts.append(
                    "ACTIVE ADAPTIVE POLICIES (cite ids in cited_policies only when one "
                    "demonstrably shaped or blocked this outcome):\n"
                    + "\n".join(f"- [{e['id']}] {e['title']}" for e in pol[:30])
                )
        except Exception:
            pass  # evidence enrichment, never a reason to skip the grade

    # Tool execution summary with last few errors per tool. Labeled with its
    # scope: tool_summary accumulates across ALL attempts of the turn (see
    # TurnState.tool_summary) while the transcript below is current-attempt
    # only — without the label, reflect compares the agent's per-attempt
    # behavior against multi-attempt totals and calls the mismatch dishonesty
    # (field case 0ba19fdbc823: "claimed 8, actual 20" — the 20 included the
    # stripped first attempt's calls).
    if tool_summary:
        scope_note = (
            " (cumulative across ALL attempts of this turn — on a retry these counts "
            "include prior attempts' calls; the transcript below covers only the "
            "current attempt)"
            if attempt > 1
            else ""
        )
        summary_lines = [f"TOOL EXECUTION SUMMARY{scope_note}:"]
        for tool_name, stats in sorted(tool_summary.items()):
            refusals = int(stats.get("refusals") or 0)
            summary_lines.append(
                f"- {tool_name}: {stats['calls']} call(s), "
                f"{stats['failures']} failure(s), {stats['total_latency_ms']}ms total"
                + (f", {refusals} policy refusal(s) — not tool failures" if refusals else "")
            )
            for err in stats.get("errors", [])[:5]:
                summary_lines.append(f"    ERROR: {err}")
            for err in stats.get("refusal_errors", [])[:3]:
                summary_lines.append(f"    REFUSED: {err}")
        parts.append("\n".join(summary_lines))

    # Current attempt's own calls (C2): the scope-sensitive rules (THRASHING's
    # distinct-tool count, per-attempt behavior) grade against THIS section on
    # retries, never against the cumulative block above.
    if attempt > 1 and tool_summary_attempts and len(tool_summary_attempts) >= attempt:
        cur = tool_summary_attempts[attempt - 1] or {}
        cur_lines = [f"CURRENT ATTEMPT (#{attempt}) TOOL CALLS — this attempt only:"]
        if cur:
            for tool_name, stats in sorted(cur.items()):
                refusals = int(stats.get("refusals") or 0)
                cur_lines.append(
                    f"- {tool_name}: {stats['calls']} call(s), {stats['failures']} failure(s)"
                    + (f", {refusals} policy refusal(s)" if refusals else "")
                )
        else:
            cur_lines.append("(no tool calls this attempt)")
        parts.append("\n".join(cur_lines))

    # User's ask (echoed) so reflect anchors against the original goal even when
    # the transcript scrolls through tool calls.
    parts.append(f"USER REQUEST:\n{user_request}")

    # Per-attempt transcript with verbatim tool results — this is the change
    # that lets reflect verify claims against what tools actually returned,
    # rather than against its training-data priors. Scoped to the current
    # attempt so retries don't drown the verifier in stale messages.
    from core.context.tokens import get_estimator

    estimator = get_estimator()
    preamble_tokens = estimator.count("\n\n".join(parts))
    final_response_tokens = estimator.count(final_assistant or "")
    transcript_budget = max(_REFLECT_CONTEXT_BUDGET - preamble_tokens - final_response_tokens - 2000, 5000)
    transcript = _build_attempt_transcript_section(
        messages,
        budget_tokens=transcript_budget,
        turn_user_msg_id=turn_user_msg_id,
    )
    if transcript:
        parts.append("=" * 60)
        parts.append("ATTEMPT TRANSCRIPT (current attempt only — prior attempts elided)")
        parts.append("=" * 60)
        parts.append(transcript)

    parts.append(f"AGENT FINAL RESPONSE:\n{final_assistant or '(no final assistant message)'}")

    # Mechanical grounding flags over the same attempt slice the transcript
    # above was built from. Appended last so the model reads the response
    # first and the flags second, as a reviewer would.
    grounding = _grounding_check(
        final_assistant, _messages_since_attempt_start(messages, turn_user_msg_id), user_request
    )
    if grounding_out is not None:
        grounding_out.update(grounding)
    section = _format_grounding(grounding)
    if section:
        parts.append(section)

    return "\n\n".join(parts)


def _build_evidence(
    session_id: str,
    attempt: int = 1,
    tool_summary: dict | None = None,
    scout_report=None,
    turn_user_msg_id: int | None = None,
    termination_reason: str | None = None,
    prior_termination_reasons: list[str] | None = None,
    tool_summary_attempts: list | None = None,
    grounding_out: dict | None = None,
) -> tuple[str, str]:
    """Build evidence for reflect verification.

    Always emits per-attempt evidence: preamble (workspace files, termination
    history, scout plan, tool summary) + the current attempt's
    transcript with verbatim tool results + the agent's final response.
    Earlier attempts are NOT included — they are carried forward to scout via
    the prior turn_digest stored in post_mortems, not re-shown to reflect.

    The legacy ``reflect_full_transcript`` setting is now a no-op
    (deprecated): reflect always sees the per-attempt transcript.

    `turn_user_msg_id` is used as a fallback boundary when no scout-role
    marker exists in the messages (older sessions, scout-skipped paths).

    Returns (user_request, evidence_summary).
    """
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

    evidence = _build_compact_evidence(
        session_id,
        user_request,
        messages,
        attempt,
        tool_summary,
        scout_report,
        tool_summary_attempts=tool_summary_attempts,
        termination_reason=termination_reason,
        prior_termination_reasons=prior_termination_reasons,
        turn_user_msg_id=turn_user_msg_id,
        grounding_out=grounding_out,
    )
    return user_request, evidence


def _count_unmatched_braces(s: str) -> int:
    """Count net unclosed `{` braces, skipping those inside JSON strings."""
    depth = 0
    in_string = False
    escape_next = False
    for ch in s:
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
        elif not in_string:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
    return max(0, depth)


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
        # Ensure object is closed — count only braces outside strings to
        # avoid miscounting { or } that appear inside JSON string values.
        open_braces = _count_unmatched_braces(candidate)
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
    # A grade with NO verdict field at all is a malformed grade, not a pass.
    # Same precedent as the invalid-verdict coercion below: defaulting absent
    # data to "pass" makes a broken grader look like a confident approval
    # (ARC-3 sweep: contentless confidence-0.0 passes on substantial turns).
    _verdict = data.get("verdict")
    _verdict_coerced = False
    if not _verdict:
        logger.warning("Reflect grade missing verdict field — coercing to retry")
        _verdict = "retry"
        _verdict_coerced = True
        data.setdefault("reasoning", "(malformed grade: no verdict field — coerced to retry)")
    result = ReflectResult(
        verdict=_verdict,
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
        # pass on garbage was historically dangerous: run e8c94b86
        # (2026-04-27) had reflect emit verdict='fail' (not in the schema)
        # while the reasoning correctly noted the deliverable was missing,
        # and the coercion silently flipped it to pass — shipping a fake-pass
        # to any caller without its own pass-but-no-output check.
        # "retry" is the correct default for malformed verdicts: the model
        # tried to say something other than pass, so don't pretend it said
        # pass. The retry budget will catch genuinely-broken cases.
        logger.warning(
            "Invalid reflect verdict %r (%s), coercing to 'retry'",
            result.verdict,
            (result.reasoning or "")[:120],
        )
        result.verdict = "retry"
        _verdict_coerced = True

    # Structured attribution (optional — reflect prompt will be updated to emit
    # these in Phase 2. For now, accept them if present and default otherwise).
    cause = data.get("failure_cause", "none")
    if cause not in FAILURE_CAUSES:
        logger.debug("Unknown failure_cause %r from reflect, defaulting to none", cause)
        cause = "none"
    # If verdict says failure but cause defaulted to none, mark unknown via "env"
    # fallback? No — keep strict; downstream treats "none" on non-pass as missing.
    result.failure_cause = cause

    # Self-contradiction guard: a non-pass verdict that explicitly attributes
    # the failure to "none" is claiming a failure and denying one in the same
    # object. Observed in the 14-day verdict review (Aug 2026): verdict=escalate,
    # failure_cause="none", reasoning "the ask was fully satisfied" — a
    # strictness artifact that cost a real escalation notification.
    # Scope is deliberately narrow: only an EXPLICIT "none" trips this. A
    # missing or unrecognized failure_cause is malformed output, and malformed
    # output stays on the conservative side (see the verdict coercion above,
    # which goes the other way for the same reason).
    if result.verdict in ("retry", "escalate") and data.get("failure_cause") == "none":
        logger.warning(
            "Reflect verdict %r carried failure_cause=none — coercing to 'pass' (%s)",
            result.verdict,
            (result.reasoning or "")[:120],
        )
        result.verdict = "pass"
        result.reasoning = (
            result.reasoning or ""
        ) + " [verdict coerced to pass: non-pass verdict carried failure_cause=none]"

    conf_explicit = False
    try:
        conf = float(data.get("confidence", 0.0))
        conf_explicit = "confidence" in data
    except (TypeError, ValueError):
        conf = 0.0
    result.confidence = max(0.0, min(1.0, conf))

    # Materiality floor (2026-08-27 verdict audit): the prompt defines
    # confidence <0.5 as ambiguous evidence and the materiality bar grades
    # ambiguity as pass, yet low-confidence retry/escalate verdicts kept
    # landing (a 0.45 escalate whose only failure was evidence the verifier
    # couldn't see). Enforce mechanically — but only on an EXPLICIT numeric
    # confidence the model itself emitted. Coerced verdicts and grades that
    # omit confidence carry no meaningful self-assessment, and flipping
    # them to pass would undo the deliberate conservative coercions above.
    floor = float(getattr(settings, "reflect_nonpass_confidence_floor", 0.5) or 0)
    if (
        conf_explicit
        and not _verdict_coerced
        and floor > 0
        and result.verdict in ("retry", "escalate")
        and result.confidence < floor
    ):
        logger.info(
            "Reflect %s at confidence %.2f (< %.2f floor) — downgrading to pass-with-lessons (%s)",
            result.verdict,
            result.confidence,
            floor,
            (result.reasoning or "")[:120],
        )
        result.reasoning = (result.reasoning or "") + (
            f" [downgraded from {result.verdict}: confidence {result.confidence:.2f} is below the "
            f"{floor:.2f} materiality floor — ambiguous evidence grades pass; findings kept as lessons]"
        )
        result.verdict = "pass"
        result.failure_cause = "none"

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

    # Turn digest — structured record of what happened during the attempt.
    # Defensively trim per-call result_excerpts so a misbehaving model can't
    # exfiltrate megabytes through this field. The prompt asks for ≤2000
    # chars per excerpt; we enforce here regardless.
    # On pass verdicts, only store if reflect_emit_digest_on_pass is enabled
    # (default off — saves tokens since the next turn starts fresh anyway).
    raw_digest = data.get("turn_digest")
    if isinstance(raw_digest, dict) and raw_digest:
        emit_on_pass = bool(getattr(settings, "reflect_emit_digest_on_pass", False))
        if result.verdict != "pass" or emit_on_pass:
            result.turn_digest = _sanitize_turn_digest(raw_digest)

    # Mechanical lesson effector — only meaningful on retry; capped and
    # validated against the registry by the consumer (hooks).
    raw_excl = data.get("retry_without_tools")
    if result.verdict == "retry" and isinstance(raw_excl, list):
        result.retry_without_tools = [str(t)[:80] for t in raw_excl if isinstance(t, str) and t.strip()][:5]

    raw_cited = data.get("cited_policies")
    if isinstance(raw_cited, list):
        result.cited_policies = [str(p).strip("[] ")[:64] for p in raw_cited if isinstance(p, str) and p.strip()][:5]

    raw_exp = data.get("experience")
    if settings.reflect_experience and isinstance(raw_exp, dict) and raw_exp:
        result.experience = _sanitize_experience(raw_exp)

    return result


_SENTIMENTS = frozenset({"satisfied", "neutral", "frustrated", "unknown"})
_FRICTION_LABEL_RE = __import__("re").compile(r"[^a-z0-9_]+")


def _sanitize_experience(raw: dict) -> dict:
    """Enforce the experience schema regardless of what the model emitted.

    Booleans absent or non-boolean are dropped (not coerced to False — an
    unanswered question is not a "no", and Candor frequency observations must
    only count real reads). Friction labels are normalized to snake_case so
    the same failure mode can't split into a dozen categorical values on
    casing alone.
    """
    cleaned: dict = {}
    sentiment = str(raw.get("user_sentiment", "") or "").strip().lower()
    cleaned["user_sentiment"] = sentiment if sentiment in _SENTIMENTS else "unknown"

    for key in ("clarification_loop", "first_response_sufficient"):
        val = raw.get(key)
        if isinstance(val, bool):
            cleaned[key] = val

    friction = raw.get("friction") or []
    if isinstance(friction, list):
        labels = []
        for f in friction:
            label = _FRICTION_LABEL_RE.sub("_", str(f).strip().lower()).strip("_")[:40]
            if label and label not in labels:
                labels.append(label)
        cleaned["friction"] = labels[:6]
    else:
        cleaned["friction"] = []

    observations = raw.get("user_observations") or []
    if isinstance(observations, list):
        cleaned["user_observations"] = [str(o).strip()[:400] for o in observations if str(o).strip()][:3]
    else:
        cleaned["user_observations"] = []

    cleaned["note"] = str(raw.get("note", "") or "").strip()[:500]
    return cleaned


def _sanitize_turn_digest(digest: dict) -> dict:
    """Defensively trim a turn_digest dict to enforce per-field caps.

    Trust-but-verify: the prompt specifies caps, but we enforce them here so
    a misbehaving or runaway model can't blow up the post_mortem payload or
    the next scout's input.
    """
    cap = max(int(getattr(settings, "reflect_digest_max_chars_per_excerpt", 2000) or 2000), 200)
    cleaned: dict = {}
    cleaned["scout_plan_summary"] = str(digest.get("scout_plan_summary", ""))[:1000]
    cleaned["agent_final_response"] = str(digest.get("agent_final_response", ""))[:2000]
    cleaned["what_was_tried"] = str(digest.get("what_was_tried", ""))[:1000]

    raw_findings = digest.get("key_findings") or []
    if isinstance(raw_findings, list):
        cleaned["key_findings"] = [str(f)[:500] for f in raw_findings if f][:10]
    else:
        cleaned["key_findings"] = []

    raw_calls = digest.get("tool_calls") or []
    cleaned_calls: list[dict] = []
    if isinstance(raw_calls, list):
        for tc in raw_calls[:30]:  # bounded fan-in
            if not isinstance(tc, dict):
                continue
            outcome = tc.get("outcome", "unknown")
            if outcome not in ("success", "error", "partial"):
                outcome = "unknown"
            cleaned_calls.append(
                {
                    "tool": str(tc.get("tool", ""))[:120],
                    "args": str(tc.get("args", ""))[:500],
                    "outcome": outcome,
                    "result_excerpt": str(tc.get("result_excerpt", ""))[:cap],
                }
            )
    cleaned["tool_calls"] = cleaned_calls
    return cleaned


def _forced_cause(current: str, fallback: str) -> str:
    """Attribution for a verdict the harness forced, not the model.

    "none" is the schema's absence value, not a cause — and it is truthy, so
    the `current or fallback` idiom this replaces never fired: every clamped
    or guard-forced verdict shipped with failure_cause="none", the exact
    self-contradiction _result_from_data coerces away one step earlier.
    """
    return fallback if current in ("", "none") else current


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
    reflect_mode: str = "sync",
    tool_summary_attempts: list | None = None,
    turn_user_msg_id: int | None = None,
) -> None:
    """Persist a post-mortem artifact for this reflect invocation (Phase 2c).

    Always called — pass, retry, escalate, parse-failure. Never raises.
    The artifact is what snooze consumes; a missing row means silent signal
    loss, so we log failures but never let them propagate.

    extra_payload merges into the JSON payload (e.g. parse_error markers).

    reflect_mode stamps which grading regime produced this row ("sync" =
    blocking, retry-capable; "deferred" = observe-only background grade).
    The verdict sample now spans both, and a rate computed across the two
    without splitting them is meaningless.
    """
    try:
        payload = {
            "verdict": result.verdict,
            "reflect_mode": reflect_mode,
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
        # Per-attempt breakdown (C2) — persisted so the deferred grader and
        # later audits can scope counts per attempt; absent pre-C2 rows read
        # via .get() as before.
        if tool_summary_attempts:
            payload["tool_summary_attempts"] = tool_summary_attempts
        # Persist turn_digest when present so the next scout can read it on
        # retry (sessions/manager.py composes scout_message from this).
        # Older readers use .get(), so adding the key is back-compat-safe.
        if result.turn_digest:
            payload["turn_digest"] = result.turn_digest
        if result.experience:
            payload["experience"] = result.experience
        if (result.grounding or {}).get("ungrounded") or (result.grounding or {}).get("unverified_rows"):
            payload["grounding"] = result.grounding
        if extra_payload:
            payload.update(extra_payload)
        # Canary isolation (plan §5): stamp the payload so downstream
        # consumers (synthesis, dream evidence, the Phase 4 tripwire window)
        # can exclude these rows even after the session itself is pruned.
        try:
            _sess = db.get_session(session_id) or {}
            if _sess.get("session_type") == "canary":
                payload["session_type"] = "canary"
        except Exception:
            pass
        # H2 stamps (plan §12.4), here at the single choke point so EVERY
        # post-mortem carries them (parse-error and error paths included).
        # model_override is an in-memory AgentSession field, never a DB
        # column — reading it from the sessions row mislabeled every
        # override-running session as settings.llm_model.
        if "agent_model" not in payload:
            try:
                from sessions.manager import get_manager as _get_mgr

                _live = _get_mgr().get(session_id)
                payload["agent_model"] = (
                    (getattr(_live, "model_override", None) if _live else None) or settings.llm_model or ""
                )
            except Exception:
                pass
        if scout_report is not None and "task_category" not in payload:
            # Scout's task_type is the real classification axis; the old
            # execution_mode stamp survives only as the fallback for reports
            # that predate the field (cache, fallback, older deploys) so
            # rows never lose their category outright.
            payload["task_category"] = (
                getattr(scout_report, "task_type", "") or getattr(scout_report, "execution_mode", "") or ""
            )
        # Decoupled turn metrics (tokens + wall clock, retries included):
        # anchored on the turn's user message so synthesis can accumulate
        # per-(model, category) averages. Observability only — no consumer
        # routes or budgets on it. Guarded: a metrics failure must never
        # cost the post-mortem row.
        if turn_user_msg_id and "turn_metrics" not in payload:
            try:
                _msg = db.get_message(int(turn_user_msg_id))
                _t0 = str((_msg or {}).get("created_at") or "")
                if _t0:
                    # End anchor = the turn's last message, NOT now: deferred
                    # reflect grades minutes after the turn ends, and a
                    # now-anchored window would add the deferred delay to
                    # wall_ms and swallow any next turn's tokens.
                    _t1 = db.session_last_message_at(session_id, int(turn_user_msg_id)) or ""
                    _usage = db.session_token_usage_since(session_id, _t0, until_iso=_t1)
                    _started = datetime.fromisoformat(_t0)
                    if _started.tzinfo is None:
                        _started = _started.replace(tzinfo=timezone.utc)
                    if _t1:
                        _ended = datetime.fromisoformat(_t1)
                        if _ended.tzinfo is None:
                            _ended = _ended.replace(tzinfo=timezone.utc)
                    else:
                        _ended = datetime.now(timezone.utc)
                    _wall_ms = int((_ended - _started).total_seconds() * 1000)
                    payload["turn_metrics"] = {
                        "tokens": int(_usage.get("total") or 0),
                        "llm_calls": int(_usage.get("calls") or 0),
                        "wall_ms": max(_wall_ms, 0),
                    }
            except Exception as me:
                logger.debug("turn_metrics stamp failed for %s: %s", session_id, me)
        scout_viability = None
        execution_mode = None
        if scout_report is not None:
            scout_viability = getattr(scout_report, "viability", None)
            execution_mode = getattr(scout_report, "execution_mode", None)
            payload["scout_summary"] = {
                "viability": scout_viability,
                "viability_notes": list(getattr(scout_report, "viability_notes", []) or []),
                "execution_mode": execution_mode,
                "task_type": getattr(scout_report, "task_type", "") or "",
                "recommended_tools": list(scout_report.recommended_tools or []),
                "recommended_skills": list(scout_report.recommended_skills or []),
                "deliverables_plan": [
                    {"description": d.description, "execution_hint": d.execution_hint}
                    for d in (scout_report.deliverables_plan or [])
                ],
                "from_cache": bool(getattr(scout_report, "from_cache", False)),
                "from_fallback": bool(getattr(scout_report, "from_fallback", False)),
                # Usage counted at scout submit-time; carried here so
                # synthesis can attribute this turn's outcome to the hints.
                "used_hints": list(getattr(scout_report, "used_hints", []) or []),
            }
        if result.cited_policies:
            payload["cited_policies"] = list(result.cited_policies)

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


async def _save_user_observations(session_id: str, result: ReflectResult) -> None:
    """Route reflect's per-turn user observations into the user model.

    Reflect reads every full transcript anyway; this makes it a sensor for
    the user model instead of only a verdict machine. Writes go through the
    normal store path — add_entry's dedup gate absorbs repeats, and user.*
    writes flow into Candor's user_fact attestations via the store hook.
    Never raises; a memory problem must not affect the verdict.
    """
    observations = (result.experience or {}).get("user_observations") or []
    if not observations or not (settings.reflect_experience and settings.memory_recall):
        return
    try:
        # Canary sessions never write memory (plan §5) — same rule as distill.
        session = db.get_session(session_id) or {}
        if session.get("session_type") == "canary":
            return
        from core.memory.store import get_memory_store

        store = get_memory_store()
        if not store:
            return
        for content in observations:
            if len(content) < 20:
                continue  # too short for the dedup gate to score reliably
            await asyncio.to_thread(
                store.add_entry,
                content=content,
                file_name="user.profile",
                entry_type="profile",
                tags=f"user,profile,reflect,{time.strftime('%Y-%m-%d')}",
                weight="normal",
                source="reflect",
            )
    except Exception as e:
        logger.debug("Reflect user-observation save failed for %s: %s", session_id, e)


async def reflect_on_session(
    session_id: str,
    emit=None,
    attempt: int = 1,
    tool_summary: dict | None = None,
    scout_report=None,
    extra_evidence: str = "",
    turn_user_msg_id: int | None = None,
    termination_reason: str | None = None,
    prior_termination_reasons: list[str] | None = None,
    gate_results: list | None = None,
    reflect_mode: str = "sync",
    tool_summary_attempts: list | None = None,
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
        reflect_mode: "sync" (blocking, gates the retry loop) or "deferred"
            (observe-only background grade). Stamped on the post-mortem so the
            two regimes stay separable in verdict-rate analysis.

    Returns:
        ReflectResult with verdict and optional lessons
    """
    # Off-loop: evidence assembly loads the full transcript from the DB.
    grounding: dict = {}
    user_request, evidence = await asyncio.to_thread(
        _build_evidence,
        session_id,
        attempt=attempt,
        tool_summary=tool_summary,
        scout_report=scout_report,
        turn_user_msg_id=turn_user_msg_id,
        termination_reason=termination_reason,
        prior_termination_reasons=prior_termination_reasons,
        tool_summary_attempts=tool_summary_attempts,
        grounding_out=grounding,
    )
    if not user_request or not evidence:
        r = ReflectResult(verdict="pass", reasoning="No user request found to verify")
        await asyncio.to_thread(
            _write_post_mortem,
            session_id,
            attempt,
            r,
            scout_report,
            tool_summary,
            reflect_mode=reflect_mode,
            tool_summary_attempts=tool_summary_attempts,
            turn_user_msg_id=turn_user_msg_id,
        )
        return r

    if extra_evidence:
        evidence = f"{evidence}\n\n{extra_evidence}"

    # Deterministic gate evidence (plan 3a): appended so the model sees it,
    # and enforced mechanically below regardless of what it concludes.
    if gate_results:
        from core.gates import format_evidence as _gate_evidence

        evidence = f"{evidence}\n\n{_gate_evidence(gate_results)}"

    if emit:
        emit({"type": "reflect.start"})

    # Three-role scheme: the verifier runs on Primary — its verdicts gate
    # retries, worker trust, and routing; it must never be weaker than the
    # model it judges.
    # Primary, with the Background model as a safety net for degenerate
    # configs where llm_model is unset (per-session overrides carrying the
    # agent turns) — chat(model="") would otherwise reach the provider.
    model = settings.llm_model or settings.background_model
    start = time.monotonic()

    # Inner retry budget for the reflect LLM call itself. A parse failure here
    # is a verifier-side problem; re-running scout + the agent loop (~5-10 min)
    # to recover from a malformed JSON response is a large mismatch in cost,
    # so we retry the reflect call locally before falling back to a soft pass.
    # Bumped to 2 (was 1) because the turn_digest is now load-bearing on retry —
    # a parse-loss at this layer means the next scout flies blind.
    INNER_REPROMPT_LIMIT = 2
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

            # Output budget bumped from 2048 → 8192. A passing verdict still
            # generates only a few hundred tokens (model self-regulates), but a
            # retry/escalate verdict now also emits the turn_digest with
            # verbatim tool-result excerpts, which can easily exceed 2k tokens.
            # Output cost is per-token-generated, so the headroom is free until
            # actually used.
            # Reflect gates turn completion (and parent check_workers), so it
            # queues with the session's own scheduling identity rather than the
            # default background identity, which sorts last in the fair queue.
            # Carrying the session_id also subjects the call to the session's
            # wall-clock budget — guarantee headroom so a turn that spent its
            # whole budget still gets a verdict instead of an UNVERIFIED stamp.
            from core.llm.client import ensure_session_budget
            from core.llm.client import sched_identity as _sched_identity

            ensure_session_budget(session_id, 120)
            _created_at, _priority = _sched_identity(session_id)
            from core.llm.client import chat_with_backup

            response = await chat_with_backup(
                client,
                messages=messages,
                model=model,
                max_tokens=8192,
                session_id=session_id,
                session_created_at=_created_at,
                session_priority=_priority,
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

            # Contentless-pass guard (ARC-3 sweep): a "pass" with zero
            # analysis on a turn with real tool work is a lazy grade, not a
            # considered one. Spend one re-prompt like the parse-failure
            # path; if the grader stays silent, accept but say so loudly in
            # the record instead of looking like a confident approval.
            try:
                _work = sum(
                    (v.get("calls", 0) if isinstance(v, dict) else int(v or 0)) for v in (tool_summary or {}).values()
                )
            except (TypeError, ValueError):
                _work = 0
            if result.verdict == "pass" and not (result.reasoning or "").strip() and _work >= 5:
                if inner_attempt < INNER_REPROMPT_LIMIT:
                    logger.warning(
                        "Reflect returned a contentless pass for session %s "
                        "(%d tool calls) — reprompting (inner_attempt=%d)",
                        session_id,
                        _work,
                        inner_attempt,
                    )
                    continue
                result.reasoning = "(grader returned no analysis after re-ask — treat this pass as low-confidence)"

            result.grounding = dict(grounding)

            # Defensive ceiling-loop guard: if the agent hit round_ceiling on
            # this turn AND on at least one prior turn, retry is provably
            # hopeless against the same hard wall. Override the LLM's verdict.
            # Only round_ceiling is guarded; other terminal reasons (compaction,
            # budget) often have legitimate retries — they get the prompt rule
            # but not this code-level override.
            if (
                termination_reason == "round_ceiling"
                and any(r == "round_ceiling" for r in (prior_termination_reasons or []))
                and result.verdict == "retry"
            ):
                logger.info(
                    "Reflect ceiling-loop guard: forcing escalate (was %s) for session %s",
                    result.verdict,
                    session_id,
                )
                result.verdict = "escalate"
                # The wall is the task's shape against the round budget, not a
                # bad plan or a bad execution of one.
                result.failure_cause = _forced_cause(result.failure_cause, "task")
                if not result.missing:
                    result.missing = (
                        "Agent hit round_ceiling on consecutive attempts. Either split "
                        "the task into smaller pieces or raise max_tool_rounds in settings."
                    )

            # Gate clamp (plan 3a): a failing deterministic gate makes `pass`
            # unreachable — mechanically, AFTER the LLM call, BEFORE the
            # post-mortem write, so the artifact records the clamped verdict
            # (Phase 4's tripwire reads post-mortems; an unclamped record
            # would poison it). An LLM judge cannot overrule an exit code.
            if gate_results:
                from core.gates import failing as _gates_failing

                _failed = _gates_failing(gate_results)
                if _failed and result.verdict == "pass":
                    names = ", ".join(g.name for g in _failed)
                    logger.info(
                        "Gate clamp: verdict pass -> retry for session %s (failing: %s)",
                        session_id,
                        names,
                    )
                    result.verdict = "retry"
                    # The gate names work the turn did not finish; the model's
                    # own attribution wins when it identified something else.
                    result.failure_cause = _forced_cause(result.failure_cause, "task")
                    result.reasoning = (
                        f"[gate clamp] Deterministic gate(s) failing: {names}. "
                        f"A pass verdict is unreachable while a gate fails. "
                    ) + (result.reasoning or "")
                    if not result.missing:
                        result.missing = f"Make the failing gate(s) pass: {names}"

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
            # H2 stamps (agent_model/task_category) now land inside
            # _write_post_mortem itself, covering every write path.
            extra_payload = extra_payload or {}
            if gate_results:
                extra_payload["gates"] = [g.to_payload() for g in gate_results]
            await asyncio.to_thread(
                _write_post_mortem,
                session_id,
                attempt,
                result,
                scout_report,
                tool_summary,
                extra_payload=extra_payload,
                reflect_mode=reflect_mode,
                tool_summary_attempts=tool_summary_attempts,
                turn_user_msg_id=turn_user_msg_id,
            )
            await _save_user_observations(session_id, result)
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
        await asyncio.to_thread(
            _write_post_mortem,
            session_id,
            attempt,
            r,
            scout_report,
            tool_summary,
            extra_payload={"parse_error": True, "raw_response_excerpt": (raw or "")[:500]},
            reflect_mode=reflect_mode,
            tool_summary_attempts=tool_summary_attempts,
            turn_user_msg_id=turn_user_msg_id,
        )
        return r

    except Exception as e:
        logger.warning("Reflect failed for session %s: %s", session_id, e)
        r = ReflectResult(verdict="pass", reasoning=f"Reflect error: {e}")
        await asyncio.to_thread(
            _write_post_mortem,
            session_id,
            attempt,
            r,
            scout_report,
            tool_summary,
            reflect_mode=reflect_mode,
            tool_summary_attempts=tool_summary_attempts,
            turn_user_msg_id=turn_user_msg_id,
        )
        return r
