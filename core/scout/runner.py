"""Pernix — Scout agent runner: lifecycle, caching, fallback, validation.

The scout runs as a multi-turn tool-calling agent. It gathers context
iteratively via read-only tools (memory, skills, tools, sessions), then
submits a structured ScoutReport via the submit_report tool.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import traceback
from pathlib import Path
from typing import Callable

import httpx

from config import settings
from core.llm.errors import FailoverError, FailoverReason
from core.llm.semaphore import PRIORITY_BACKGROUND, PRIORITY_ORCHESTRATOR, PRIORITY_WORKER
from core.llm.types import TokenUsage
from core.scout.report import ScoutReport, SessionBrief
from db import models as db

logger = logging.getLogger("pernix.scout")


# ---------------------------------------------------------------------------
# Retry helpers
# ---------------------------------------------------------------------------

# HTTP status codes that are safe to retry (transient server issues, model loading)
_RETRYABLE_STATUS_CODES = frozenset({500, 502, 503, 429})

# FailoverReasons worth retrying. AUTH / MODEL_NOT_FOUND / CONTEXT_OVERFLOW /
# FORMAT_ERROR will not fix themselves on retry.
_RETRYABLE_FAILOVER_REASONS = frozenset(
    {
        FailoverReason.RATE_LIMIT,
        FailoverReason.OVERLOADED,
        FailoverReason.TIMEOUT,
        FailoverReason.UNKNOWN,
    }
)


def _is_retryable(error: Exception) -> bool:
    """Check if an error is transient and worth retrying."""
    if isinstance(error, FailoverError):
        if error.reason in _RETRYABLE_FAILOVER_REASONS:
            return True
        if isinstance(error.original, httpx.HTTPStatusError):
            return error.original.response.status_code in _RETRYABLE_STATUS_CODES
        return False
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in _RETRYABLE_STATUS_CODES
    if isinstance(error, (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout)):
        return True
    if isinstance(error, ConnectionError | OSError):
        return True
    return False


def _retry_wait_seconds(error: Exception, attempt: int) -> int:
    """Backoff schedule for a failed scout attempt before the next retry.

    Server-side errors (FailoverError, HTTPStatusError) recover quickly so we
    keep their wait short. Connection / timeout errors may indicate the model
    is loading or the server is saturated, so we back off more aggressively.
    """
    if isinstance(error, FailoverError) or isinstance(error, httpx.HTTPStatusError):
        return 2 * attempt  # 2s, 4s
    return 5 * attempt  # 5s, 10s


def _log_scout_error(error: Exception, session_id: str, attempt: int, max_attempts: int, elapsed_ms: int) -> None:
    """Log detailed diagnostic info for scout failures."""
    parts = [
        f"Scout failed for session {session_id} (attempt {attempt}/{max_attempts}, {elapsed_ms}ms)",
        f"  Error type: {type(error).__module__}.{type(error).__qualname__}",
        f"  Error message: {error}",
    ]
    # Unwrap FailoverError so the diagnostic block below sees the underlying
    # httpx error (response body, "Likely cause" hint) rather than the wrapper.
    if isinstance(error, FailoverError):
        parts.append(f"  Failover reason: {error.reason.value}")
        if error.original is not None:
            error = error.original
            parts.append(f"  Wrapped error: {type(error).__module__}.{type(error).__qualname__}: {error}")
    if isinstance(error, httpx.HTTPStatusError):
        resp = error.response
        parts.append(f"  HTTP status: {resp.status_code}")
        parts.append(f"  URL: {resp.request.url}")
        # Log response body for diagnostics (Ollama often includes error details)
        try:
            body = resp.text[:500]
            parts.append(f"  Response body: {body}")
        except Exception:
            pass
        if resp.status_code == 500 and "ollama" in str(resp.request.url):
            parts.append(
                "  Likely cause: Ollama internal error (tool call XML parsing failure or model issue — usually transient)"
            )
    if isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout)):
        parts.append("  Likely cause: Ollama server unreachable or model loading")
    if isinstance(error, httpx.ReadTimeout):
        parts.append("  Likely cause: Ollama request timed out (model may be loading or overloaded)")

    parts.append(f"  Traceback: {traceback.format_exception_only(type(error), error)[-1].strip()}")
    logger.warning("\n".join(parts))


# ---------------------------------------------------------------------------
# Scout system prompt (multi-turn, tool-calling)
# ---------------------------------------------------------------------------

SCOUT_SYSTEM_PROMPT = """You are a Scout Agent. Your job is to prepare context for a main agent that will handle the user's request. You do NOT handle the request yourself.

Your initial context already includes baseline memory search results, available tools, available skills, and cross-session findings. Review these carefully before deciding if you need more. When baseline memory or cross-session findings substantively cover the user's request, your approach_guidance must synthesize from those findings first — treat external search (search_web/browse_web) as supplementation, not the default opening move.

When [OPERATIONAL INTEL] is present (calibrated reliability from logged outcome history):
- It is an EXCEPTION REPORT: it lists only degraded or conditional items. A tool or domain absent from the block has no known problem — never report its absence as a concern or a gap.
- Fold relevant entries into approach_guidance: steer the plan away from targets with low success rates, and toward any stated working condition (e.g. "works when method=browse" means plan browse_web for those domains instead of http_get; a failure-mode line like "rate_limit 38%" means plan for backoff or an alternative source).
- The percentages are calibrated from real observation counts. Weigh them by evidence: a wide credible interval or few obs is weak evidence; many obs is strong. An [unstable] or [under_specified] tag means the rate is context-dependent — flag that uncertainty in your plan rather than trusting the point estimate.
- When reliability is central to the task, add predict_reliability / why_reliability / reliability_questions to recommended_tools so the main agent can query live calibrated numbers and their evidence chains.

When [ADAPTIVE ROUTING HINTS] is present (machine-curated tool/skill selection guidance, human-governed): fold relevant hints into your tool and skill recommendations. Hints are advisory — evidence-backed but not binding; the user's explicit request always wins.

When [MODEL ROUTING INTEL] is present (observed verdict rates by model and task category): it is an exception report — a model absent from it has no known problem. Steer recommended_model away from listed (model, category) pairs when a viable alternative exists; never report a model's absence as a concern.

You also have tools to search deeper if the baseline is insufficient:
- search_memory: Run additional memory queries with different keywords or modes. If a preloaded snippet is truncated and looks relevant, call search_memory with keywords from that entry and file=<file_name> to retrieve the complete content from that file.
- search_sessions: Search other sessions with different queries
- search_tools: Discover additional tools by capability
- search_skills: Find more skill packages
- read_skill_instructions: Read full instructions for a skill before recommending it
- search_post_mortems: Look up past failure narratives (filter by failure_cause or subject tool/skill name). Use when you suspect a prior failure pattern is relevant.
- search_adaptive: Search machine-curated routing hints, prompt notes, and policies by keyword (only useful when the adaptive layer is enabled).

PROCEDURE:
1. Review the user message, session context, and pre-loaded baseline data (memory, tools, skills).
2. If the baseline data is sufficient, call submit_report immediately with your recommendations.
3. If you need deeper context (e.g., a skill looks promising but you want to read its instructions, or you want to search memory with different keywords), use your tools first.
4. Call submit_report exactly once to deliver your findings.

IMPORTANT: You have a maximum of 6 tool rounds. You MUST call submit_report by round 5 at the latest — round 6 disables tools and is reserved for emergency text output. Do not exhaust all rounds searching — gather what you need quickly, then submit. If in doubt, submit with what you have rather than searching more.

You MUST call submit_report to deliver your findings. If you cannot use tools, output a raw JSON report instead (same fields as submit_report).

REPORT FIELD GUIDANCE:
- memory_context: Relevant knowledge from your memory searches. Quote with attribution. Max 500 tokens. Report FACTS YOU FOUND — never conclusions about what is missing. Do NOT write "no X is configured" or "SESSIONS.md shows X: not set". An unfilled field in SOUL/RULES/SESSIONS is deployment config left blank, not evidence the fact is unknown, and asserting otherwise makes the main agent refuse tasks it could have answered from the very facts you just quoted. If memory answers the request, state the answer plainly and let the agent use it.
- cross_session_context: Relevant findings from session searches. Quote with session attribution. Max 500 tokens. Empty string if nothing relevant.
- recommended_tools: Array of tool names the main agent will need (5-15 tools). Only include extension tools — builtin tools are always available.
- tool_rationale: One sentence explaining your tool selection.
- recommended_skills: Array of skill names (0-3). Skills are pre-built instruction packages with workflows, scripts, and reference material. The first skill listed will be auto-loaded. Prioritize skills that were successfully used in past sessions. Empty array only if no skills match.
- skill_rationale: One sentence explaining skill selection and how the skill(s) apply. Empty string if no skills needed.
- recommended_model: If the task requires specific capabilities (vision, code, long-context), recommend a model ID from the AVAILABLE MODELS list. Empty string if current model is fine.
- model_rationale: One sentence explaining why this model is needed. Empty string if no model switch.
- session_state: Brief session orientation. Max 200 tokens.
- approach_guidance: Step-by-step plan for approaching this task. Number each step, name tools/skills, flag risks, incorporate lessons from memory/past sessions. Max 500 tokens. **MEMORY-FIRST ORDERING**: If the baseline MEMORY SEARCH RESULTS or cross-session findings substantively cover the user's request, step 1 of approach_guidance MUST synthesize from those findings before any external research. Treat search_web/browse_web as supplementation for verification or gap-filling, not the default first move. Only when memory baseline is empty or clearly insufficient should external search lead the plan.
- deliverables_plan: Array of concrete work items the agent is expected to produce (0-6). Each item has a "description" (the artifact or outcome, e.g. "Write summary.md with key findings") and an optional "execution_hint" (inline | task | worker). Leave empty for pure Q&A. Reflect will check each item at turn end, so be specific and measurable.
- execution_mode: Overall approach — "inline" (default, single-agent work) or "tasks" (multi-step sequential via task system).

RULES:
- Be terse. Every token costs the main agent context space.
- Only include entries RELEVANT to this specific task.
- SOUL.md/RULES.md/SESSIONS.md in your context are for YOUR planning only — the main agent receives those files directly and in full. Never copy their content into report fields; reference a rule by name in approach_guidance when it shapes the plan.
- You have read-only access. You cannot modify anything.
- SKILLS are the highest-leverage recommendation — a single skill can replace many tool rounds of trial-and-error. If memory mentions a skill in a successful context, recommend it.
- APPROACH GUIDANCE is the most important field — it becomes the agent's playbook. Write numbered steps, name tools/skills, flag risks, reference past lessons.
- REFLECT RETRY: When the user message contains a [REFLECT — Retry #N] block, the agent's full prior-turn history is still in its conversation context — all file reads and tool results are already visible. Your approach_guidance MUST NOT repeat steps already completed. Start the plan from the first incomplete step. If the retry block lists "Tools already used," treat those as done; instruct the agent to use glob or file_read only to confirm workspace state, not to re-fetch data it already has.
- WORKER DELEGATION: Recommend spawn_worker, check_workers, await_workers, get_worker_result when: (a) multiple independent subtasks benefit from parallelism, (b) a subtask needs a different model, (c) large divide-and-conquer scope, or (d) data fetching followed by processing. Do NOT recommend for simple tasks. If session type is "worker", NEVER recommend orchestration tools.
- PYTHON PACKAGES: Workspace venv at data/workspace/.venv/ is auto-activated. Use bash with pip or discover_tools for package management.
- USER INTENT: When the user names a specific action/tool, prioritize matching tools. User preference > efficiency.
- LIVE STATE BEATS MEMORY: For mutable operational state — worker limits, scheduled jobs, active models, settings values, feature toggles — the live tool answer (list_scheduled_jobs, list_features, list_available_models, telos_status, the spawn error text itself) is the source of truth. A memory entry about such state records the past: use it to know WHERE to look, never as the current value. When a plan depends on such a value, make the live check a step; do not copy a remembered limit, job list, or setting into approach_guidance as fact. (This is the mutable-state complement of KNOWN FACTS BEAT EMPTY CONFIG below, which covers stable user facts.)
- KNOWN FACTS BEAT EMPTY CONFIG: If a request needs a fact about the user (location, timezone, name, preferences) and memory has it, put that fact in memory_context and build approach_guidance around USING it. Do not plan a clarifying question for something memory already answers, and do not treat a blank field in SESSIONS.md as contradicting a recalled fact. Ask the user only when neither memory nor config has it, or when memory is genuinely ambiguous (e.g. two conflicting locations) — in which case say so and name the candidates.
- SESSION HISTORY QUERIES: For "what did we do today/yesterday/recently" — recommend list_recent_sessions (chronological, timestamp-ordered). search_sessions is FTS5 keyword search over message CONTENT; use it to find sessions where a topic was discussed, never to find sessions by date. Pair list_recent_sessions + read_session_summary for deep dives into specific sessions.
- TIME ZONES: The injected CURRENT DATE/TIME shows both UTC and local time. All harness timestamps (sessions, messages, cron runs) are stored in UTC (+00:00). "Today" and "yesterday" mean local time, not UTC — use the local time for date math. Never assume a date boundary from UTC alone.
- Do NOT use <think> or reasoning tags. /no_think"""

# Injected into the RULES block only when settings.rlm_enabled (the tool only
# exists then). Kept out of the static prompt so disabled servers never bias
# scout toward a tool the agent doesn't have.
_SCOUT_RLM_RULE = (
    "- RECURSIVE ANALYSIS: When the task means densely analyzing a very large input "
    "(huge file, multi-document corpus, transcript, log dump, session history — anything "
    "over ~100K chars needing whole-input understanding), recommend rlm_process and make "
    "approach_guidance stage the content as workspace file(s) first, then call "
    "rlm_process(task=..., source=paths). Do NOT plan paginated file_read loops over "
    "inputs that size, and do NOT recommend it for inputs that fit in context."
)

# Injected only when settings.gates_enabled (add_gate only exists then). A
# structural spec is countable, so the mechanical gate — not the probabilistic
# post-hoc Reflect pass — is the right verifier: gates run before Reflect at
# turn end and a failing one blocks a pass verdict without burning a retry on
# something a 5-line checker could have caught.
_SCOUT_GATE_RULE = (
    "- STRUCTURAL SPECS: When the request pins countable properties of a deliverable "
    "(exactly N sections, ≥M words each, K items/facts per section, a file that must "
    "exist or parse), recommend add_gate and make registering the gate the FIRST step "
    "of approach_guidance: a short shell/python command over the output file that exits "
    "non-zero while any count is unmet, with watch_paths on the deliverable. The gate "
    "runs before Reflect at turn end and mechanically blocks a pass verdict. Do NOT "
    "register gates for non-countable qualities (tone, accuracy, relevance) — those "
    "stay with Reflect."
)


def _scout_system_prompt() -> str:
    """SCOUT_SYSTEM_PROMPT plus conditional rules, keeping /no_think last."""
    rules = []
    if settings.rlm_enabled:
        rules.append(_SCOUT_RLM_RULE)
    if settings.gates_enabled:
        rules.append(_SCOUT_GATE_RULE)
    if not rules:
        return SCOUT_SYSTEM_PROMPT
    block = "\n".join(rules)
    head, _, tail = SCOUT_SYSTEM_PROMPT.rpartition("\n- Do NOT use <think>")
    if not head:  # tail marker drifted — fail open with the static prompt
        return SCOUT_SYSTEM_PROMPT + "\n" + block
    return f"{head}\n{block}\n- Do NOT use <think>{tail}"


# ---------------------------------------------------------------------------
# Scout tool schemas (OpenAI function-calling format)
# ---------------------------------------------------------------------------

_SCOUT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_memory",
            "description": "Search persistent memory for relevant knowledge, lessons learned, past approaches, and skill usage history. Supports keyword and hybrid search.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query — use specific keywords for best results"},
                    "mode": {
                        "type": "string",
                        "enum": ["hybrid", "bm25", "recent"],
                        "description": "Search mode: hybrid (keyword+temporal, default), bm25 (keyword only), recent (last 24h)",
                    },
                    "limit": {"type": "integer", "description": "Max results to return (default 10, max 20)"},
                    "file": {
                        "type": "string",
                        "description": "Restrict results to a specific memory file (e.g. pernix.lessons). Use to get full content from a file seen in the preloaded baseline.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_sessions",
            "description": "Search other sessions for relevant past work, conversations, and findings via full-text search. Useful for finding how similar tasks were handled before.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_tools",
            "description": "Search the tool registry for available tools matching a query. Returns tool names, descriptions, and categories.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural language description of needed capability"},
                    "limit": {"type": "integer", "description": "Max results (default 15)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_skills",
            "description": "Search for available skill packages (domain expertise with workflows, scripts, and references). Skills provide step-by-step instructions for specific task types.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Task or domain description"},
                    "limit": {"type": "integer", "description": "Max results (default 5)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_skill_instructions",
            "description": "Read the full instructions (L2 body) of a specific skill. Use after search_skills to inspect a promising skill's workflow before recommending it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Skill name (from search_skills results)"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_post_mortems",
            "description": "Look up past reflect post-mortems for targeted failure analysis. Use when the current task resembles a pattern that may have failed before (e.g. checking how a specific skill performed, or what went wrong with a particular failure cause). Returns concise summaries — NOT full payloads.",
            "parameters": {
                "type": "object",
                "properties": {
                    "failure_cause": {
                        "type": "string",
                        "description": "Optional exact failure cause filter (e.g. 'skill', 'tool', 'scout', 'none').",
                    },
                    "subject": {
                        "type": "string",
                        "description": "Optional tool or skill name to find post-mortems that referenced it.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 5, max 10).",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_adaptive",
            "description": "Search the adaptive layer (machine-curated routing hints, prompt notes, policies) by keyword. Use when the preloaded [ADAPTIVE ROUTING HINTS] block looks relevant but truncated, or to check for policy on a specific tool/skill/topic.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Keywords to match against titles and content"},
                    "kind": {
                        "type": "string",
                        "enum": ["routing_hint", "prompt_note", "policy", "worker_spec"],
                        "description": "Optional kind filter",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_report",
            "description": "Submit the final scout report. Call this exactly once when you have gathered enough context. This ends the scout session.",
            "parameters": {
                "type": "object",
                "properties": {
                    "memory_context": {
                        "type": "string",
                        "description": "Relevant memory entries with attribution (max 500 tokens)",
                    },
                    "cross_session_context": {
                        "type": "string",
                        "description": "Relevant findings from other sessions (max 500 tokens)",
                    },
                    "recommended_tools": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Extension tool names the agent will need (5-15)",
                    },
                    "tool_rationale": {"type": "string", "description": "One sentence explaining tool selection"},
                    "recommended_skills": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Skill names (0-3, first is auto-loaded)",
                    },
                    "skill_rationale": {"type": "string", "description": "One sentence explaining skill selection"},
                    "recommended_model": {
                        "type": "string",
                        "description": "Model ID if specific capabilities needed, else empty",
                    },
                    "model_rationale": {"type": "string", "description": "Why this model is needed, else empty"},
                    "session_state": {"type": "string", "description": "Brief session orientation (max 200 tokens)"},
                    "approach_guidance": {
                        "type": "string",
                        "description": "Numbered step-by-step plan with tools, risks, and lessons (max 500 tokens)",
                    },
                    "deliverables_plan": {
                        "type": "array",
                        "description": "Concrete items the agent must produce (0-6). Each item: {description, execution_hint?}. Empty for pure Q&A.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "description": {
                                    "type": "string",
                                    "description": "Concrete artifact or outcome (e.g. 'Write summary.md')",
                                },
                                "execution_hint": {
                                    "type": "string",
                                    "enum": ["inline", "task", "worker"],
                                    "description": "Per-item execution suggestion",
                                },
                            },
                            "required": ["description"],
                        },
                    },
                    "execution_mode": {
                        "type": "string",
                        "enum": ["inline", "tasks"],
                        "description": "Overall execution approach. Default 'inline' for simple tasks.",
                    },
                },
                "required": ["recommended_tools", "approach_guidance"],
            },
        },
    },
]

# Last-round tool surface: submit_report only. Scout has run out of rounds to
# search, but must still be able to deliver what it already has.
_SCOUT_SUBMIT_ONLY = [t for t in _SCOUT_TOOLS if t["function"]["name"] == "submit_report"]


# ---------------------------------------------------------------------------
# Scout tool execution
# ---------------------------------------------------------------------------


def _exec_scout_tool(name: str, args: dict, brief: SessionBrief) -> str:
    """Execute a scout tool and return the result as a string."""

    if name == "search_memory":
        try:
            from core.memory.store import get_memory_store

            store = get_memory_store()
            if not store:
                return "Memory store not available."
            query = args.get("query", "")
            mode = args.get("mode", "hybrid")
            limit = min(args.get("limit", 10), 20)
            file_filter = args.get("file", "")
            results = store.search(
                query,
                mode=mode,
                limit=limit * 2 if file_filter else limit,
                _track_hits=False,
                expand_wikilinks=True,  # H4: [[refs]] pull linked entries
            )
            if file_filter:
                results = [r for r in results if r.entry.file_name.lower() == file_filter.lower()][:limit]
            if not results:
                return "No results found."
            lines = []
            for r in results:
                content = r.entry.content
                if len(content) > 6144:  # ~2048 tokens at 3 chars/token
                    logger.warning(
                        "Large memory entry in %s (%d chars, ~%d tokens) — injecting full content into scout",
                        r.entry.file_name,
                        len(content),
                        len(content) // 3,
                    )
                lines.append(
                    f"[{r.entry.file_name} epoch={r.entry.epoch} score={r.score:.1f} type={r.entry.entry_type}] {content}"
                )
            return "\n\n".join(lines)
        except Exception as e:
            return f"Memory search error: {e}"

    elif name == "search_sessions":
        try:
            from core.scout.search import gather_cross_session_data

            query = args.get("query", "")
            result = gather_cross_session_data(query, brief.session_id)
            return result or "No relevant findings in other sessions."
        except Exception as e:
            return f"Session search error: {e}"

    elif name == "search_adaptive":
        try:
            from config import settings as _settings

            if not _settings.adaptive_enabled:
                return "Adaptive layer is disabled."
            from db import models as db

            query = (args.get("query") or "").lower()
            words = [w for w in query.split() if len(w) >= 2]
            entries = db.adaptive_list_entries(kind=args.get("kind") or None)
            scored = []
            for e in entries:
                haystack = f"{e['title']} {e['content']}".lower()
                hits = sum(1 for w in words if w in haystack)
                if hits or not words:
                    scored.append((hits, e))
            scored.sort(key=lambda t: -t[0])
            if not scored:
                return "No matching adaptive entries."
            lines = [
                f"[{e['kind']} id={e['id']} v{e['version']} scope={e['scope']}] {e['title']}: {e['content'][:400]}"
                for _, e in scored[:8]
            ]
            return "\n".join(lines)
        except Exception as e:
            return f"Adaptive search error: {e}"

    elif name == "search_tools":
        try:
            from core.tools.registry import get_registry

            registry = get_registry()
            query = args.get("query", "")
            limit = min(args.get("limit", 15), 30)
            discovered = registry.discover(query, limit=limit)
            if not discovered:
                return "No matching tools found."
            lines = []
            for t in discovered:
                lines.append(f"- {t.name} [{t.category}]: {t.description}")
            return "\n".join(lines)
        except Exception as e:
            return f"Tool discovery error: {e}"

    elif name == "search_skills":
        try:
            from core.skills.registry import get_skill_registry

            skill_reg = get_skill_registry()
            query = args.get("query", "")
            limit = min(args.get("limit", 5), 10)
            discovered = skill_reg.discover(query, limit=limit)
            if not discovered:
                return "No matching skills found."
            lines = []
            for s in discovered:
                tags_str = f" [{', '.join(s.tags[:5])}]" if s.tags else ""
                extras = []
                if s.has_scripts:
                    extras.append("has scripts")
                if s.has_references:
                    extras.append("has references")
                extra_str = f" ({', '.join(extras)})" if extras else ""
                lines.append(f"- {s.name}{tags_str}: {s.description}{extra_str}")
            return "\n".join(lines)
        except Exception as e:
            return f"Skill discovery error: {e}"

    elif name == "read_skill_instructions":
        try:
            from core.skills.registry import get_skill_registry

            skill_reg = get_skill_registry()
            skill_name = args.get("name", "")
            if not skill_reg.exists(skill_name):
                return f"Skill '{skill_name}' not found in registry."
            if skill_reg.is_disabled(skill_name):
                return f"Skill '{skill_name}' is disabled — do not recommend it."
            instructions = skill_reg.load_instructions(skill_name)
            if not instructions:
                return f"Skill '{skill_name}' has no instructions (empty SKILL.md)."
            return instructions[:12000]
        except Exception as e:
            return f"Skill read error: {e}"

    elif name == "search_post_mortems":
        try:
            from db import models as db_mod

            hits = db_mod.search_post_mortems_for_scout(
                failure_cause=(args.get("failure_cause") or None),
                subject=(args.get("subject") or None),
                limit=int(args.get("limit", 5) or 5),
            )
            return _format_post_mortem_hits(hits)
        except Exception as e:
            return f"Post-mortem search error: {e}"

    elif name == "submit_report":
        # Handled by the loop — this should not be called directly
        return "Report submitted."

    else:
        return f"Unknown tool: {name}"


def _extract_report(args: dict) -> ScoutReport:
    """Build a ScoutReport from submit_report tool arguments."""
    from core.scout.report import DeliverableSpec

    raw_deliv = args.get("deliverables_plan") or []
    deliverables = []
    if isinstance(raw_deliv, list):
        for d in raw_deliv:
            if isinstance(d, dict):
                hint = d.get("execution_hint", "inline")
                if hint not in ("inline", "task", "worker"):
                    hint = "inline"
                deliverables.append(
                    DeliverableSpec(
                        description=str(d.get("description", ""))[:500],
                        execution_hint=hint,
                    )
                )
            elif isinstance(d, str) and d.strip():
                # Tolerate models that emit a plain string per deliverable.
                deliverables.append(DeliverableSpec(description=d.strip()[:500]))

    mode = str(args.get("execution_mode", "inline"))
    if mode not in ("inline", "tasks"):
        # Clamp unknown / deprecated values (e.g. legacy "workers") to inline.
        mode = "inline"

    # identity/rules/instructions are deliberately NOT read from args: the
    # compiler injects those files whole, and honoring a model-echoed copy
    # here would let a stale or re-worded variant shadow the real ones.
    return ScoutReport(
        memory_context=str(args.get("memory_context", "")),
        cross_session_context=str(args.get("cross_session_context", "")),
        recommended_tools=args.get("recommended_tools", []) if isinstance(args.get("recommended_tools"), list) else [],
        tool_rationale=str(args.get("tool_rationale", "")),
        recommended_skills=(
            args.get("recommended_skills", []) if isinstance(args.get("recommended_skills"), list) else []
        ),
        skill_rationale=str(args.get("skill_rationale", "")),
        recommended_model=str(args.get("recommended_model", "")),
        model_rationale=str(args.get("model_rationale", "")),
        session_state=str(args.get("session_state", "")),
        approach_guidance=str(args.get("approach_guidance", "")),
        deliverables_plan=deliverables,
        execution_mode=mode,
    )


# ---------------------------------------------------------------------------
# Scout self-validation (Phase 2a)
#
# Runs a structured check on the report scout just submitted. If the report
# has fixable issues, scout gets up to _MAX_REVISIONS revision rounds with
# the issues injected. Pure Python (no extra LLM call) and bounded by the
# revision counter — can never loop forever.
# ---------------------------------------------------------------------------


def _is_degenerate_report(report: ScoutReport) -> bool:
    """True when scout produced nothing usable — no plan and no tools.

    Distinct from a *thin* report (short approach, few tools), which is still a
    real answer. This catches the two ways the scout loop can bottom out with a
    blank ScoutReport: the model never called submit_report, or its final text
    was unparseable. Callers replace these with `_build_fallback_report`.
    (identity/rules don't count here — they come from the compiler's
    directives block now, so scout output never carries them.)
    """
    return not ((report.approach_guidance or "").strip() or report.recommended_tools)


def _unfixable_issues(report: ScoutReport) -> list[str]:
    """Issues only the scout itself can fix — `_validate_report` cannot.

    These are the only issues worth spending a scout round on. Everything in
    `_sanitizable_issues` gets stripped downstream regardless of what the model
    does, so demanding a revision for those trades a working report for a
    round the scout may not survive.
    """
    issues: list[str] = []

    # approach_guidance must be non-trivial. Nothing downstream can write a
    # plan the scout didn't.
    guidance = (report.approach_guidance or "").strip()
    if len(guidance) < 30:
        issues.append(
            "approach_guidance is empty or too short — write numbered, concrete steps "
            "naming tools/skills and flagging risks."
        )

    # deliverables_plan: if present, entries must have descriptions.
    if report.deliverables_plan:
        blank = [i for i, d in enumerate(report.deliverables_plan, 1) if not (d.description or "").strip()]
        if blank:
            issues.append(
                f"deliverables_plan has {len(blank)} entries with empty descriptions. "
                "Each deliverable must name a concrete artifact or outcome."
            )

    return issues


def _sanitizable_issues(report: ScoutReport) -> list[str]:
    """Issues `_validate_report` strips automatically after scout finishes.

    Worth surfacing as viability notes, never worth a revision round: the
    hallucinated tool/skill/model is dropped either way.
    """
    issues: list[str] = []

    # 1. Recommended tools must exist in the registry AND not be disabled.
    try:
        from core.tools.registry import get_registry

        reg = get_registry()
        missing = [t for t in (report.recommended_tools or []) if not reg.exists(t)]
        if missing:
            issues.append(
                f"Recommended tools do not exist in the registry: {', '.join(missing)}. "
                "Use only tool names from the AVAILABLE TOOLS list or call search_tools."
            )
        disabled_tools = [t for t in (report.recommended_tools or []) if reg.exists(t) and reg.is_disabled(t)]
        if disabled_tools:
            issues.append(
                f"Recommended tools are disabled: {', '.join(disabled_tools)}. "
                "Drop them — the user has toggled them off in Explorer > Tools."
            )
    except Exception as e:
        logger.debug("Scout validator: tool registry check failed: %s", e)

    # 3. Recommended skills must exist, not be disabled, AND pass pre-flight validation.
    try:
        from core.skills.registry import get_skill_registry

        skill_reg = get_skill_registry()
        missing_skills = [s for s in (report.recommended_skills or []) if not skill_reg.exists(s)]
        if missing_skills:
            issues.append(
                f"Recommended skills do not exist: {', '.join(missing_skills)}. "
                "Drop them, or use search_skills to find real ones."
            )
        disabled_skills = [
            s for s in (report.recommended_skills or []) if skill_reg.exists(s) and skill_reg.is_disabled(s)
        ]
        if disabled_skills:
            issues.append(
                f"Recommended skills are disabled: {', '.join(disabled_skills)}. "
                "Drop them — the user has toggled them off in Explorer > Skills."
            )
        invalid_skills = [
            s
            for s in (report.recommended_skills or [])
            if skill_reg.exists(s) and not skill_reg.is_disabled(s) and not skill_reg.is_valid(s)
        ]
        if invalid_skills:
            issues.append(
                f"Recommended skills failed pre-flight validation (broken scripts): "
                f"{', '.join(invalid_skills)}. Drop them or recommend alternatives."
            )
    except Exception as e:
        logger.debug("Scout validator: skill registry check failed: %s", e)

    # 3. Recommended model, if set, must be known.
    if report.recommended_model and _known_model_ids:
        if report.recommended_model not in _known_model_ids:
            issues.append(
                f"recommended_model '{report.recommended_model}' is not in AVAILABLE MODELS. "
                "Use an exact model id from the list, or leave empty to use the default."
            )

    return issues


def _self_check_report(report: ScoutReport) -> list[str]:
    """Check a ScoutReport for correctable issues. Returns list of issue strings.

    Empty list = valid. Each issue string is suitable for injecting into the
    scout's context as a revision request. Distinct from the sanitizing
    `_validate_report` below, which mutates the report to enforce invariants
    after scout completes. This runs during the scout loop for self-revision.
    """
    return _unfixable_issues(report) + _sanitizable_issues(report)


def _format_revision_request(issues: list[str]) -> str:
    """Format validator issues as a scout-facing revision request."""
    bullets = "\n".join(f"- {issue}" for issue in issues)
    return (
        "[SCOUT SELF-CHECK — REVISION REQUESTED]\n"
        "Your submitted report has the following fixable issues:\n"
        f"{bullets}\n\n"
        "RESPONSE FORMAT: Call submit_report again with corrected values. "
        "Do NOT reply with prose explaining what you'll do — call the tool. "
        "Revision rounds are limited — if you cannot resolve an issue, explain "
        "why in tool_rationale/skill_rationale so the main agent is informed."
    )


def _format_post_mortem_hits(hits: list[dict]) -> str:
    """Format post-mortem search results as a scout-facing summary.

    Never returns raw payload_json — only structured fields plus a short
    reasoning excerpt. Each entry capped at ~300 chars; whole output capped.
    """
    if not hits:
        return "No matching post-mortems."

    import json as _json
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    lines: list[str] = []
    now = _dt.now(_tz.utc)
    for h in hits:
        # Parse payload safely; extract a short reasoning excerpt only.
        reasoning = ""
        try:
            payload = _json.loads(h.get("payload_json") or "{}")
            reasoning = str(payload.get("reasoning", ""))[:200]
        except (ValueError, TypeError):
            pass
        # Humanize created_at into "Nd ago" / "Nh ago".
        age = ""
        try:
            created = _dt.fromisoformat(str(h.get("created_at", "")).replace("Z", "+00:00"))
            delta = now - created
            if delta.days >= 1:
                age = f"{delta.days}d ago"
            else:
                age = f"{delta.seconds // 3600}h ago"
        except (ValueError, TypeError):
            age = "unknown"
        session = (h.get("session_id") or "")[:8]
        line = (
            f"[session {session}, verdict={h.get('verdict','?')}, "
            f"cause={h.get('failure_cause','?')}, attempt={h.get('attempt','?')}, "
            f"{age}] {reasoning}"
        )
        lines.append(line[:300])

    out = "\n".join(lines)
    return out[:3000]


# ---------------------------------------------------------------------------
# Core minimum tools (always available to the main agent)
# ---------------------------------------------------------------------------

# All builtin tools — always included regardless of scout recommendation.
# Extension tools (orchestration, web, vcs, etc.) are still scout-curated.
CORE_MINIMUM = frozenset(
    {
        "file_read",
        "file_write",
        "file_edit",
        "multiedit",
        "glob",
        "grep",
        "bash",
        "remember",
        "recall",
        "deep_recall",
        "ingest",
        "ask_user",
        "notify_user",
        "discover_tools",
        "get_tool_schema",
        "discover_skills",
        "load_skill",
        "read_skill_resource",
    }
)

# Cache
import threading as _threading

_cache: dict[str, tuple[ScoutReport, float]] = {}
_cache_lock = _threading.Lock()
CACHE_TTL = 300  # 5 minutes

# Known model IDs (populated during scout LLM run for validation)
_known_model_ids: set[str] = set()


async def build_model_catalog_block() -> str | None:
    """The AVAILABLE MODELS block scout puts in its prompt, or None.

    Reads the registry catalog rather than listing models live. Every field
    rendered here — id, provider, context_length, vision — is already in the
    registry, populated at startup and repopulated whenever an unknown model
    turns up; the live call meant `/api/tags` plus an `/api/show` per
    uncached model on every scout run (4.7s of an 18.3s scout when warm, up
    to 20s cold), and it was the slowest gatherer, so it set the floor for
    the whole gather phase. The live listing stays as the fallback for a
    registry that never populated.
    """
    global _known_model_ids
    try:
        from core.llm.client import get_llm_client

        llm_client = get_llm_client()
        registry = llm_client.router.registry
        models = registry.all_models() if registry.populated else []
        if not models:
            models = await asyncio.wait_for(llm_client.list_models(), timeout=8)
        if not models:
            return None
        models = sorted(models, key=lambda m: (m.provider, m.id))
        _known_model_ids = {m.id for m in models}
        lines = ["", f"AVAILABLE MODELS (current: {settings.llm_model}):"]
        for m in models:
            caps = " [vision]" if m.supports_vision else ""
            lines.append(f"- {m.id} ({m.provider}, ctx={m.context_length:,}{caps})")
        return "\n".join(lines)
    except Exception as e:
        logger.debug("Scout model listing failed: %s", e)
        return None


# Cap on the auto-injected skill body, in CHARACTERS (~1.25k tokens, not the
# ~5k an earlier comment here claimed). Anything longer is cut with a marker
# pointing at load_skill.
SKILL_INJECT_MAX_CHARS = 5000

# Max tool rounds for the scout's internal loop.
# IMPORTANT: keep the round counts in SCOUT_SYSTEM_PROMPT (line ~104) in sync
# with this constant — the prompt is hardcoded and will drift if this changes.
SCOUT_MAX_ROUNDS = 6
# Max self-check revisions scout can request on a single run.
# Extra slot lets scout fix multiple unrelated issues sequentially
# (e.g. a contradictory signal AND an unknown model in the same submit).
_MAX_REVISIONS = 2


# ---------------------------------------------------------------------------
# Build session brief
# ---------------------------------------------------------------------------


def build_session_brief(session_id: str, context_budget: int | None = None) -> SessionBrief:
    """Build a SessionBrief deterministically from DB state.

    `context_budget` overrides `settings.context_budget` when computing
    `context_utilization`. Pass the effective per-session value
    (session.context_budget_override or global default) so the brief
    reflects the budget the session actually runs under.
    """
    session = db.get_session(session_id)
    if not session:
        return SessionBrief(session_id=session_id)

    messages = db.get_messages(session_id)

    # Legacy rows may hold base64 image data inlined as a JSON list. Collapse
    # them to plain text markers before we compute previews or token totals,
    # otherwise a single attachment can blow up context_utilization to >1000%.
    from core.context.compiler import _legacy_multimodal_to_text

    def _msg_text(m: dict) -> str:
        return _legacy_multimodal_to_text(m.get("content") or "")

    # Recent messages: last 3, role + first 200 chars
    recent = []
    for m in messages[-3:]:
        content = _msg_text(m)[:200].replace("\n", " ")
        recent.append(f"{m['role']}: {content}")

    # Tools used recently (from last 3 tool-call rounds)
    tools_used = set()
    for m in messages[-20:]:
        if m["role"] == "assistant" and m.get("tool_calls"):
            try:
                tcs = json.loads(m["tool_calls"]) if isinstance(m["tool_calls"], str) else m["tool_calls"]
                for tc in (tcs if isinstance(tcs, list) else []):
                    if isinstance(tc, dict):
                        tools_used.add(tc.get("function", {}).get("name", tc.get("name", "")))
            except (json.JSONDecodeError, TypeError):
                pass

    # Compaction summary
    compaction_summary = None
    for m in reversed(messages):
        if m["role"] == "compaction":
            compaction_summary = (m.get("content") or "")[:500]
            break

    # Context utilization estimate. Use the text-only view so legacy rows
    # with inlined base64 don't produce bogus 1000%+ utilization figures —
    # those images are stripped at compile time anyway (except for the
    # latest turn on vision models, which is counted separately upstream).
    from core.context.tokens import get_estimator

    estimator = get_estimator()

    def _effective_tokens(m: dict) -> int:
        cached = m.get("token_count")
        if cached is not None:
            return int(cached)
        # Approximate: message overhead + text tokens. count_message would
        # re-tokenize raw content (legacy base64), which is what we want
        # to avoid here.
        return 4 + estimator.count(_msg_text(m))

    total_tokens = sum(_effective_tokens(m) for m in messages)
    effective_budget = context_budget if context_budget is not None else settings.context_budget
    utilization = total_tokens / max(effective_budget, 1)

    # Count user turns
    turn_count = sum(1 for m in messages if m["role"] == "user")

    return SessionBrief(
        session_id=session_id,
        title=session.get("title", "New session"),
        turn_count=turn_count,
        session_type=session.get("session_type", "normal"),
        compaction_summary=compaction_summary,
        recent_messages=recent,
        tools_used_recently=sorted(tools_used),
        context_utilization=min(utilization, 1.0),
        is_fresh=(turn_count == 0),
    )


# ---------------------------------------------------------------------------
def _build_lessons_section(message: str) -> str:
    """Format relevant lessons (entry_type='lesson') for scout injection.

    Lessons are operational workarounds extracted by snooze_reflect from past
    failed sessions. We surface up to 5 high-relevance matches; if none match
    the current request, the section is omitted entirely (no empty header).
    """
    if not message or not message.strip():
        return ""
    try:
        from core.memory.store import get_memory_store

        store = get_memory_store()
        if not store:
            return ""
        lessons = store.search_lessons(message, limit=5, _track_hits=False)
    except Exception as e:
        logger.debug("Scout lessons lookup failed: %s", e)
        return ""

    if not lessons:
        return ""

    import time as _time

    now_ts = int(_time.time())
    parts = [
        "",
        "RELEVANT PAST LESSONS (operational workarounds from prior sessions — "
        "apply when the situation matches; codebase moves quickly so verify "
        "the lesson still describes current behavior before acting on it):",
    ]
    for r in lessons:
        snippet = (r.entry.content or "").replace("\n", " ").strip()
        if len(snippet) > 400:
            snippet = snippet[:400] + "…"
        # Surface age so the agent can weigh lessons against recent code
        # changes. A "manifest bug" lesson 2d old is suspect if the run
        # engine was rewritten yesterday — let the agent see the freshness.
        age_days = max(0, (now_ts - int(r.entry.epoch or now_ts)) // 86400)
        age_str = f"{age_days}d ago" if age_days > 0 else "today"
        parts.append(f"- [{r.entry.file_name}, {age_str}] {snippet}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Scout bypass logic
# ---------------------------------------------------------------------------


# Conversational openers that signal a follow-up/acknowledgement rather than
# a new task. Anchored at the start; combined with a word cap and a no-URL
# check below so genuine short tasks ("delete all my cron jobs") still scout.
_CONVERSATIONAL_RE = re.compile(
    r"^(yes|yeah|yep|no|nope|ok(ay)?|sure|thanks?|thank you|got it|sounds good|"
    r"go ahead|continue|proceed|do it|please do|why|how come|what about|and then|"
    r"nice|great|cool|perfect)\b[,.!\s]*",
    re.IGNORECASE,
)


def should_bypass_scout(message: str, turn_count: int) -> bool:
    """Determine if scout should be skipped for trivial interactions."""
    if not settings.scout_enabled:
        return True
    words = len(message.split())
    # Short follow-ups in active sessions
    if words <= 3 and turn_count > 1:
        return True
    # Conversational confirmations/follow-ups ("yes please go ahead and do
    # that", "ok sounds good, continue") — the prior turn's scout already
    # mapped the task, and these paid the full multi-second scout for no new
    # information. Gated on an opener match, a word cap, and no URLs so a
    # short NEW task ("ok now fetch https://...") still gets scouted.
    if turn_count > 1 and words <= 12 and "://" not in message and _CONVERSATIONAL_RE.match(message.strip()):
        return True
    # Slash commands
    if message.startswith("/"):
        return True
    # Context reset resumption
    if message.startswith("[Context was reset"):
        return True
    return False


# ---------------------------------------------------------------------------
# Scout cache
# ---------------------------------------------------------------------------


def _cache_key(message: str, brief: SessionBrief) -> str:
    # Deliberately coarse. The old key included turn_count, the exact
    # recently-used-tools list, and utilization at 0.1 granularity — all of
    # which change between consecutive turns, so the cache could only hit
    # when the same message was re-sent in the same turn state (essentially
    # never; the cache was dead weight). Within the 5-minute TTL a report
    # for the same message in the same session/phase at a similar context
    # fill is still valid guidance.
    util_bucket = int(brief.context_utilization * 4)  # 25% buckets
    raw = f"{message}:{brief.session_id}:{brief.phase}:{util_bucket}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _get_cached(message: str, brief: SessionBrief) -> ScoutReport | None:
    key = _cache_key(message, brief)
    with _cache_lock:
        entry = _cache.get(key)
        if entry and time.time() < entry[1]:
            report = entry[0]
            report.from_cache = True
            return report
    return None


MAX_CACHE_SIZE = 500


def _put_cache(message: str, brief: SessionBrief, report: ScoutReport) -> None:
    with _cache_lock:
        now = time.time()
        # Evict expired entries when cache is getting large
        if len(_cache) > MAX_CACHE_SIZE // 2:
            expired = [k for k, (_, ttl) in _cache.items() if now >= ttl]
            for k in expired:
                del _cache[k]
        # Hard cap: evict oldest entries if still over limit
        if len(_cache) >= MAX_CACHE_SIZE:
            oldest = sorted(_cache.items(), key=lambda x: x[1][1])
            for k, _ in oldest[: len(_cache) - MAX_CACHE_SIZE + 1]:
                del _cache[k]
        key = _cache_key(message, brief)
        _cache[key] = (report, now + CACHE_TTL)


# ---------------------------------------------------------------------------
# Scout execution
# ---------------------------------------------------------------------------


async def run_scout(
    session_id: str,
    message: str,
    session_brief: SessionBrief | None = None,
    emit: Callable[[dict], None] | None = None,
    is_retry: bool = False,
) -> ScoutReport:
    """Run the scout agent. Returns ScoutReport (from cache, LLM, or fallback).

    The scout:
    1. Reads SOUL.md, RULES.md, SESSIONS.md (if they exist)
    2. Iteratively searches memory, tools, skills via tool calls
    3. Submits a curated ScoutReport via submit_report tool

    is_retry: this turn repeats one that failed verification. Cache reads are
    skipped — the key is coarse (message + session + phase + utilization
    bucket) with a 5-minute TTL, so a reflect retry of the same user message
    would otherwise be handed back the very plan that just failed. Writes are
    unaffected: the fresh plan is still worth caching.
    """
    brief = session_brief or build_session_brief(session_id)

    # Check bypass
    if should_bypass_scout(message, brief.turn_count):
        cached = None if is_retry else _get_cached(message, brief)
        if cached:
            logger.debug("Scout bypassed, using cached report for session %s", session_id)
            return cached
        logger.debug("Scout bypassed, using fallback for session %s", session_id)
        return _build_fallback_report(message, brief, reason="bypass")

    # Check cache
    cached = None if is_retry else _get_cached(message, brief)
    if cached:
        logger.debug("Scout cache hit for session %s", session_id)
        return cached

    # Run scout LLM with retry for transient errors (model loading, 500s, etc.)
    max_attempts = 3
    last_error: Exception | None = None
    empty_approach_retried = False  # one-shot guard for structural-empty retry
    degraded_report: ScoutReport | None = None  # best fallback seen so far

    def _is_empty_approach(rep: ScoutReport) -> bool:
        """True when scout returned a structurally valid report with no
        actionable guidance — the LLM gave up rather than the task needing none.
        """
        # Minor: this_time pure Q&A, scout may legitimately return brief approach.
        # Only treat *completely empty* as a failure signal.
        return not (getattr(rep, "approach_guidance", "") or "").strip()

    # Use the session's own scheduling priority so concurrent worker scouts
    # are not starved by lower-priority scouts from sibling workers.
    sched_priority = PRIORITY_WORKER if brief.session_type == "worker" else PRIORITY_ORCHESTRATOR
    try:
        from datetime import datetime as _dt

        sched_created_at = _dt.fromisoformat(
            db.get_session(session_id).get("created_at", "").replace("Z", "+00:00")
        ).timestamp()
    except Exception:
        sched_created_at = float("inf")

    for attempt in range(1, max_attempts + 1):
        start = time.monotonic()
        try:
            report = await asyncio.wait_for(
                _run_scout_llm(
                    message,
                    brief,
                    emit=emit,
                    session_id=session_id,
                    session_created_at=sched_created_at,
                    session_priority=sched_priority,
                ),
                timeout=settings.scout_timeout,
            )
            report.scout_latency_ms = int((time.monotonic() - start) * 1000)
            report = _validate_report(report)

            # Empty-approach retry: the primary model returned a parseable but
            # uselessly empty plan, or bottomed out into the deterministic
            # fallback. Give it one more shot before falling through to the
            # dedicated fallback model. Skipped on cached bypass turns
            # (handled above) and when disabled by setting.
            if (
                not empty_approach_retried
                and getattr(settings, "scout_retry_on_empty_approach", True)
                and (_is_empty_approach(report) or report.from_fallback)
                and attempt < max_attempts
            ):
                empty_approach_retried = True
                logger.info(
                    "Scout returned no usable plan for session %s — retrying primary model (attempt %d)",
                    session_id,
                    attempt + 1,
                )
                await asyncio.sleep(1)
                continue

            # A fallback report is a degraded artifact, not a scout result:
            # keep it as the floor but let the dedicated fallback model try
            # for a real plan first. Breaking out (rather than returning) also
            # keeps it out of the cache, so the next turn re-scouts instead of
            # inheriting a scout-less plan for the full CACHE_TTL.
            if report.from_fallback:
                logger.warning(
                    "Scout produced only a fallback report for session %s after %d attempt(s)",
                    session_id,
                    attempt,
                )
                degraded_report = report
                break

            _put_cache(message, brief, report)
            if attempt > 1:
                logger.info("Scout succeeded on attempt %d for session %s", attempt, session_id)
            logger.info(
                "Scout completed for session %s in %dms (tools: %s)",
                session_id,
                report.scout_latency_ms,
                ", ".join(report.recommended_tools[:5]),
            )
            return report

        except asyncio.TimeoutError:
            logger.warning(
                "Scout timed out after %ds for session %s (attempt %d/%d) — skipping remaining primary retries",
                settings.scout_timeout,
                session_id,
                attempt,
                max_attempts,
            )
            last_error = TimeoutError(f"Scout timed out after {settings.scout_timeout}s")
            # A full-budget wall-clock timeout means the model isn't going to
            # respond — retrying the same hung endpoint just burns another
            # scout_timeout per attempt. Escalate straight to the fallback model.
            break
        except Exception as e:
            last_error = e
            elapsed_ms = int((time.monotonic() - start) * 1000)
            _log_scout_error(e, session_id, attempt, max_attempts, elapsed_ms)
            if attempt < max_attempts and _is_retryable(e):
                wait = _retry_wait_seconds(e, attempt)
                logger.info(
                    "Scout retrying in %ds for session %s (attempt %d/%d)", wait, session_id, attempt + 1, max_attempts
                )
                await asyncio.sleep(wait)
                continue
            break

    # Fallback model — one last try on the unified settings.fallback_model
    # (Settings → Models → Fallback Model) before returning the deterministic
    # stub. Same knob as the main agent's rate-limit failover so operators
    # configure it in one place.
    fallback_model = (settings.fallback_model or "").strip()
    primary_model = (settings.background_model or "").strip()
    if fallback_model and fallback_model != primary_model:
        logger.info(
            "Scout exhausted primary attempts for session %s — trying " "fallback_model=%s (last error: %s)",
            session_id,
            fallback_model,
            last_error,
        )
        start = time.monotonic()
        try:
            report = await asyncio.wait_for(
                _run_scout_llm(
                    message,
                    brief,
                    emit=emit,
                    model_override=fallback_model,
                    session_id=session_id,
                    session_created_at=sched_created_at,
                    session_priority=sched_priority,
                ),
                timeout=settings.scout_timeout,
            )
            report.scout_latency_ms = int((time.monotonic() - start) * 1000)
            report = _validate_report(report)
            if report.from_fallback:
                # Fallback model bottomed out too — keep its deterministic
                # report and stop pretending scout ran.
                logger.warning("Scout fallback model produced no usable plan for session %s", session_id)
                degraded_report = report
            else:
                _put_cache(message, brief, report)
                logger.info(
                    "Scout fallback model succeeded for session %s in %dms", session_id, report.scout_latency_ms
                )
                return report
        except Exception as e:
            logger.warning("Scout fallback model also failed for session %s: %r", session_id, e)

    logger.warning(
        "Scout exhausted all attempts for session %s, using fallback (%s)",
        session_id,
        f"last error: {last_error}" if last_error else "no model produced a usable plan",
    )
    # Reuse the fallback we already built (it carries scout_model/latency and
    # any partial context) rather than rebuilding an identical one.
    return degraded_report if degraded_report is not None else _build_fallback_report(message, brief)


async def _run_scout_llm(
    message: str,
    brief: SessionBrief,
    emit: Callable[[dict], None] | None = None,
    model_override: str = "",
    session_id: str = "",
    session_created_at: float = float("inf"),
    session_priority: int = PRIORITY_BACKGROUND,
) -> ScoutReport:
    """Execute the scout as a multi-turn tool-calling agent.

    Pre-loads deterministic context (files, models), then enters a tool loop
    where the scout LLM can iteratively search memory, tools, skills, and
    sessions before submitting its final report via submit_report.
    """
    from core.llm.client import get_llm_client

    def _step(step: str, detail: str = ""):
        if emit:
            emit({"type": "scout.step", "step": step, "detail": detail})

    # --- Phase 1: Pre-load deterministic context (fast, no LLM) ---
    # Legacy rows could pass a JSON-serialized multimodal list in `message`.
    # Scout must see plain text only — vision payloads go directly to the
    # main-model compile path, never here.
    from core.context.compiler import _legacy_multimodal_to_text

    scout_message = _legacy_multimodal_to_text(message) if isinstance(message, str) else str(message)

    from datetime import datetime as _dt
    from datetime import timezone as _tz

    _now_utc = _dt.now(_tz.utc)
    _now_local = _now_utc.astimezone()
    _now_str = f"{_now_utc.strftime('%Y-%m-%d %H:%M UTC')}" f" / {_now_local.strftime('%Y-%m-%d %H:%M %Z')} (local)"

    user_content_parts = [
        f"USER MESSAGE: {scout_message}",
        "",
        f"CURRENT DATE/TIME: {_now_str}",
        "",
        f"SESSION CONTEXT:\n{brief.to_prompt_text()}",
    ]

    # Read instruction files
    _step("reading", "Loading identity & rules")
    for filename, label in [
        ("data/agent/SOUL.md", "SOUL.md contents"),
        ("data/agent/RULES.md", "RULES.md contents"),
    ]:
        path = Path(filename)
        if path.exists():
            content = path.read_text()[:12000]
            user_content_parts.append(f"\n{label}:\n{content}")

    # Check data/agent/ for SESSIONS.md / INSTRUCTIONS.md
    _step("reading", "Checking project instructions")
    agent_dir = Path("data/agent")
    for fname in ["SESSIONS.md", "INSTRUCTIONS.md"]:
        agent_path = agent_dir / fname
        if agent_path.exists():
            content = agent_path.read_text()[:12000]
            user_content_parts.append(f"\nProject instructions ({fname}):\n{content}")
            break

    # --- Baseline gathering -------------------------------------------------
    # Six independent searches (memory, deep memory, cross-session FTS, tool
    # discovery, skill discovery, lessons) plus the provider model listing.
    # These used to run sequentially on the event loop — together they were
    # the bulk of scout's pre-LLM latency and a large share of every prompt's
    # time-to-first-token. They are independent reads, so run them
    # concurrently on threads (emit_event is thread-safe) and append results
    # in a fixed order to keep the prompt deterministic.
    _mem_cap = int(getattr(settings, "scout_preload_memory_char_limit", 300) or 300)

    def _gather_memory_baseline() -> str | None:
        _step("memory", "Searching memory")
        try:
            from core.memory.store import get_memory_store

            store = get_memory_store()
            if not store:
                return None
            results = store.search(message, limit=10, _track_hits=False)
            if results:
                _step("memory", f"Found {len(results)} relevant memories")
                mem_lines = ["", "MEMORY SEARCH RESULTS (use search_memory tool for deeper/different queries):"]
                # Per-item cap keeps the pre-load bundle from ballooning as the
                # memory index grows across sessions. Configurable because small
                # scout models (e.g. 35B Ollama) degrade under high input ctx.
                for r in results:
                    mem_lines.append(
                        f"[{r.entry.file_name} score={r.score:.1f} type={r.entry.entry_type}] "
                        f"{r.entry.content[:_mem_cap]}"
                    )
                return "\n".join(mem_lines)
            # Explicit 0-result signal — tells the scout LLM to call search_memory
            # with keyword variants rather than assuming memory is empty.
            return (
                "\nMEMORY BASELINE: 0 results for this query. "
                "Call search_memory with decomposed keywords or @tags: queries before "
                "concluding memory is empty — FTS5 requires matching terms."
            )
        except Exception as e:
            logger.debug("Scout memory search failed: %s", e)
            return None

    def _gather_deep_memory() -> str | None:
        try:
            from core.scout.search import gather_deep_memory

            deep_mem = gather_deep_memory(message, char_cap=_mem_cap)
            if deep_mem:
                _step("memory", "Deep search found additional results")
                return f"\nDEEP MEMORY SEARCH:\n{deep_mem}"
        except Exception as e:
            logger.debug("Scout deep memory search failed: %s", e)
        return None

    def _gather_cross_session() -> str | None:
        _step("sessions", "Searching other sessions")
        try:
            from core.scout.search import gather_cross_session_data

            cross = gather_cross_session_data(message, brief.session_id)
            if cross:
                _step("sessions", "Found relevant data in other sessions")
                return f"\n{cross}"
        except Exception as e:
            logger.debug("Scout cross-session search failed: %s", e)
        return None

    def _gather_tool_discovery() -> str | None:
        _step("tools", "Discovering relevant tools")
        try:
            from core.tools.registry import get_registry

            registry = get_registry()
            discovered = registry.discover(message, limit=15)
            if discovered:
                _step("tools", f"Found {len(discovered)} candidate tools")
                tool_lines = ["", "AVAILABLE TOOLS (from discovery search):"]
                for t in discovered:
                    tool_lines.append(f"- {t.name} [{t.category}]: {t.description}")
                return "\n".join(tool_lines)
        except Exception as e:
            logger.debug("Scout tool discovery failed: %s", e)
        return None

    def _gather_skill_discovery() -> str | None:
        _step("skills", "Discovering relevant skills")
        try:
            from core.skills.registry import get_skill_registry

            skill_reg = get_skill_registry()
            discovered_skills = skill_reg.discover(message, limit=5)
            if discovered_skills:
                _step("skills", f"Found {len(discovered_skills)} candidate skills")
                skill_lines = ["", "AVAILABLE SKILLS (domain expertise packages — recommend when task matches):"]
                for s in discovered_skills:
                    tags_str = f" [{', '.join(s.tags[:3])}]" if s.tags else ""
                    extras = []
                    if s.has_scripts:
                        extras.append("has scripts")
                    if s.has_references:
                        extras.append("has references")
                    extra_str = f" ({', '.join(extras)})" if extras else ""
                    skill_lines.append(f"- {s.name}{tags_str}: {s.description}{extra_str}")
                return "\n".join(skill_lines)
        except Exception as e:
            logger.debug("Scout skill discovery failed: %s", e)
        return None

    def _gather_lessons() -> str | None:
        # Relevant past lessons (entry_type='lesson') — workarounds extracted
        # by snooze_reflect from prior failed sessions, via hybrid search.
        try:
            lessons_section = _build_lessons_section(message)
            if lessons_section:
                _step("lessons", "Injecting relevant past lessons")
                return lessons_section
        except Exception as e:
            logger.debug("Scout lessons injection failed: %s", e)
        return None

    async def _gather_models() -> str | None:
        _step("models", "Listing available models")
        return await build_model_catalog_block()

    async def _gather_candor_intel() -> str | None:
        # Calibrated operational intel (Candor add-on): degraded tools,
        # admitted conditions, open questions. The bridge serializes store
        # access on its own thread — awaiting here never blocks the loop.
        # First call after boot may hit the lazy ledger fold; the timeout
        # falls back to the last cached brief instead of stalling scout.
        if not (settings.candor_enabled and settings.candor_scout_brief):
            return None
        try:
            from core.extensions.candor.bridge import get_candor_bridge

            bridge = get_candor_bridge()
            try:
                brief = await asyncio.wait_for(bridge.intel_brief(), timeout=4)
            except asyncio.TimeoutError:
                brief = bridge.cached_brief()
            if brief:
                _step("candor", "Injecting operational reliability intel")
            return brief
        except Exception as e:
            logger.debug("Scout candor intel failed: %s", e)
            return None

    def _gather_model_routing_intel() -> str | None:
        # H2 (plan §12.4): learned (model, task-category) verdict rates as
        # an exception brief steering recommended_model. Pure SQLite read.
        try:
            from core.synthesis import build_model_routing_brief

            return build_model_routing_brief()
        except Exception as e:
            logger.debug("Scout model-routing intel failed: %s", e)
            return None

    def _gather_adaptive_hints() -> str | None:
        # Adaptive routing hints (plan 4e): learned tool/skill selection
        # guidance renders ONLY here, beside [OPERATIONAL INTEL] — planning
        # signal for scout, never agent-prompt weight (I5).
        if not settings.adaptive_enabled:
            return None
        try:
            from core.adaptive.render import build_routing_hints_block

            return build_routing_hints_block() or None
        except Exception as e:
            logger.debug("Scout adaptive hints failed: %s", e)
            return None

    gathered = await asyncio.gather(
        asyncio.to_thread(_gather_memory_baseline),
        asyncio.to_thread(_gather_deep_memory),
        asyncio.to_thread(_gather_cross_session),
        asyncio.to_thread(_gather_tool_discovery),
        asyncio.to_thread(_gather_skill_discovery),
        asyncio.to_thread(_gather_lessons),
        _gather_models(),
        _gather_candor_intel(),
        asyncio.to_thread(_gather_adaptive_hints),
        asyncio.to_thread(_gather_model_routing_intel),
    )
    user_content_parts.extend(part for part in gathered if part)

    user_content = "\n".join(user_content_parts)

    # --- Phase 2: Multi-turn tool-calling loop ---
    _step("thinking", "Scout analyzing context")
    client = get_llm_client()
    model = model_override or settings.background_model or settings.llm_model

    if not client.has_capacity(model):
        _step("waiting", "Waiting for LLM capacity")

    messages = [
        {"role": "system", "content": _scout_system_prompt()},
        {"role": "user", "content": user_content},
    ]

    total_usage = TokenUsage()
    report = None
    # Scout self-validation state (Phase 2a).
    # When scout submits a report that fails _self_check_report, we inject
    # a revision request and let scout submit once more. Hard-capped at 1.
    revisions_used = 0

    for round_num in range(SCOUT_MAX_ROUNDS):
        is_last_round = round_num == SCOUT_MAX_ROUNDS - 1
        # On the last round, drop the search tools so scout can't keep digging —
        # but keep submit_report. Removing every tool made the final round
        # unwinnable: the round before it tells scout it MUST submit, and a
        # revision request lands there by construction, so scout was ordered to
        # call a tool that was no longer on offer. Text output is still parsed
        # as a fallback below for models that answer in prose anyway.
        tools = _SCOUT_TOOLS if not is_last_round else _SCOUT_SUBMIT_ONLY

        # On penultimate round, inject a reminder to submit next round
        if round_num == SCOUT_MAX_ROUNDS - 2 and report is None:
            messages.append(
                {
                    "role": "user",
                    "content": "[SYSTEM] You have 1 tool round remaining. You MUST call submit_report on your next response. Summarize your findings and submit now.",
                }
            )

        response = await client.chat(
            messages=messages,
            model=model,
            max_tokens=4096,
            tools=tools,
            session_id=session_id,
            session_created_at=session_created_at,
            session_priority=session_priority,
        )

        # Accumulate token usage
        total_usage.prompt_tokens += response.usage.prompt_tokens
        total_usage.completion_tokens += response.usage.completion_tokens
        total_usage.total_tokens += response.usage.total_tokens

        # No tool calls — LLM is done (or forced text on last round).
        # Special case: if we JUST sent a revision request, the model
        # sometimes responds with prose ("I'll fix the model ID...") instead
        # of calling submit_report again. That is a recoverable model
        # confusion — append a stricter format reminder and retry the round
        # rather than restarting the entire scout from scratch (the previous
        # behavior, which burned a whole fresh attempt — up to settings.
        # scout_timeout of wall clock — per occurrence on real runs).
        if not response.tool_calls:
            just_sent_revision = (
                revisions_used > 0
                and messages
                and messages[-1].get("role") == "tool"
                and "[SCOUT SELF-CHECK — REVISION REQUESTED]" in (messages[-1].get("content") or "")
            )
            if just_sent_revision and response.content and not is_last_round:
                # Record what the model said so we can show the loop is
                # converging (or not), then ask once more in stricter form.
                logger.info(
                    "Scout returned prose after revision request; nudging "
                    "with stricter format reminder. Prose excerpt: %r",
                    response.content[:200],
                )
                messages.append({"role": "assistant", "content": response.content})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "[FORMAT ERROR] Your last response was prose. "
                            "You must call the submit_report tool — explanations "
                            "in plain text cannot be parsed. Apply your fix and "
                            "call submit_report now."
                        ),
                    }
                )
                continue  # next loop iteration will re-prompt
            if response.content:
                # Try to parse as JSON report (graceful fallback)
                report = _parse_scout_response(response.content)
            break

        # On last round, native tool-calling models (e.g. Qwen3) may still
        # emit tool_calls even with tools=None. Only honor submit_report;
        # for anything else, fall through to text parsing.
        if is_last_round:
            for tc in response.tool_calls:
                if tc.name == "submit_report":
                    try:
                        args = json.loads(tc.arguments) if tc.arguments else {}
                    except json.JSONDecodeError:
                        args = {}
                    _step("done", "Scout submitting report (last round)")
                    report = _extract_report(args)
                    break
            if report is None and response.content:
                logger.info("Scout emitted tool_calls on last round without submit_report, trying text parse")
                report = _parse_scout_response(response.content)
            break

        # Process tool calls
        # Build assistant message with tool_calls for conversation history
        assistant_msg: dict = {"role": "assistant", "content": response.content or ""}
        assistant_msg["tool_calls"] = [
            {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": tc.arguments}}
            for tc in response.tool_calls
        ]
        messages.append(assistant_msg)

        for tc in response.tool_calls:
            try:
                args = json.loads(tc.arguments) if tc.arguments else {}
            except json.JSONDecodeError:
                args = {}

            if tc.name == "submit_report":
                # Candidate report — run Phase 2a self-validation before accepting.
                candidate = _extract_report(args)
                issues = _self_check_report(candidate)

                # No issues → accept and stop.
                if not issues:
                    _step("done", "Scout submitting report")
                    candidate.viability = "verified"
                    report = candidate
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": "Report submitted."})
                    break

                # Issues found. Only spend a round on the ones the scout alone
                # can fix — `_validate_report` strips hallucinated tools, skills
                # and models regardless, so revising for those trades a usable
                # report for a round the scout may not survive. In the logged
                # failures every revision was triggered by a name the sanitizer
                # would have dropped, and none ever produced a second submit.
                blocking = _unfixable_issues(candidate)
                rounds_remaining = SCOUT_MAX_ROUNDS - round_num - 1
                if blocking and revisions_used < _MAX_REVISIONS and rounds_remaining >= 1:
                    _step("revising", f"Scout self-check flagged {len(blocking)} blocking issue(s)")
                    revisions_used += 1
                    messages.append(
                        {"role": "tool", "tool_call_id": tc.id, "content": _format_revision_request(blocking)}
                    )
                    logger.info("Scout revision requested: %s", blocking)
                    # Don't break the outer loop — let the next round execute.
                else:
                    _step("done", "Scout submitting report (unverified)")
                    candidate.viability = "unverified"
                    candidate.viability_notes = issues
                    report = candidate
                    logger.warning(
                        "Scout report accepted with unresolved issues: %s",
                        issues,
                    )
                    messages.append(
                        {"role": "tool", "tool_call_id": tc.id, "content": "Report submitted (with notes)."}
                    )
                    break
            else:
                # Execute read-only tool
                _step("tool", f"{tc.name}")
                result = _exec_scout_tool(tc.name, args, brief)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
                logger.debug("Scout tool %s returned %d chars", tc.name, len(result))

        if report is not None:
            break

    # If no report was produced — or the model produced a structurally empty
    # one — degrade to the deterministic fallback rather than an empty stub.
    # An empty stub is worse than no scout at all: it strips identity, rules,
    # instructions and memory from the system prompt, and narrows the tool
    # list to CORE_MINIMUM, which silently removes every extension tool the
    # agent needs (browse_web, search_web, orchestration, ...). The agent then
    # reinvents capabilities it already has. The deterministic fallback keeps
    # that context, so a degraded scout turn stays workable.
    if report is None:
        logger.warning("Scout did not submit report after %d rounds, using deterministic fallback", SCOUT_MAX_ROUNDS)
        report = _build_fallback_report(message, brief)
    elif _is_degenerate_report(report):
        logger.warning("Scout returned an empty report — replacing with deterministic fallback")
        report = _build_fallback_report(message, brief)

    # Best-effort validation for reports that skipped the in-loop path
    # (last-round native tool-call, text fallback, or fabricated empty report).
    if report.viability == "pending":
        issues = _self_check_report(report)
        if issues:
            report.viability = "unverified"
            report.viability_notes = issues
            logger.info("Scout post-loop report marked unverified: %s", issues)
        else:
            report.viability = "verified"

    report.scout_model = model
    report.scout_tokens = total_usage
    return report


def _parse_scout_response(text: str) -> ScoutReport:
    """Parse JSON response from scout LLM.

    Handles: raw JSON, markdown-fenced JSON, JSON embedded in thinking/reasoning text,
    malformed JSON with trailing commas, and partial JSON.
    """
    if not text or not text.strip():
        logger.warning("Scout returned empty response")
        return ScoutReport()

    text = text.strip()

    # Strip markdown fences
    if "```" in text:
        # Extract content between first and last fence
        parts = text.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{"):
                text = part
                break

    # Try direct parse
    data = _try_parse_json(text)

    # Try extracting JSON object from mixed content (thinking + JSON)
    if data is None:
        # Find the outermost { ... } block
        brace_start = text.find("{")
        if brace_start >= 0:
            # Find matching closing brace
            depth = 0
            brace_end = -1
            for i in range(brace_start, len(text)):
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        brace_end = i + 1
                        break
            if brace_end > brace_start:
                data = _try_parse_json(text[brace_start:brace_end])

    if data is None:
        logger.warning("Scout response not valid JSON (possible truncation): %s", text[:300])
        report = ScoutReport()
        report.from_fallback = True
        return report

    # identity/rules/instructions ignored — see _extract_report for rationale.
    return ScoutReport(
        memory_context=str(data.get("memory_context", "")),
        cross_session_context=str(data.get("cross_session_context", "")),
        recommended_tools=data.get("recommended_tools", []) if isinstance(data.get("recommended_tools"), list) else [],
        tool_rationale=str(data.get("tool_rationale", "")),
        recommended_skills=(
            data.get("recommended_skills", []) if isinstance(data.get("recommended_skills"), list) else []
        ),
        skill_rationale=str(data.get("skill_rationale", "")),
        recommended_model=str(data.get("recommended_model", "")),
        model_rationale=str(data.get("model_rationale", "")),
        session_state=str(data.get("session_state", "")),
        approach_guidance=str(data.get("approach_guidance", "")),
    )


def _try_parse_json(text: str) -> dict | None:
    """Try parsing JSON with tolerance for common LLM errors."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Fix trailing commas (common LLM error)
    cleaned = re.sub(r",\s*([\]}])", r"\1", text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Fix unquoted keys
    cleaned2 = re.sub(r"(\w+)\s*:", r'"\1":', text)
    try:
        return json.loads(cleaned2)
    except json.JSONDecodeError:
        pass

    return None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_report(report: ScoutReport) -> ScoutReport:
    """Validate and sanitize scout report.

    Strips tool/skill names that are unknown (hallucinations) AND that are
    disabled (user toggled them off). The two cases are logged separately so
    the operational distinction stays visible in the logs.
    """
    from core.tools.registry import get_registry

    registry = get_registry()

    # Strip tools that don't exist (hallucinations) or are disabled
    existing_tools = [t for t in report.recommended_tools if registry.exists(t)]
    hallucinated_tools = set(report.recommended_tools) - set(existing_tools)
    if hallucinated_tools:
        logger.warning("Scout hallucinated tools: %s", hallucinated_tools)
    valid_tools = [t for t in existing_tools if not registry.is_disabled(t)]
    disabled_tools = set(existing_tools) - set(valid_tools)
    if disabled_tools:
        logger.warning("Scout recommended disabled tools (stripped): %s", disabled_tools)

    # Ensure core minimum always present
    tool_set = set(valid_tools) | CORE_MINIMUM
    report.recommended_tools = sorted(tool_set)

    # Validate recommended skills — same two-case strip + log
    try:
        from core.skills.registry import get_skill_registry

        skill_reg = get_skill_registry()
        existing_skills = [s for s in report.recommended_skills if skill_reg.exists(s)]
        hallucinated_skills = set(report.recommended_skills) - set(existing_skills)
        if hallucinated_skills:
            logger.warning("Scout hallucinated skills: %s", hallucinated_skills)
        valid_skills = [s for s in existing_skills if not skill_reg.is_disabled(s)]
        disabled_skills = set(existing_skills) - set(valid_skills)
        if disabled_skills:
            logger.warning("Scout recommended disabled skills (stripped): %s", disabled_skills)
        report.recommended_skills = valid_skills

        # Hybrid injection: auto-load L2 for top-1 skill that passed pre-flight validation
        if valid_skills:
            top_skill = valid_skills[0]
            if not skill_reg.is_valid(top_skill):
                logger.warning(
                    "Skipping auto-injection of skill '%s' — failed pre-flight validation "
                    "(missing/empty/broken scripts). Agent can still load it manually.",
                    top_skill,
                )
                # Pre-flight failed — clear any stale injection from prior state
                # so a previously-set name/body doesn't leak through.
                report.injected_skill_name = ""
                report.injected_skill = ""
            else:
                report.injected_skill_name = top_skill
                instructions = skill_reg.load_instructions(top_skill)
                if instructions:
                    if len(instructions) > SKILL_INJECT_MAX_CHARS:
                        # The cut is silent to the agent otherwise: it reads a
                        # procedure that stops mid-step and follows it as if
                        # complete. Name the escape hatch at the cut point.
                        report.injected_skill = (
                            instructions[:SKILL_INJECT_MAX_CHARS]
                            + f"\n[skill truncated — call load_skill('{top_skill}') for the full procedure]"
                        )
                    else:
                        report.injected_skill = instructions
                    logger.info("Auto-injected skill '%s' (%d chars)", top_skill, len(report.injected_skill))
                else:
                    logger.warning(
                        "Failed to load L2 instructions for skill '%s' — will recommend by name only", top_skill
                    )
                    report.injected_skill_name = ""  # Clear — can't inject what we can't load
                    report.injected_skill = ""
        else:
            # No valid skills survived stripping (all hallucinated, all disabled,
            # or none were ever recommended). Clear any pre-set auto-injection
            # so a stale name/body from prior state doesn't leak into the
            # agent's system prompt.
            report.injected_skill_name = ""
            report.injected_skill = ""
    except Exception as e:
        logger.debug("Scout skill validation failed: %s", e)

    # Validate recommended model against known models
    if report.recommended_model:
        if _known_model_ids and report.recommended_model not in _known_model_ids:
            logger.warning("Scout recommended unknown model: %s", report.recommended_model)
            report.recommended_model = ""
            report.model_rationale = ""

    # Truncate oversized fields
    report.identity = report.identity[:1500]
    report.rules = report.rules[:1500]
    report.instructions = report.instructions[:1500]
    report.memory_context = report.memory_context[:2500]
    report.cross_session_context = report.cross_session_context[:2500]
    report.session_state = report.session_state[:1000]
    report.approach_guidance = report.approach_guidance[:2000]
    report.model_rationale = report.model_rationale[:500]

    return report


# ---------------------------------------------------------------------------
# Fallback (deterministic, no LLM)
# ---------------------------------------------------------------------------


def _build_fallback_report(message: str, brief: SessionBrief, *, reason: str = "degraded") -> ScoutReport:
    """Deterministic fallback when scout LLM fails or is bypassed.

    reason: "bypass" when scout was skipped on purpose (cheap turn), "degraded"
    when it ran and produced nothing usable. Only "degraded" warns the agent
    that its tool list is an uncurated default.

    Skills are intentionally excluded — skill discovery requires NLP matching
    which is too expensive for a synchronous fallback. The agent can still
    call discover_skills() / load_skill() mid-loop since they're in CORE_MINIMUM.

    SOUL.md/RULES.md/SESSIONS.md are intentionally NOT loaded here: the
    context compiler's fixed-prefix directives block delivers them whole on
    every turn, fallback or not — this report carries only what scout would
    have curated.
    """
    # Basic memory recall
    memory_context = ""
    try:
        from core.memory.store import get_memory_store

        store = get_memory_store()
        if store:
            memory_context = store.recall(message, top=3) or ""
    except Exception:
        pass

    # Default tools: core + recently used
    from core.tools.registry import get_registry

    registry = get_registry()
    tool_names = set(CORE_MINIMUM)
    # Recently-used names come from message history and can outlive the tool
    # (extension unloaded, tool renamed) — filter, or the self-check flags them
    # as hallucinations and the agent gets a spurious "unverified plan" notice.
    tool_names.update(t for t in brief.tools_used_recently if registry.exists(t) and not registry.is_disabled(t))
    # Add common tools. The web trio matters most here: CORE_MINIMUM is
    # builtins only, so without them a fallback turn has no way to reach a
    # page — and the agent tends to reinvent one (pip install playwright,
    # bootstrap a browser, crash) instead of using the browser Pernix ships.
    for name in [
        "remember",
        "recall",
        "glob",
        "grep",
        "ask_user",
        "call_model",
        "search_web",
        "http_get",
        "browse_web",
    ]:
        if registry.exists(name) and not registry.is_disabled(name):
            tool_names.add(name)

    approach = (
        "Scout unavailable — proceed conservatively. Use discover_tools / "
        "discover_skills before assuming tool surface. Verify file paths "
        "with glob/grep before file_write. Aim to write any deliverable early."
    )

    return ScoutReport(
        memory_context=memory_context,
        recommended_tools=sorted(tool_names),
        tool_rationale="Fallback: core tools + recently used (scout unavailable)",
        session_state=brief.to_prompt_text()[:500] if not brief.is_fresh else "",
        approach_guidance=approach,
        from_fallback=True,
        fallback_reason=reason,
    )
