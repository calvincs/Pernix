"""Pernix — Agent core loop.

Handles: context assembly, tool loop with stuck detection,
streaming response, checkpoint building, continuation.
Phase 3: no scout yet — uses fallback (direct) approach.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field

from config import settings
from core.context.compaction import compact_with_llm
from core.context.compiler import attach_cache_breakpoints, compile_context, normalize_for_openrouter
from core.context.tokens import get_estimator
from core.llm.client import get_llm_client
from core.llm.errors import FailoverError, FailoverReason
from core.llm.router import OPENAI_FORMAT_PROVIDERS
from core.llm.semaphore import PRIORITY_ORCHESTRATOR, PRIORITY_WORKER
from core.llm.types import StreamEventType
from core.tools.executor import execute_tool_round
from core.tools.registry import get_registry
from db import models as db
from sessions.state import AgentSession

logger = logging.getLogger("pernix.agent")


# ---------------------------------------------------------------------------
# Stuck detection
# ---------------------------------------------------------------------------

_FILE_TOOLS = {"file_edit", "file_write", "file_read", "file_append"}

_STREAM_BACKOFFS = (5, 10, 15)


def _is_stream_retryable(error: str) -> bool:
    return any(
        k in error
        for k in (
            "500",
            "502",
            "503",
            "504",
            "ConnectError",
            "ReadTimeout",
            "ConnectTimeout",
            "Connection refused",
        )
    )


# Empirically-verified tool-name hallucinations with compatible argument schemas.
# Rewrite silently (logged) instead of burning a round on the difflib hint path.
_TOOL_ALIASES = {
    "get_worker_output": "get_worker_result",
    "worker_get": "get_worker_result",
    "wait_for_workers": "await_workers",
}


def _prior_turn_tool_names(session_id: str, lookback: int = 40) -> set[str]:
    """Return tool names the session has invoked in recent assistant turns.

    Used for the monotonic-allowlist check at turn start — if the agent
    already used a tool successfully, scout's next recommendation should not
    silently drop it from the schema.
    """
    names: set[str] = set()
    try:
        messages = db.get_messages(session_id, last=lookback)
    except Exception:
        return names
    for m in messages[-lookback:]:
        if m.get("role") != "assistant":
            continue
        tc_raw = m.get("tool_calls")
        if not tc_raw:
            continue
        try:
            tcs = json.loads(tc_raw) if isinstance(tc_raw, str) else tc_raw
        except (json.JSONDecodeError, TypeError):
            continue
        for tc in (tcs if isinstance(tcs, list) else []):
            if not isinstance(tc, dict):
                continue
            name = tc.get("name") or tc.get("function", {}).get("name", "")
            if name:
                names.add(name)
    return names


_WEB_TOOLS = frozenset({"browse_web", "http_get", "search_web"})

# Kimi K2.6 emits its native special-token tool-call format as plain text when
# it degrades under loop pressure instead of using the structured API format.
# These patterns let us recover those calls rather than discarding them.
_KIMI_SECTION_RE = re.compile(
    r"<\|tool_calls_section_begin\|>.*?<\|tool_calls_section_end\|>",
    re.DOTALL,
)
_KIMI_CALL_RE = re.compile(
    r"<\|tool_call_begin\|>\s*(\S+?)(?::(\w+))?\s*<\|tool_call_argument_begin\|>(.*?)<\|tool_call_end\|>",
    re.DOTALL,
)

# Generic XML-style tool-call salvage. Catches model-emitted markup that
# leaks as text when the provider's chat-template parser fails to match the
# real special tokens. Covers DeepSeek's DSML format and any future
# Anthropic-XML-shaped degradation. The captured prefix group (\1) lets the
# closing tag's decoration match the opening tag's. Parameters are matched
# with a re.escape'd prefix at call time to avoid regex-injection.
_GENERIC_INVOKE_RE = re.compile(
    r'<([^\s<>/]*?)invoke\s+name="([^"]+)"\s*>(.*?)</\1invoke>',
    re.DOTALL,
)
_GENERIC_PARAM_RE_TMPL = r'<{prefix}parameter\s+name="([^"]+)"(?:\s+[^>]*)?\s*>(.*?)</{prefix}parameter>'

# Tool-result substrings that indicate "no useful info came back". Matched
# case-insensitively against the tool result body. Kept narrow on purpose so
# legitimate "404 documentation page" hits don't false-positive.
_LOW_INFO_RESULT_RE = re.compile(
    r"^\s*\(no output\)\s*$"
    r"|^No results found"
    r"|^\s*404:\s*Not Found"
    r"|Page not found"
    r"|Blocked: \S+ resolves to a private/internal address",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass
class StuckDetector:
    """Multi-signal stuck state detection (10 signals).

    Signals 1-7 are exact-args / structural — they catch tight loops where
    the agent re-issues the same call. Signals 8-10 are *semantic* — they
    catch the spiral mode where the agent issues many distinct calls (e.g.
    20 differently-worded search_web queries) that all return empty or
    bot-walled content. Without them, a search-spiral looks productive
    because every args hash is unique.
    """

    content_history: deque = field(default_factory=lambda: deque(maxlen=5))
    tool_call_history: deque = field(default_factory=lambda: deque(maxlen=10))
    # Per-result observations: (tool_name, host_or_query_token, low_info_bool, body_size)
    # Bounded so a long turn doesn't grow it without limit.
    web_result_history: deque = field(default_factory=lambda: deque(maxlen=12))
    domain_hits: dict = field(default_factory=dict)  # hostname → count this turn
    repeat_count: int = 0
    behavioral_flags: set = field(default_factory=set)
    has_unresolved_failure: bool = False
    unresolved_failure_rounds: int = 0
    file_failure_counts: dict = field(default_factory=dict)  # (tool_name, file_path) → int
    tool_failure_counts: dict = field(default_factory=dict)  # tool_name → consecutive-failure count

    def evaluate(self, content: str, tool_calls: list[dict] | None, tool_failures: dict, registry) -> tuple[float, int]:
        """Evaluate stuck signals. Returns (score 0-1, repeat_count)."""
        score = 0.0

        # Signal 1: Exact content repeat
        if content and content in self.content_history:
            score += 0.5
            self.behavioral_flags.add("content_repeat")

        # Signal 2: Tool call cycle
        if tool_calls:
            sig = tuple(sorted(f"{tc.get('name','')}:{_hash_args(tc.get('arguments',''))}" for tc in tool_calls))
            if sig in self.tool_call_history:
                score += 0.4
                self.behavioral_flags.add("tool_cycle")
            self.tool_call_history.append(sig)

        # Signal 3: Error-retry without change
        if tool_calls:
            for tc in tool_calls:
                name = tc.get("name", "")
                args_hash = _hash_args(tc.get("arguments", ""))
                prev_fails = tool_failures.get(name, [])
                if any(args_hash in f for f in prev_fails):
                    score += 0.4
                    self.behavioral_flags.add("error_loop")

        # Signal 4: Noop (no tools, no substance)
        if not tool_calls and content and _is_meta_commentary(content):
            score += 0.2
            self.behavioral_flags.add("noop_loop")

        # Signal 5: Hallucinated tools
        if tool_calls:
            for tc in tool_calls:
                if not registry.exists(tc.get("name", "")):
                    score += 0.3
                    self.behavioral_flags.add("hallucinated_tool")

        # Signal 6: Unresolved failure drift — agent makes unrelated calls
        # without addressing a recent tool failure
        if self.has_unresolved_failure and tool_calls:
            self.unresolved_failure_rounds += 1
            if self.unresolved_failure_rounds >= 3:
                score += 0.3
                self.behavioral_flags.add("failure_drift")

        # Signal 7: Same file tool, same target file, repeated failures.
        # Catches loops where each attempt uses a different old_string/args
        # (bypassing Signal 3's exact-args check) but targets the same file.
        for key, count in self.file_failure_counts.items():
            if count >= 3:
                score += 0.4
                self.behavioral_flags.add("file_edit_loop")
                break

        # Signal 11: Same NON-file tool failing repeatedly with varied args.
        # Generalises Signal 7 beyond file tools — catches loops like call_model
        # 404-ing on a series of guessed model ids, where each call has a unique
        # args hash (evading Signal 3), the tool is real (evading Signal 5), and
        # a fresh failure each round keeps resetting Signal 6's drift counter.
        for tool_name, count in self.tool_failure_counts.items():
            if tool_name not in _FILE_TOOLS and count >= 3:
                score += 0.4
                self.behavioral_flags.add("tool_failure_loop")
                break

        # Signal 8: Empty-result streak. ≥3 consecutive web/search results that
        # matched the low-info pattern (No results / 404 / SSRF block).
        # Catches the search-spiral mode that signals 1-3 miss because each
        # query has a unique args hash.
        recent_web = list(self.web_result_history)
        if len(recent_web) >= 3:
            tail = recent_web[-3:]
            if all(entry[2] for entry in tail):  # entry[2] = low_info bool
                score += 0.3
                self.behavioral_flags.add("empty_result_streak")

        # Signal 9: Bot-wall streak. ≥3 of the last browse_web/http_get
        # responses whose body was implausibly small for a real page (<800 b).
        # Filter to browse/http first so interleaved search_web calls don't
        # break the streak (the motivating spiral had 20 search_web + 18
        # browse_web interspersed — the original [-3:] slice over all web
        # types would have silently missed this).
        browse_http = [e for e in recent_web if e[0] in {"browse_web", "http_get"}]
        if len(browse_http) >= 3 and all(e[3] is not None and e[3] < 800 for e in browse_http[-3:]):
            score += 0.3
            self.behavioral_flags.add("bot_wall_streak")

        # Signal 10: Same-domain repetition. Same hostname hit >5 times in a
        # turn — indicates the agent is grinding on one source instead of
        # pivoting (rate-limited, blocked, or genuinely the wrong source).
        for host, count in self.domain_hits.items():
            if count > 5:
                score += 0.2
                self.behavioral_flags.add("same_domain_repetition")
                break

        # Update
        if content:
            self.content_history.append(content)

        if score > 0.3:
            self.repeat_count += 1
        elif not self.has_unresolved_failure:
            self.repeat_count = max(0, self.repeat_count - 1)

        return score, self.repeat_count

    def observe_result(self, tool_name: str, args: dict | None, content: str, was_error: bool) -> None:
        """Record a tool result for semantic-streak signals (8-10).

        Called per tool result, not per LLM round. Cheap — just bookkeeping.
        """
        if tool_name not in _WEB_TOOLS:
            return
        body = content or ""
        low_info = was_error or len(body.strip()) == 0 or bool(_LOW_INFO_RESULT_RE.search(body))
        body_size = len(body) if body else 0
        # Pull a cheap host token out of args for domain_hits.
        host_token = ""
        if isinstance(args, dict):
            url = args.get("url") or args.get("href") or ""
            if isinstance(url, str) and "://" in url:
                try:
                    from urllib.parse import urlparse

                    host_token = (urlparse(url).hostname or "").lower()
                except Exception:
                    host_token = ""
        if host_token:
            self.domain_hits[host_token] = self.domain_hits.get(host_token, 0) + 1
        self.web_result_history.append((tool_name, host_token, low_info, body_size))

    def mark_failure(self, tool_name: str = "", args: dict | None = None) -> None:
        """Record that a tool failure occurred this round.

        If tool_name is a file-targeting tool and args contains a file path,
        increments the per-file failure counter (Signal 7).
        """
        self.has_unresolved_failure = True
        self.unresolved_failure_rounds = 0
        if tool_name:
            # Per-tool consecutive-failure counter (Signal 11). Reset by a
            # success of the SAME tool in mark_success, so failures interleaved
            # with unrelated successful calls still accumulate.
            self.tool_failure_counts[tool_name] = self.tool_failure_counts.get(tool_name, 0) + 1
        if tool_name in _FILE_TOOLS and args:
            file_path = args.get("path") or args.get("file_path") or args.get("file", "")
            if file_path:
                key = (tool_name, str(file_path))
                self.file_failure_counts[key] = self.file_failure_counts.get(key, 0) + 1

    def mark_success(self, tool_name: str = "", args: dict | None = None) -> None:
        """Record that a tool succeeded, clearing unresolved failure state.

        If tool_name is a file-targeting tool, resets its per-file failure counter.
        """
        self.has_unresolved_failure = False
        self.unresolved_failure_rounds = 0
        if tool_name:
            self.tool_failure_counts.pop(tool_name, None)
        if tool_name in _FILE_TOOLS and args:
            file_path = args.get("path") or args.get("file_path") or args.get("file", "")
            if file_path:
                key = (tool_name, str(file_path))
                self.file_failure_counts.pop(key, None)


def _goal_budget_exceeded(session_id: str, goal_id: int) -> str | None:
    """Synchronous mid-turn budget check (runs in a thread). Returns a short
    reason string when the active goal's token or time budget is spent."""
    from datetime import datetime, timezone

    goal = db.get_active_goal(session_id)
    if not goal or int(goal.get("id", 0)) != int(goal_id):
        return None
    if goal.get("token_budget"):
        used = db.goal_token_usage(goal["id"])
        if used >= int(goal["token_budget"]):
            return f"token budget spent ({used:,}/{int(goal['token_budget']):,})"
    if goal.get("time_budget_s") and goal.get("started_at"):
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(goal["started_at"])).total_seconds()
        if elapsed >= int(goal["time_budget_s"]):
            return f"time budget spent ({int(elapsed)}s/{int(goal['time_budget_s'])}s)"
    return None


def _hash_args(args) -> str:
    if isinstance(args, dict):
        args = json.dumps(args, sort_keys=True)
    return hashlib.sha256(str(args).encode()).hexdigest()[:12]


def _summarize_args(args: dict, max_value_len: int = 200) -> dict:
    """Summarize tool arguments for event emission, truncating long values."""
    summary = {}
    for k, v in args.items():
        s = str(v)
        summary[k] = s[:max_value_len] + "..." if len(s) > max_value_len else s
    return summary


# Tools where semantic dedup is applied (expensive/LLM-wrapping tools)
_SEMANTIC_DEDUP_TOOLS = {"call_model"}

# Tools excluded from cross-round hard dedup — cheap reads where fresh state matters.
# bash is intentionally absent: repeated bash calls (e.g. re-running transcription) are the primary target.
# Caveat: bash output also depends on workspace file contents, so a cached bash
# result goes stale the moment the agent edits a file. _STATE_MUTATING_TOOLS +
# _invalidate_bash_dedup below clear those stale entries so an "edit → re-run the
# same command" cycle re-executes instead of short-circuiting to the pre-edit result.
_CROSS_ROUND_DEDUP_EXCLUDED = {"file_read", "glob"}

# Tools that mutate workspace files. When one of these succeeds, any cached bash
# result may no longer reflect reality (the agent just changed a file that a
# later identical bash command reads/uploads/runs), so we invalidate the bash
# dedup cache. Scoped to the agent's file editors — not bash itself, since we
# can't reliably tell which bash commands mutate state.
_STATE_MUTATING_TOOLS = {"file_write", "file_edit", "multiedit"}


def _invalidate_bash_dedup(cross_round_calls: dict) -> int:
    """Drop cached bash results from the cross-round dedup map.

    Called when a state-mutating tool (file_write/file_edit/multiedit) succeeds.
    bash output is a function of workspace file contents, so once the agent edits
    a file, an identical bash command (e.g. re-uploading and running a script it
    just fixed) can produce a different result. Leaving the stale entry cached
    makes the next identical call short-circuit to the pre-edit output, trapping
    the agent in an "edit → re-run → stale failure" loop (observed in session
    9b075def0076). Returns the number of entries removed (for logging).
    """
    stale = [k for k in cross_round_calls if k.startswith("bash:")]
    for k in stale:
        del cross_round_calls[k]
    return len(stale)


def _parse_args_dict(tc: dict) -> dict:
    """Parse tool call arguments into a dict."""
    raw = tc.get("arguments", "")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def _is_near_duplicate_call(a: dict, b: dict, tool_name: str) -> bool:
    """Check if two tool calls are near-duplicates based on structural args.

    For call_model: same model + same images/attachments = near-duplicate,
    regardless of prompt wording differences.
    """
    a_args = _parse_args_dict(a)
    b_args = _parse_args_dict(b)

    if tool_name == "call_model":
        # Same model and same images → near-duplicate
        same_model = a_args.get("model", "") == b_args.get("model", "")
        same_images = a_args.get("images", []) == b_args.get("images", [])
        return same_model and same_images

    # Generic fallback: compare all args except the largest string value
    # (assumed to be the "prompt" or main content)
    a_structural = {k: v for k, v in a_args.items() if not isinstance(v, str) or len(v) < 100}
    b_structural = {k: v for k, v in b_args.items() if not isinstance(v, str) or len(v) < 100}
    return a_structural == b_structural and len(a_structural) > 0


def _is_meta_commentary(text: str) -> bool:
    """Check if text is purely meta-commentary without substance."""
    stalling = ["i'll now", "let me try", "i need to", "i should", "i will", "let me ", "i'm going to", "next i"]
    lower = text.lower().strip()
    return any(lower.startswith(s) for s in stalling) and len(lower) < 200


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------


async def run_agent(
    session_id: str,
    message: str,
    session: AgentSession,
    is_retry: bool = False,
    pre_saved: bool = False,
) -> None:
    """Main agent execution loop.

    1. Save user message (skipped on Reflect retries — already in DB)
    2. Build context (base prompt + fallback SOUL/RULES loading)
    3. Tool loop with stuck detection
    4. Stream final response
    5. Post-response: checkpoint, continuation
    """
    registry = get_registry()
    client = get_llm_client()
    estimator = get_estimator()

    # Derive LLM scheduling context for this session once.
    # Workers get lower priority than their orchestrating parent.
    _sched_priority = PRIORITY_WORKER if session.session_type == "worker" else PRIORITY_ORCHESTRATOR
    try:
        from datetime import datetime as _dt

        # created_at lives on the DB row, not the in-memory AgentSession —
        # the old `session.created_at` raised AttributeError on every turn,
        # was swallowed, and age-based scheduling fairness never worked
        # (every session ranked float("inf")).
        _row = await asyncio.to_thread(db.get_session, session_id)
        _sched_created_at = _dt.fromisoformat((_row or {}).get("created_at", "").replace("Z", "+00:00")).timestamp()
    except Exception:
        _sched_created_at = float("inf")

    # Resolve the live goal id once per turn for token_usage stamping
    # (plan 3b). Workers keep an inherited id from spawn; everyone else
    # re-resolves so a goal created/completed between turns is respected.
    if settings.goals_enabled and session.session_type != "worker":
        try:
            _goal_row = await asyncio.to_thread(db.get_active_goal, session_id)
            session.active_goal_id = (_goal_row or {}).get("id")
        except Exception:
            session.active_goal_id = None

    # Save user message (skip on retry or when already persisted by the caller).
    # The manager locks current_turn_user_msg_id in at pre-save time so it
    # stays stable even if a queued message overwrites session.last_user_msg_id
    # mid-turn. compile_context and reflect read this to scope to this turn.
    if not is_retry and not pre_saved:
        _new_id = await asyncio.to_thread(db.add_message, session_id, "user", message)
        session.last_user_msg_id = _new_id
        session.current_turn_user_msg_id = _new_id
    _turn_user_msg_id = session.current_turn_user_msg_id
    session.touch()
    # Clear any prior turn's termination_reason — set freshly below at each exit path.
    session.termination_reason = None

    # Helper: save assistant/tool messages tagged with this turn's user msg id
    # so compile_context can render messages in logical-turn order even when
    # raw id ordering is jumbled by queued user messages arriving mid-turn.
    async def _save_turn_msg(role: str, content: str, **kwargs) -> int:
        # Off-loop: add_message also FTS-indexes the content inline — for a
        # 100KB tool result that's porter tokenization plus a write that can
        # block behind busy_timeout, all of which froze every session's SSE
        # when run on the event loop.
        meta = kwargs.pop("metadata", None)
        if _turn_user_msg_id is not None:
            try:
                base = json.loads(meta) if meta else {}
            except Exception:
                base = {}
            base.setdefault("parent_user_msg_id", _turn_user_msg_id)
            meta = json.dumps(base)
        return await asyncio.to_thread(db.add_message, session_id, role, content, metadata=meta, **kwargs)

    # Get scout report (prepared by session manager before agent runs)
    scout_report = session.last_scout_report
    if scout_report:
        scout_text = scout_report.to_system_prompt_section()
        recommended = scout_report.get_tool_names()
        active_tools_set = set(recommended)
        # Always ensure core tools are available
        for t in registry.enabled_tools():
            if t.source == "builtin":
                active_tools_set.add(t.name)
        # Monotonic allowlist: if the agent successfully used an extension tool
        # in a prior turn of this session, keep it in the schema. Prevents
        # scout from silently narrowing the surface between turns (e.g.
        # dropping install_package after a successful pip install), forcing
        # the agent to re-discover tools it has already proven it needs.
        try:
            prior_tools = _prior_turn_tool_names(session_id)
            for tname in prior_tools:
                # Skip disabled — a tool the user toggled off between turns
                # must NOT be re-promoted by the monotonic allowlist; otherwise
                # disabling a previously-used tool has no effect on the next turn.
                if registry.exists(tname) and not registry.is_disabled(tname):
                    active_tools_set.add(tname)
        except Exception as _e:
            logger.debug("Monotonic allowlist lookup failed for %s: %s", session_id, _e)
        # Pull in co-occurring siblings so e.g. spawn_worker brings
        # get_worker_result / check_workers / await_workers into the schema.
        active_tools_set = registry.expand_cooccurrence(active_tools_set)
        # Retry effector (audit P1f): tools reflect disabled for this retry
        # attempt are removed from the schema entirely — overriding builtins
        # and the monotonic allowlist. The executor enforces this too.
        _retry_excluded = getattr(session, "retry_excluded_tools", None) or set()
        if _retry_excluded:
            active_tools_set -= _retry_excluded
        active_tools = sorted(active_tools_set)  # deterministic order for prompt cache
    else:
        # No scout report available. Nothing to substitute: SOUL/RULES/SESSIONS
        # arrive via the compiler's fixed-prefix directives block on every
        # turn, so there is no identity to recover here — only the per-task
        # curation is missing, and no deterministic text can stand in for that.
        scout_text = ""
        active_tools = sorted(t.name for t in registry.enabled_tools())

    # Effective model: per-session override (for workers) or global default.
    # Resolved per-round inside the loop so an in-turn switch_model call
    # actually takes effect on subsequent rounds (the override is a string
    # field on `session`, mutated by the model_mgmt extension).
    _baseline_model = settings.llm_model

    def _resolve_effective_model() -> tuple[str, bool, bool]:
        raw = session.model_override or settings.llm_model
        resolved_id = client.router.registry.resolve_model_id(raw)
        if resolved_id != raw:
            logger.info("Session %s: resolved model '%s' -> '%s'", session_id, raw, resolved_id)
        info = client.router.registry.get_model_info(resolved_id)
        return (
            resolved_id,
            (info.supports_vision if info else False),
            (info.supports_audio if info else False),
        )

    effective_model, model_supports_vision, model_supports_audio = _resolve_effective_model()
    _last_effective_model = effective_model

    # Model-derived context budget (audit P2): settings.context_budget is a
    # fallback, not a global truth — a 1M-context model should not silently
    # run at the fallback size. Registry catalog lookup, no network.
    def _derive_model_budget(model: str) -> int | None:
        try:
            _mb_info = client.router.registry.get_model_info(model)
            _mb_len = int(getattr(_mb_info, "context_length", 0) or 0) if _mb_info else 0
            if _mb_len > 0:
                return max(32_000, int(_mb_len * 0.9))
        except Exception as _e:
            logger.debug("Model context budget lookup failed for %s: %s", model, _e)
        return None

    _model_budget: int | None = _derive_model_budget(effective_model)

    # Tool loop state
    stuck = StuckDetector()
    tool_failures: dict[str, list[str]] = {}
    nudges_fired: set[str] = set()  # one harness nudge per pattern per turn
    _cross_round_calls: dict[str, tuple[int, str]] = {}  # hash → (round_num, truncated_result)
    did_tool_calls = False
    # Compaction-retry accounting. The state machine explicitly models repeated
    # PROCESSING↔COMPACTING round-trips per turn (compaction_count in the state
    # log); the old single-shot boolean here meant a long turn that needed a
    # second compaction died with compaction_failed even when the deliverable
    # was one round away. Allow up to _COMPACTION_ATTEMPT_LIMIT, but require
    # each compaction to actually shrink the context (progress guard below) so
    # a no-op compactor can't burn all attempts in a tight loop.
    _compaction_attempts = 0
    _COMPACTION_ATTEMPT_LIMIT = 3
    _tokens_before_last_compaction: int | None = None
    _tried_fallback = False  # sticky per-turn: once we fail over, stay on fallback for all remaining rounds
    _last_usage = None  # local tracker — avoids reading shared client.last_usage across sessions
    # Counter for "stuck + told to call ask_user but did not" consecutive rounds.
    # Without a cap, an LLM that ignores the ask_user nudge keeps the loop
    # spinning indefinitely (observed: 16 nudges in a row before the agent
    # self-corrected). After STUCK_ASK_USER_LIMIT consecutive nudge-and-continue
    # rounds, fall through to the same "summarize and stop" break path used
    # when ask_user isn't available.
    _stuck_ask_user_continues = 0
    STUCK_ASK_USER_LIMIT = 3

    # Counter for max_tokens-truncation continuations. When the LLM reports
    # finish_reason="length" (Ollama: done_reason="length") with no tool calls
    # and visible content, we save the partial assistant message, inject a
    # system reminder, and let the loop iterate so the model can finish its
    # thought. Capped to prevent a runaway "model wants to write forever"
    # case from spinning the loop indefinitely.
    _length_continuation_count = 0
    LENGTH_CONTINUATION_LIMIT = 2

    tool_round = 0
    while tool_round < settings.max_tool_rounds:
        # --- Pre-round checks ---
        # Cooperative cancellation checkpoint
        if session.cancel_requested:
            logger.info("Session %s: cancel requested, exiting agent loop", session_id)
            session.termination_reason = "cancelled"
            return

        # In-turn goal budget checkpoint (audit P5): budgets used to be
        # checked only BETWEEN turns, so a single turn could overshoot
        # token/time budgets without bound. Every third round is enough —
        # the between-turns check remains the authoritative settlement.
        if settings.goals_enabled and session.active_goal_id and tool_round > 0 and tool_round % 3 == 0:
            try:
                _exceeded = await asyncio.to_thread(_goal_budget_exceeded, session_id, session.active_goal_id)
            except Exception as _e:
                logger.debug("In-turn goal budget check failed: %s", _e)
                _exceeded = None
            if _exceeded:
                logger.info("Session %s: goal budget exceeded mid-turn (%s), ending turn", session_id, _exceeded)
                session.emit_event({"type": "goal.budget_exceeded", "reason": _exceeded})
                session.termination_reason = "budget_exhausted"
                break

        # Pause checkpoint (for workers). The pause/resume round-trip is
        # modelled explicitly in the state machine: PROCESSING→PAUSE_REQUESTED
        # (set by pause_worker HTTP/tool) → PAUSED (observed here) → PROCESSING
        # (on resume). A cancel during pause wakes the loop and triggers the
        # CancelledError → CANCELLING path in _run_agent_safe.
        if not session.pause_event.is_set():
            from sessions import state_v2 as _sv2

            if _sv2._current_state(session) is _sv2.SessionStateV2.PAUSE_REQUESTED:
                try:
                    _sv2.transition(session, _sv2.SessionStateV2.PAUSED, "pause-observed")
                except Exception as _e:
                    logger.error("pause-observed transition failed: %s", _e)
            await session.pause_event.wait()
            if _sv2._current_state(session) is _sv2.SessionStateV2.PAUSED:
                try:
                    _sv2.transition(session, _sv2.SessionStateV2.PROCESSING, "resume")
                except Exception as _e:
                    logger.error("resume transition failed: %s", _e)
            # Re-check cancel after resume (cancel_worker may have fired
            # while we were paused).
            if session.cancel_requested:
                logger.info("Session %s: cancel observed after resume", session_id)
                session.termination_reason = "cancelled"
                return

        # Re-resolve effective model each round so an in-turn switch_model
        # call (which writes session.model_override) actually moves the next
        # round's LLM call to the new provider/model. Emit an event the UI
        # can render as an "<orig> ⇄ <override>" indicator while active, and
        # persist a model_divider message so the chat shows the switch even
        # after a page reload (live SSE events aren't replayed from DB).
        effective_model, model_supports_vision, model_supports_audio = _resolve_effective_model()
        if effective_model != _last_effective_model:
            _model_budget = _derive_model_budget(effective_model)
            override_active = session.model_override is not None
            session.emit_event(
                {
                    "type": "model.override",
                    "from": _baseline_model,
                    "to": effective_model if override_active else None,
                    "active": override_active,
                }
            )
            logger.info(
                "Session %s: effective model changed mid-turn '%s' -> '%s' (override_active=%s)",
                session_id,
                _last_effective_model,
                effective_model,
                override_active,
            )
            try:
                await asyncio.to_thread(
                    db.add_message,
                    session_id,
                    "model_divider",
                    "",
                    metadata=json.dumps(
                        {
                            "from": _last_effective_model,
                            "to": effective_model,
                            "active": override_active,
                            "baseline": _baseline_model,
                        }
                    ),
                )
            except Exception as _e:
                logger.debug("model_divider persist failed: %s", _e)
            session.emit_event(
                {
                    "type": "model.divider",
                    "from": _last_effective_model,
                    "to": effective_model,
                    "active": override_active,
                    "baseline": _baseline_model,
                }
            )
            _last_effective_model = effective_model

        # Build context (resource status is dynamic — includes remaining tool rounds).
        # Both run off-loop: resource status aggregates token_usage, and
        # compile_context loads the full message history + tokenizes — per
        # round, this was the single heaviest synchronous block on the loop.
        effective_budget = session.context_budget_override or _model_budget or settings.context_budget
        resource_status = await asyncio.to_thread(
            _build_resource_status, session_id, estimator, tool_round, context_budget=effective_budget
        )
        payload = await asyncio.to_thread(
            compile_context,
            session_id=session_id,
            tool_schemas=registry.get_schemas(active_tools),
            scout_report_text=scout_text,
            resource_status=resource_status,
            supports_vision=model_supports_vision,
            supports_audio=model_supports_audio,
            context_budget=effective_budget,
            model_name=effective_model,
            turn_user_msg_id=_turn_user_msg_id,
        )

        # Context health check
        utilization = payload.token_count / max(effective_budget, 1)
        if utilization > settings.context_critical_threshold:
            # Progress guard: a prior compaction this turn must have shrunk the
            # context measurably, else retrying the compactor is wishful — bail
            # to compaction_failed instead of burning the remaining attempts.
            _no_progress = (
                _tokens_before_last_compaction is not None
                and payload.token_count >= _tokens_before_last_compaction * 0.95
            )
            if _compaction_attempts < _COMPACTION_ATTEMPT_LIMIT and not _no_progress:
                logger.warning(
                    "Context critical (%.0f%%), attempting compaction-retry %d/%d",
                    utilization * 100,
                    _compaction_attempts + 1,
                    _COMPACTION_ATTEMPT_LIMIT,
                )
                session.emit_event({"type": "context.compacting", "reason": "critical_threshold"})
                from sessions import state_v2 as _sv2

                try:
                    _sv2.transition(session, _sv2.SessionStateV2.COMPACTING, "compact-critical")
                except Exception as _e:
                    logger.error("compact-critical transition failed: %s", _e)
                _tokens_before_last_compaction = payload.token_count
                compacted = await compact_with_llm(session_id, payload.messages)
                _compaction_attempts += 1
                session.touch()  # keep reaper honest — COMPACTING can take seconds
                if compacted:
                    try:
                        _sv2.transition(session, _sv2.SessionStateV2.PROCESSING, "compact-done")
                    except Exception as _e:
                        logger.error("compact-done transition failed: %s", _e)
                    continue  # re-compile context, retry same round
            # Compaction failed, made no progress, or attempts exhausted — break with error
            logger.warning("Context critical (%.0f%%) after compaction, breaking", utilization * 100)
            session.emit_event({"type": "context.reset"})
            session.emit_event(
                {
                    "type": "stream.error",
                    "error": f"Context full ({utilization:.0%}). Compaction insufficient.",
                }
            )
            session.termination_reason = "compaction_failed"
            break

        # Proactive compaction stays single-shot relative to the critical/
        # overflow paths: once a forced compaction has run this turn, the
        # critical path above owns further attempts.
        if payload.needs_compaction and _compaction_attempts == 0:
            from sessions import state_v2 as _sv2

            session.emit_event({"type": "context.compacting"})
            try:
                _sv2.transition(session, _sv2.SessionStateV2.COMPACTING, "compact-proactive")
            except Exception as _e:
                logger.error("compact-proactive transition failed: %s", _e)
            # Count this against the per-turn attempt budget and record the
            # pre-compaction size. Without this, a compactor that fails to
            # shrink the context (e.g. the historical compacted_up_to=0 bug)
            # re-fires every tool round for the whole turn. Incrementing hands
            # any further attempts to the critical path above, which enforces
            # the _no_progress guard and the attempt limit.
            _tokens_before_last_compaction = payload.token_count
            await compact_with_llm(session_id, payload.messages)
            _compaction_attempts += 1
            session.touch()
            try:
                _sv2.transition(session, _sv2.SessionStateV2.PROCESSING, "compact-done")
            except Exception as _e:
                logger.error("compact-done (proactive) transition failed: %s", _e)

        # Normalize for provider
        messages = payload.messages
        model = effective_model
        _provider = client.resolve_provider(model)
        if _provider in OPENAI_FORMAT_PROVIDERS:
            messages = normalize_for_openrouter(messages)
            messages = attach_cache_breakpoints(messages, model, _provider, payload.static_prefix_chars)

        # --- LLM call ---
        # Keep tools available throughout the loop so the model can chain
        # (e.g. discover_tools → search_web → answer). Only force tools=None
        # on the absolute last round to guarantee a text response. The
        # "LAST ROUND (tools disabled)" copy in resource_status (tier at
        # remaining==1) already tells the model what to do — no second
        # compile needed.
        stream_tools = payload.tools
        if tool_round == settings.max_tool_rounds - 1:
            stream_tools = None

        # --- Stream with retry/fallback ---
        _stream_retries = 0
        # _tried_fallback is declared before the outer loop (per-turn sticky).
        # If we already fell back this turn, skip straight to the fallback model
        # rather than re-attempting the rate-limited primary on every round.
        _stream_current_model = settings.fallback_model if _tried_fallback else model
        _compaction_triggered = False
        collected_content = ""
        collected_tool_calls: list[dict] = []
        # Track per-round assistant latency. Reset inside the retry loop
        # so the value reflects the *successful* attempt's wall clock, not
        # the cumulative time across retries.
        _round_started_at = time.monotonic()

        while True:  # stream retry loop
            collected_content = ""
            collected_tool_calls = []
            _stream_err: str | None = None
            _compaction_triggered = False
            _round_started_at = time.monotonic()
            _stream_finish_reason: str | None = None  # captured from DONE event

            if not client.has_capacity(_stream_current_model):
                session.emit_event({"type": "session.waiting_llm"})

            try:
                async for event in client.chat_stream(
                    messages,
                    tools=stream_tools,
                    model=_stream_current_model,
                    session_id=session_id,
                    session_created_at=_sched_created_at,
                    session_priority=_sched_priority,
                ):
                    if event.type == StreamEventType.TOKEN and event.content:
                        collected_content += event.content
                        session.emit_event(
                            {
                                "type": "stream.token",
                                "content": event.content,
                            }
                        )

                    elif event.type == StreamEventType.TOOL_CALL and event.tool_calls:
                        for tc in event.tool_calls:
                            # Merge by ID if we already have a partial with this ID
                            existing = next(
                                (c for c in collected_tool_calls if c["id"] == tc.id and tc.id),
                                None,
                            )
                            if existing:
                                if tc.name:
                                    existing["name"] = tc.name
                                existing["arguments"] += tc.arguments
                            else:
                                collected_tool_calls.append(
                                    {
                                        "id": tc.id,
                                        "name": tc.name,
                                        "arguments": tc.arguments,
                                    }
                                )

                    elif event.type == StreamEventType.USAGE and event.usage:
                        _last_usage = event.usage
                        await asyncio.to_thread(
                            db.add_token_usage,
                            session_id=session_id,
                            model=_stream_current_model,
                            prompt_tokens=event.usage.prompt_tokens,
                            completion_tokens=event.usage.completion_tokens,
                            total_tokens=event.usage.total_tokens,
                            cache_read_tokens=event.usage.cache_read_tokens,
                            cache_write_tokens=event.usage.cache_write_tokens,
                            source="provider",
                            provider=client.resolve_provider(_stream_current_model),
                            goal_id=session.active_goal_id,
                        )

                    elif event.type == StreamEventType.ERROR and event.error:
                        _stream_err = event.error
                        break

                    elif event.type == StreamEventType.DONE:
                        # Capture the provider's finish_reason so the agent
                        # loop can detect max_tokens truncation and trigger an
                        # in-turn continuation (vs. a full reflect-retry).
                        _stream_finish_reason = event.finish_reason

            except FailoverError as fe:
                # Context overflow: trigger compaction and restart the tool round
                if fe.reason == FailoverReason.CONTEXT_OVERFLOW and _compaction_attempts < _COMPACTION_ATTEMPT_LIMIT:
                    logger.warning(
                        "API context overflow in session %s, attempting compaction-retry %d/%d",
                        session_id,
                        _compaction_attempts + 1,
                        _COMPACTION_ATTEMPT_LIMIT,
                    )
                    from sessions import state_v2 as _sv2

                    session.emit_event({"type": "context.compacting", "reason": "api_overflow"})
                    try:
                        _sv2.transition(session, _sv2.SessionStateV2.COMPACTING, "compact-overflow")
                    except Exception as _e:
                        logger.error("compact-overflow transition failed: %s", _e)
                    _tokens_before_last_compaction = payload.token_count
                    compacted = await compact_with_llm(session_id, payload.messages)
                    _compaction_attempts += 1
                    session.touch()
                    if compacted:
                        try:
                            _sv2.transition(session, _sv2.SessionStateV2.PROCESSING, "compact-done")
                        except Exception as _e:
                            logger.error("compact-done (overflow) transition failed: %s", _e)
                    _compaction_triggered = True
                    break  # exit stream retry loop; continue tool_round loop below
                _stream_err = fe.message

            except Exception as e:
                _stream_err = str(e)

            if _compaction_triggered:
                break  # exit stream retry loop

            if _stream_err is None:
                break  # stream completed successfully

            # --- Retry / fallback decision ---
            if _stream_retries < len(_STREAM_BACKOFFS) and _is_stream_retryable(_stream_err):
                wait = _STREAM_BACKOFFS[_stream_retries]
                _stream_retries += 1
                logger.warning(
                    "LLM stream error (attempt %d/3) in session %s, retrying in %ds: %s",
                    _stream_retries,
                    session_id,
                    wait,
                    _stream_err,
                )
                session.emit_event(
                    {"type": "stream.retry", "attempt": _stream_retries, "wait": wait, "error": _stream_err}
                )
                await asyncio.sleep(wait)
                continue

            # A different model is a viable fallback even on the same provider
            # (model-specific failures, per-model rate buckets). Requiring a
            # different provider meant an Ollama-primary/Ollama-fallback
            # config silently had no failover at all.
            fallback = settings.fallback_model
            if fallback and not _tried_fallback and fallback != _stream_current_model:
                _tried_fallback = True
                _stream_retries = 0
                _stream_current_model = fallback
                logger.warning(
                    "LLM retries exhausted for session %s, switching to fallback model: %s",
                    session_id,
                    fallback,
                )
                session.emit_event({"type": "stream.fallback", "model": fallback})
                _fb_provider = client.resolve_provider(fallback)
                if _fb_provider in OPENAI_FORMAT_PROVIDERS:
                    messages = normalize_for_openrouter(payload.messages)
                    messages = attach_cache_breakpoints(messages, fallback, _fb_provider, payload.static_prefix_chars)
                else:
                    messages = payload.messages
                continue

            # No viable path — save partial and report failure.
            #
            # SOFT-LAND for LLM budget exhaustion: when the per-session LLM
            # time budget (settings.llm_session_timeout, default 1800s) trips
            # mid-turn, the agent has typically already produced visible work
            # in this turn (prior assistant rounds, tool calls, files). Hard-
            # erroring the turn means reflect runs against a transcript that
            # looks broken, even when the actual deliverable is fine. Detect
            # the LLMSessionTimeoutError specifically and route to a clean
            # termination (BUDGET_EXHAUSTED) so reflect grades the partial
            # output as a near-pass instead of "session crashed."
            _is_budget_exhausted = "exceeded the" in (_stream_err or "") and "LLM time limit" in (_stream_err or "")
            if collected_content:
                mid = await _save_turn_msg("assistant", collected_content, partial=1)
                session.emit_event(
                    {
                        "type": "partial.saved",
                        "content_preview": collected_content[:100],
                        "message_id": mid,
                    }
                )
            if _is_budget_exhausted:
                # Inject a system message documenting the soft-land so the
                # reflect evidence shows it AND the user-visible transcript
                # explains the truncation point. We do NOT set session.error
                # here — that's reserved for genuine failures.
                await asyncio.to_thread(
                    db.add_message,
                    session_id,
                    "system",
                    "Turn ended early: per-session LLM time budget exhausted. "
                    "Any content the agent produced before this point is the "
                    "best result available for this turn. Reflect should grade "
                    "the existing transcript on its merits.",
                )
                logger.warning(
                    "LLM budget exhausted in session %s — soft-landing as " "BUDGET_EXHAUSTED instead of error: %s",
                    session_id,
                    _stream_err,
                )
                session.termination_reason = "budget_exhausted"
                session.emit_event(
                    {
                        "type": "stream.budget_exhausted",
                        "message": _stream_err,
                    }
                )
                return
            logger.error("LLM stream error in session %s: %s", session_id, _stream_err)
            session.error = _stream_err
            session.termination_reason = "error"
            session.emit_event({"type": "stream.error", "error": _stream_err})
            return

        if _compaction_triggered:
            continue  # restart tool_round loop with compacted context

        # --- Length-truncation continuation ---
        # If the provider reports finish_reason="length" with content and no
        # tool calls, the model hit max_tokens mid-thought. Save the partial,
        # inject a system reminder, and continue the loop so the next round
        # can finish. Much cheaper than a full reflect-retry (one extra agent
        # round vs. ~50s of reflect+scout+agent re-execution).
        if (
            _stream_finish_reason == "length"
            and not collected_tool_calls
            and collected_content
            and _length_continuation_count < LENGTH_CONTINUATION_LIMIT
        ):
            _length_continuation_count += 1
            logger.info(
                "Session %s: LLM hit max_tokens (finish_reason=length) " "round=%d, continuing (attempt %d/%d)",
                session_id,
                tool_round,
                _length_continuation_count,
                LENGTH_CONTINUATION_LIMIT,
            )
            # Save the partial as the assistant message (no partial=1 flag —
            # this isn't an error path, the response IS valid, just unfinished).
            await _save_turn_msg("assistant", collected_content)
            # Inject a system message instructing the model to continue. The
            # next round will see its own truncated assistant message in the
            # transcript and pick up from where it stopped.
            await asyncio.to_thread(
                db.add_message,
                session_id,
                "system",
                "Your previous response was cut off because it hit the "
                "max_tokens limit. Continue from exactly where you stopped — "
                "do not repeat what you already wrote.",
            )
            session.emit_event(
                {
                    "type": "stream.length_continuation",
                    "attempt": _length_continuation_count,
                    "max": LENGTH_CONTINUATION_LIMIT,
                }
            )
            session.touch()
            tool_round += 1  # this counts as a round consumed
            continue  # back to top of tool_round loop

        # --- Kimi native-format tool-call recovery ---
        # kimi-k2.6 sometimes falls back to its special-token format as plain
        # text (e.g. under loop pressure) instead of structured API tool calls.
        # Parse and promote them so the normal execution path handles them.
        if not collected_tool_calls and collected_content and "<|tool_call_begin|>" in collected_content:
            recovered = []
            for m in _KIMI_CALL_RE.finditer(collected_content):
                name_raw, call_id, args_raw = m.group(1), m.group(2), m.group(3).strip()
                recovered.append(
                    {
                        "id": f"kimi_{call_id}" if call_id else f"kimi_{len(recovered)}",
                        "name": name_raw.strip(),
                        "arguments": args_raw,
                    }
                )
            if recovered:
                logger.warning(
                    "Session %s: recovered %d Kimi native-format tool call(s) from text content",
                    session_id,
                    len(recovered),
                )
                collected_content = _KIMI_SECTION_RE.sub("", collected_content).strip()
                collected_tool_calls = recovered

        # --- Generic XML-style native tool-call recovery ---
        # Some models (DeepSeek-V series, etc.) emit their tool-call markup
        # as literal text when the served model's template parser fails to
        # match the real special tokens. Detect the invoke/parameter XML
        # shape, validate the tool name against the live registry, and
        # promote structurally well-formed calls into collected_tool_calls.
        if not collected_tool_calls and collected_content and "invoke" in collected_content:
            recovered: list[dict] = []
            spans_to_strip: list[tuple[int, int]] = []
            first_prefix: str | None = None
            for inv in _GENERIC_INVOKE_RE.finditer(collected_content):
                prefix, name, body = inv.group(1), inv.group(2).strip(), inv.group(3)
                if not registry.exists(name):
                    continue
                param_re = re.compile(
                    _GENERIC_PARAM_RE_TMPL.format(prefix=re.escape(prefix)),
                    re.DOTALL,
                )
                param_matches = list(param_re.finditer(body))
                if not param_matches:
                    # Structural minimum: at least one matched-prefix parameter.
                    continue
                params: dict[str, str] = {}
                for p in param_matches:
                    params[p.group(1)] = p.group(2).strip()
                recovered.append(
                    {
                        "id": f"salvage_{len(recovered)}",
                        "name": name,
                        "arguments": json.dumps(params),
                    }
                )
                spans_to_strip.append(inv.span())
                if first_prefix is None:
                    first_prefix = prefix
            if recovered:
                logger.warning(
                    "Session %s: recovered %d native-format tool call(s) from "
                    "text content (markup leaked as text; first prefix=%r)",
                    session_id,
                    len(recovered),
                    first_prefix,
                )
                for start, end in reversed(spans_to_strip):
                    collected_content = collected_content[:start] + collected_content[end:]
                # Drop a now-empty outer container like <…tool_calls></…tool_calls>.
                collected_content = re.sub(
                    r"<[^\s<>/]*?tool_calls>\s*</[^\s<>/]*?tool_calls>",
                    "",
                    collected_content,
                ).strip()
                collected_tool_calls = recovered

        # --- Response analysis ---
        if not collected_tool_calls:
            # No tool calls — model has responded. Save and finish.
            if collected_content:
                await _save_turn_msg("assistant", collected_content)
            _round_latency_ms = int((time.monotonic() - _round_started_at) * 1000)
            logger.info(
                "agent.round session=%s round=%d model=%s latency_ms=%d tool_calls=0 content_chars=%d (final)",
                session_id,
                tool_round,
                _stream_current_model,
                _round_latency_ms,
                len(collected_content or ""),
            )
            session.emit_event(
                {
                    "type": "stream.done",
                    "usage": _last_usage.__dict__ if _last_usage else {},
                    "model": _stream_current_model,
                }
            )
            session.touch()
            session.termination_reason = "complete"
            return

        # --- Tool execution ---
        did_tool_calls = True

        # NOTE: We save the assistant message AFTER validation below,
        # so only validated tool_calls end up in the DB (prevents orphans).
        # The original collected_tool_calls may include hallucinated/malformed calls.

        # Stuck detection
        score, repeats = stuck.evaluate(collected_content, collected_tool_calls, tool_failures, registry)
        if repeats >= 3:
            logger.warning("Session %s stuck (score=%.1f, repeats=%d)", session_id, score, repeats)
            # Prefer asking the user over silent exhaustion. If ask_user is
            # active, direct the agent to call it with a concrete question;
            # otherwise fall back to the historical "summarize and stop".
            # If the LLM ignored the ask_user nudge for STUCK_ASK_USER_LIMIT
            # consecutive rounds, fall through to the break path — without
            # this cap the loop spins on each new round, emitting another
            # nudge and burning more LLM time.
            ask_user_available = "ask_user" in (active_tools or [])
            if ask_user_available and _stuck_ask_user_continues < STUCK_ASK_USER_LIMIT:
                _stuck_ask_user_continues += 1
                recent_tool_names = sorted({tc.get("name", "") for tc in (collected_tool_calls or [])})
                hint = (
                    "You appear to be stuck in a loop "
                    f"(recently used: {', '.join(recent_tool_names) or 'n/a'}). "
                    "Do NOT retry the same approach. Call ask_user with a specific "
                    "clarifying question that names what you tried, what failed, and "
                    "what you need from the user to proceed. After ask_user returns, "
                    "use the answer to pick a new strategy."
                )
                await asyncio.to_thread(db.add_message, session_id, "system", hint)
                # Don't break — let one more round run so the agent can ask.
                collected_content = ""
                collected_tool_calls = []
                continue
            if ask_user_available:
                # Hit the cap. Emit a final, distinct system message so the
                # transcript records why we gave up nudging, then break.
                logger.warning(
                    "Session %s stuck cap reached (%d consecutive nudges ignored), " "force-breaking loop",
                    session_id,
                    _stuck_ask_user_continues,
                )
                await asyncio.to_thread(
                    db.add_message,
                    session_id,
                    "system",
                    f"Stuck-detection nudged you {_stuck_ask_user_continues} "
                    "times to call ask_user and you did not. Summarize what "
                    "you have so far and stop.",
                )
            else:
                await asyncio.to_thread(
                    db.add_message,
                    session_id,
                    "system",
                    "You appear to be stuck in a loop. Summarize your progress and stop.",
                )
            session.termination_reason = "round_ceiling"
            break
        else:
            # Not stuck this round — reset the consecutive-nudge counter so a
            # later separate stuck episode gets the full nudge budget.
            _stuck_ask_user_continues = 0
            if repeats >= 1 and score > 0.3:
                _nudge_tool_names = sorted({tc.get("name", "") for tc in (collected_tool_calls or [])})
                _nudge_tool_str = ", ".join(_nudge_tool_names) if _nudge_tool_names else "unknown"
                await asyncio.to_thread(
                    db.add_message,
                    session_id,
                    "system",
                    f"You are repeating tool calls ({_nudge_tool_str}). "
                    "Do NOT retry the same operation. "
                    "Review what you have already accomplished and proceed to the next unfinished step.",
                )

        # Deduplicate tool calls (exact hash)
        seen_calls: set[str] = set()
        unique_calls = []
        for tc in collected_tool_calls:
            key = f"{tc['name']}:{_hash_args(tc.get('arguments', ''))}"
            if key in seen_calls:
                # Return stub for intra-round duplicate
                await _save_turn_msg("tool", "(duplicate call — see previous result)", tool_call_id=tc.get("id", ""))
                continue
            seen_calls.add(key)
            # Cross-round hard dedup: if this exact call already succeeded in a prior round,
            # return an informative stub so the model can use the known result without re-executing.
            # Non-idempotent tools (repl — a repeated `next(pages)` MUST run
            # twice; the mutated state lives in the kernel namespace, invisible
            # to the file-based invalidators) always re-execute.
            _dedup_tool = registry.get(tc["name"])
            _tool_idempotent = getattr(_dedup_tool, "idempotent", True) if _dedup_tool else True
            if key in _cross_round_calls and tc["name"] not in _CROSS_ROUND_DEDUP_EXCLUDED and _tool_idempotent:
                prior_round, prior_result = _cross_round_calls[key]
                stub = (
                    f"(already executed in round {prior_round} with identical arguments — "
                    f"prior result: {prior_result}. "
                    "Use this result and do not call again. "
                    "If you believe the state has changed, verify with file_read or glob instead.)"
                )
                await _save_turn_msg("tool", stub, tool_call_id=tc.get("id", ""))
                continue
            unique_calls.append(tc)

        # Semantic dedup for expensive tools (near-identical arguments)
        if len(unique_calls) > 1:
            by_name: dict[str, list[dict]] = {}
            for tc in unique_calls:
                by_name.setdefault(tc["name"], []).append(tc)
            final_calls: list[dict] = []
            for name, group in by_name.items():
                if name in _SEMANTIC_DEDUP_TOOLS and len(group) > 1:
                    kept = [group[0]]
                    for tc in group[1:]:
                        if any(_is_near_duplicate_call(tc, k, name) for k in kept):
                            logger.info("Semantic dedup: skipping near-duplicate %s call", name)
                            await _save_turn_msg(
                                "tool", "(near-duplicate call — see previous result)", tool_call_id=tc.get("id", "")
                            )
                        else:
                            kept.append(tc)
                    final_calls.extend(kept)
                else:
                    final_calls.extend(group)
            unique_calls = final_calls

        # --- H1: Filter out hallucinated tools (not in registry) ---
        # Track which call IDs got aliased so we can prefix the note onto the
        # eventual tool result below. A role=system mid-conversation note is
        # stripped by normalize_for_openrouter (and Ollama's one-system rule),
        # so the correction has to travel back on the tool-role message that
        # the provider keeps verbatim.
        aliased_by_id: dict[str, tuple[str, str]] = {}
        valid_calls = []
        for tc in unique_calls:
            tc["name"] = tc["name"].strip()
            aliased = _TOOL_ALIASES.get(tc["name"])
            # Only apply the alias if the target is registered, NOT disabled,
            # AND in the session's active_tools. Without the active-tools check,
            # an alias silently promotes a tool past the active-set gate (e.g.
            # scout didn't pick it, but the model hallucinated a matching name).
            # The is_disabled gate is defense-in-depth: the monotonic allowlist
            # already filters disabled, but an alias is exactly the kind of
            # back-channel that could resurrect one if a future refactor forgets.
            if aliased and registry.exists(aliased) and not registry.is_disabled(aliased) and aliased in active_tools:
                original_name = tc["name"]
                logger.info("tool.aliased: %s -> %s", original_name, aliased)
                tc["name"] = aliased
                aliased_by_id[tc.get("id", "")] = (original_name, aliased)
            if not registry.exists(tc["name"]):
                logger.warning("Hallucinated tool '%s' — not in registry", tc["name"])
                hint = _build_hallucinated_tool_hint(tc["name"], registry)
                full_error = f"Error: Tool '{tc['name']}' does not exist. {hint}"
                await _save_turn_msg(
                    "tool",
                    full_error,
                    tool_call_id=tc.get("id", ""),
                )
                # Mirror the full hint into the UI event — the user benefits
                # from seeing what the agent was told, not just "does not exist".
                session.emit_event(
                    {
                        "type": "tool.call",
                        "name": tc["name"],
                        "arguments": {},
                        "result": full_error,
                        "full_result": full_error,
                        "truncated": False,
                        "was_error": True,
                        "latency_ms": 0,
                    }
                )
                tool_failures.setdefault(tc["name"], []).append(_hash_args(tc.get("arguments", "")))
                stuck.mark_failure()
                continue
            valid_calls.append(tc)

        # --- C3: Safe JSON parsing for tool arguments ---
        parsed_calls = []
        for tc in valid_calls:
            raw_args = tc["arguments"]
            if isinstance(raw_args, str):
                try:
                    parsed_args = json.loads(raw_args) if raw_args else {}
                except (json.JSONDecodeError, ValueError) as e:
                    logger.warning("Malformed tool arguments for '%s': %s", tc["name"], e)
                    await _save_turn_msg(
                        "tool",
                        (
                            f"Error: Could not parse arguments for tool '{tc['name']}'. "
                            f"JSON decode error: {e}. Please provide valid JSON arguments."
                        ),
                        tool_call_id=tc.get("id", ""),
                    )
                    err_msg = f"Error: Malformed JSON arguments: {e}"
                    session.emit_event(
                        {
                            "type": "tool.call",
                            "name": tc["name"],
                            "arguments": {},
                            "result": err_msg,
                            "full_result": err_msg,
                            "truncated": False,
                            "was_error": True,
                            "latency_ms": 0,
                        }
                    )
                    tool_failures.setdefault(tc["name"], []).append(_hash_args(raw_args))
                    stuck.mark_failure()
                    continue
            else:
                parsed_args = raw_args if raw_args else {}

            # --- H4: Validate required parameters against tool schema ---
            tool_def = registry.get(tc["name"])
            if tool_def and tool_def.parameters:
                required_params = tool_def.parameters.get("required", [])
                missing = [p for p in required_params if p not in parsed_args]
                if missing:
                    logger.warning("Tool '%s' missing required params: %s", tc["name"], missing)
                    schema_props = tool_def.parameters.get("properties", {})
                    param_hints = {
                        p: schema_props[p].get("description", schema_props[p].get("type", "unknown"))
                        for p in missing
                        if p in schema_props
                    }
                    await _save_turn_msg(
                        "tool",
                        (
                            f"Error: Tool '{tc['name']}' is missing required parameters: "
                            f"{', '.join(missing)}. "
                            f"Parameter details: {json.dumps(param_hints)}. "
                            f"You MUST retry this tool call with all required parameters included."
                        ),
                        tool_call_id=tc.get("id", ""),
                    )
                    err_msg = f"Error: Missing required parameters: {', '.join(missing)}"
                    session.emit_event(
                        {
                            "type": "tool.call",
                            "name": tc["name"],
                            "arguments": _summarize_args(parsed_args),
                            "result": err_msg,
                            "full_result": err_msg,
                            "truncated": False,
                            "was_error": True,
                            "latency_ms": 0,
                        }
                    )
                    tool_failures.setdefault(tc["name"], []).append(_hash_args(tc.get("arguments", "")))
                    stuck.mark_failure()
                    continue

            parsed_calls.append({"tc": tc, "parsed_args": parsed_args})

        # Save assistant message with ONLY validated tool_calls (prevents DB orphans).
        # Persist round latency so post-hoc diagnosis (which model is slow,
        # which round burned the budget) can read it from the DB without
        # re-deriving from event logs.
        validated_tool_calls = [item["tc"] for item in parsed_calls]
        _round_latency_ms = int((time.monotonic() - _round_started_at) * 1000)
        await _save_turn_msg(
            "assistant",
            collected_content or "",
            tool_calls=json.dumps(validated_tool_calls) if validated_tool_calls else None,
            latency_ms=_round_latency_ms,
        )
        logger.info(
            "agent.round session=%s round=%d model=%s latency_ms=%d tool_calls=%d content_chars=%d",
            session_id,
            tool_round,
            _stream_current_model,
            _round_latency_ms,
            len(validated_tool_calls),
            len(collected_content or ""),
        )

        # Execute tools (only valid, parsed, and validated calls)
        context = {"session_id": session_id}
        if parsed_calls:
            # Announce before execution — tool.call below is emitted only
            # AFTER the tool returns (it carries the result), so without
            # this the user stares at a static badge for the entire
            # runtime of a slow bash/search call wondering if it's stuck.
            for item in parsed_calls:
                session.emit_event(
                    {
                        "type": "tool.start",
                        "name": item["tc"]["name"],
                        "arguments": _summarize_args(item["parsed_args"]),
                    }
                )
            results = await execute_tool_round(
                [{"name": item["tc"]["name"], "arguments": item["parsed_args"]} for item in parsed_calls],
                context=context,
                registry=registry,
            )
        else:
            results = []

        # Save results and emit events
        for item, result in zip(parsed_calls, results):
            tc = item["tc"]
            tool_meta = json.dumps(
                {
                    "was_error": result.was_error,
                    "latency_ms": result.latency_ms,
                }
            )
            # If this call had its name aliased, prefix the correction onto
            # the tool result so the model sees the rewrite on its next turn.
            # (A separate role=system note would be stripped by
            # normalize_for_openrouter; tool-role messages are preserved.)
            stored_content = result.content
            alias_pair = aliased_by_id.get(tc.get("id", ""))
            if alias_pair is not None:
                _orig, _new = alias_pair
                stored_content = f"[note: tool name aliased {_orig} → {_new}]\n" + stored_content
            # Harness-side mid-turn nudge: if this tool result matches a known
            # failure signature (bot detection, 4xx/5xx from public web,
            # SSRF block on a public-looking domain), append a one-shot hint
            # pointing at the skill that solves it. The hint piggybacks on the
            # tool-role message because role=system mid-conversation gets
            # stripped by provider normalization.
            from core.harness.nudges import evaluate as _nudge_eval

            nudge = _nudge_eval(result.tool_name, stored_content, nudges_fired)
            if nudge:
                stored_content = stored_content + "\n\n" + nudge
                logger.info("harness.nudge fired tool=%s pattern hint appended", result.tool_name)
            await _save_turn_msg(
                "tool",
                stored_content,
                tool_call_id=tc.get("id", ""),
                latency_ms=result.latency_ms,
                metadata=tool_meta,
            )
            event_data = {
                "type": "tool.call",
                "name": result.tool_name,
                "arguments": _summarize_args(item["parsed_args"]),
                "result": result.content[:500],
                "full_result": result.content[:5000],
                "truncated": len(result.content) > 500 or result.metadata.get("truncated", False),
                "was_error": result.was_error,
                "latency_ms": result.latency_ms,
            }
            if result.metadata:
                event_data["metadata"] = result.metadata
            session.emit_event(event_data)

            # Track failures and update stuck detector
            if result.was_error:
                tool_failures.setdefault(result.tool_name, []).append(_hash_args(tc.get("arguments", "")))
                stuck.mark_failure(tool_name=result.tool_name, args=item["parsed_args"])
            else:
                stuck.mark_success(tool_name=result.tool_name, args=item["parsed_args"])
                # Cache successful call for cross-round hard dedup.
                # Only successful results are cached — failed calls stay eligible for retry.
                if result.tool_name not in _CROSS_ROUND_DEDUP_EXCLUDED:
                    _cr_key = f"{result.tool_name}:{_hash_args(tc.get('arguments', ''))}"
                    _cross_round_calls[_cr_key] = (tool_round, result.content[:200])
                # A successful file mutation invalidates cached bash results: the
                # same command can now yield a different outcome (e.g. re-running a
                # script the agent just fixed). Without this, "edit → re-run same
                # command" loops short-circuit to the stale pre-edit failure.
                if result.tool_name in _STATE_MUTATING_TOOLS:
                    _purged = _invalidate_bash_dedup(_cross_round_calls)
                    if _purged:
                        logger.info(
                            "Cross-round dedup: cleared %d cached bash result(s) after %s",
                            _purged,
                            result.tool_name,
                        )
            # Semantic-streak observation (signals 8-10): records the result
            # body's "low info" status and hostname for the new search-spiral
            # / bot-wall / same-domain-grind signals. Cheap bookkeeping only.
            stuck.observe_result(
                tool_name=result.tool_name,
                args=item["parsed_args"],
                content=result.content,
                was_error=result.was_error,
            )

            # Build cumulative tool execution summary for reflect diagnostic
            ts = session.last_tool_summary
            entry = ts.setdefault(
                result.tool_name,
                {
                    "calls": 0,
                    "failures": 0,
                    "errors": [],
                    "total_latency_ms": 0,
                },
            )
            entry["calls"] += 1
            entry["total_latency_ms"] += result.latency_ms
            if result.was_error:
                entry["failures"] += 1
                err_preview = result.content[:500] if result.content else "unknown"
                if err_preview not in entry["errors"]:
                    entry["errors"].append(err_preview)

            # Dynamic tool expansion via discover_tools
            if result.tool_name == "discover_tools" and not result.was_error:
                _expand_tools_from_discovery(result.content, active_tools)

            # Inject a newly created/updated custom tool directly into active_tools
            # so it appears in the LLM schema on the next round without requiring
            # a separate discover_tools call.
            if result.tool_name in ("create_tool", "update_tool") and not result.was_error:
                _inject_created_tool(item["parsed_args"].get("name", ""), active_tools)

        session.touch()

        # Post-round cancellation checkpoint
        if session.cancel_requested:
            logger.info("Session %s: cancel requested after tool round %d", session_id, tool_round)
            session.termination_reason = "cancelled"
            return

        # ask_user pause: if this round called ask_user, stop the loop and wait.
        # The user's answer arrives as a new manager.prompt() call which starts a fresh turn.
        if session.waiting_for_input:
            logger.info("Session %s: ask_user posted — suspending agent loop", session_id)
            await _save_turn_msg("assistant", "I've asked you a question and am waiting for your response.")
            session.emit_event(
                {
                    "type": "stream.done",
                    "usage": _last_usage.__dict__ if _last_usage else {},
                    "model": effective_model,
                }
            )
            session.termination_reason = "complete"
            return

        # await_workers(suspend=True) pause: loop exits and parent suspends until
        # watched workers complete. Resume is automatic via _resume_from_workers().
        if session.waiting_for_workers:
            logger.info("Session %s: workers-dispatched — suspending agent loop", session_id)
            session.emit_event(
                {
                    "type": "stream.done",
                    "usage": _last_usage.__dict__ if _last_usage else {},
                    "model": effective_model,
                }
            )
            session.termination_reason = "complete"
            return

        collected_content = ""
        collected_tool_calls = []
        tool_round += 1

    # If we exit the tool loop without returning (max rounds hit, or stuck break),
    # make one final response call with tools=None to get a clean text answer.
    # Natural while-loop exhaustion wasn't tagged by an in-loop branch above —
    # classify it now so downstream hooks can tell "round ceiling" from "complete".
    if session.termination_reason is None:
        session.termination_reason = "round_ceiling"
    done_sent = False
    if did_tool_calls:
        logger.info("Tool loop ended, generating final response (tools=None)")
        payload = await asyncio.to_thread(
            compile_context,
            session_id=session_id,
            tool_schemas=None,  # no tools — force text response
            scout_report_text=scout_text,
            resource_status=resource_status,
            supports_vision=model_supports_vision,
            supports_audio=model_supports_audio,
            context_budget=session.context_budget_override or _model_budget or settings.context_budget,
            model_name=effective_model,
            turn_user_msg_id=_turn_user_msg_id,
        )
        messages = payload.messages
        _final_provider = client.resolve_provider(effective_model)
        if _final_provider in OPENAI_FORMAT_PROVIDERS:
            messages = normalize_for_openrouter(messages)
            messages = attach_cache_breakpoints(messages, effective_model, _final_provider, payload.static_prefix_chars)

        final_content = ""
        _final_retries = 0
        # Honor the tool loop's sticky failover: once a turn has failed over,
        # re-attempting the known-bad primary for the final response just
        # burns the backoff ladder again before landing on the same fallback.
        _final_tried_fallback = _tried_fallback and settings.fallback_model != ""
        _final_model = settings.fallback_model if _final_tried_fallback and settings.fallback_model else effective_model
        while True:  # final response retry loop
            final_content = ""
            _final_err: str | None = None
            try:
                async for event in client.chat_stream(
                    messages,
                    tools=None,
                    model=_final_model,
                    session_id=session_id,
                    session_created_at=_sched_created_at,
                    session_priority=_sched_priority,
                ):
                    if event.type == StreamEventType.TOKEN and event.content:
                        final_content += event.content
                        session.emit_event({"type": "stream.token", "content": event.content})
                    elif event.type == StreamEventType.USAGE and event.usage:
                        _last_usage = event.usage
                        await asyncio.to_thread(
                            db.add_token_usage,
                            session_id=session_id,
                            model=_final_model,
                            prompt_tokens=event.usage.prompt_tokens,
                            completion_tokens=event.usage.completion_tokens,
                            total_tokens=event.usage.total_tokens,
                            source="provider",
                            provider=client.resolve_provider(_final_model),
                            goal_id=session.active_goal_id,
                        )
                    elif event.type == StreamEventType.ERROR and event.error:
                        _final_err = event.error
                        break
            except Exception as e:
                _final_err = str(e)

            if _final_err is None:
                break  # success

            if _final_retries < len(_STREAM_BACKOFFS) and _is_stream_retryable(_final_err):
                wait = _STREAM_BACKOFFS[_final_retries]
                _final_retries += 1
                logger.warning(
                    "Final response stream error (attempt %d/3) in session %s, retrying in %ds: %s",
                    _final_retries,
                    session_id,
                    wait,
                    _final_err,
                )
                session.emit_event(
                    {"type": "stream.retry", "attempt": _final_retries, "wait": wait, "error": _final_err}
                )
                await asyncio.sleep(wait)
                continue

            # Same-provider different-model fallback is allowed here for the
            # same reason as the tool loop (see above).
            fallback = settings.fallback_model
            if fallback and not _final_tried_fallback and fallback != _final_model:
                _final_tried_fallback = True
                _final_retries = 0
                _final_model = fallback
                logger.warning(
                    "Final response retries exhausted for session %s, switching to fallback: %s",
                    session_id,
                    fallback,
                )
                session.emit_event({"type": "stream.fallback", "model": fallback})
                _fb2_provider = client.resolve_provider(fallback)
                if _fb2_provider in OPENAI_FORMAT_PROVIDERS:
                    messages = normalize_for_openrouter(messages)
                    # Re-run for the NEW model: flattens stale anthropic
                    # cache parts when the fallback isn't anthropic/*.
                    messages = attach_cache_breakpoints(messages, fallback, _fb2_provider, payload.static_prefix_chars)
                continue

            logger.error("Final response error: %s", _final_err)
            if final_content:
                await _save_turn_msg("assistant", final_content)
            session.emit_event({"type": "stream.error", "error": _final_err})
            break

        if _final_err is None:
            if final_content:
                await _save_turn_msg("assistant", final_content)
            session.emit_event(
                {
                    "type": "stream.done",
                    "usage": _last_usage.__dict__ if _last_usage else {},
                    "model": _final_model,
                }
            )
            done_sent = True

    if not done_sent:
        session.emit_event(
            {
                "type": "stream.done",
                "usage": _last_usage.__dict__ if _last_usage else {},
                "model": effective_model,
            }
        )


def _expand_tools_from_discovery(discovery_result: str, active_tools: list[str]) -> None:
    """Parse discover_tools output and insert into sorted active tools list."""
    # discover_tools returns markdown lines like "- **tool_name** [category]: description"
    import bisect
    import re

    for match in re.finditer(r"\*\*(\w+)\*\*", discovery_result):
        tool_name = match.group(1)
        registry = get_registry()
        if registry.exists(tool_name) and not registry.is_disabled(tool_name) and tool_name not in active_tools:
            bisect.insort(active_tools, tool_name)


def _inject_created_tool(tool_name: str, active_tools: list[str]) -> None:
    """Add a tool registered by create_tool/update_tool into the sorted active tools list.

    Mirrors _expand_tools_from_discovery so a newly minted custom tool enters
    the LLM schema on the very next round without a separate discover_tools call.
    """
    import bisect

    registry = get_registry()
    if tool_name and registry.exists(tool_name) and not registry.is_disabled(tool_name):
        if tool_name not in active_tools:
            bisect.insort(active_tools, tool_name)


def _build_hallucinated_tool_hint(bad_name: str, registry) -> str:
    """Build a corrective hint for a tool name that isn't in the registry.

    Combines the semantic discover() index with a difflib close-match pass
    over all known tool names. The close-match pass catches tiny typos /
    plausible-but-wrong suffixes (e.g. get_worker_output → get_worker_result)
    that the token-based search may rank lower than semantic siblings.

    Suggestion-only: the caller does NOT auto-substitute. The model must
    issue a fresh tool call to pick up the corrected name.
    """
    # kimi-k2.6 sometimes leaks its native function-call tokens (e.g.
    # <|tool_call_argument_begin|>) into the tool name field instead of using
    # the OpenAI tool_calls schema. Fuzzy-matching on these garbage strings
    # produces wrong suggestions, so bail early with a targeted correction.
    if "<|" in bad_name:
        return (
            "Your response contained a raw model-specific token in the tool name. "
            "Use the standard tool_calls API field: set `name` to the tool name "
            "(e.g. `file_write`) and `arguments` to a JSON string of parameters."
        )

    import difflib

    suggestions = registry.discover(bad_name, limit=3)
    semantic_names = [s.name for s in suggestions]

    all_names = [t.name for t in registry.enabled_tools() if t.name != bad_name]
    close = difflib.get_close_matches(bad_name, all_names, n=3, cutoff=0.7)

    # Promote close-match candidates to the front; de-dupe preserving order.
    ordered: list[str] = []
    for name in close + semantic_names:
        if name not in ordered:
            ordered.append(name)

    if not ordered:
        return (
            "No close match found in the active tool set. Call discover_tools "
            "with a keyword (e.g. the noun from your intent) to find the right tool."
        )

    top = ordered[0]
    top_def = registry._tools.get(top)
    top_desc = (top_def.description if top_def else "") or ""
    # Trim description so the hint stays readable.
    if len(top_desc) > 140:
        top_desc = top_desc[:137] + "..."

    others = ordered[1:3]
    others_str = (" Other possibilities: " + ", ".join(f"`{n}`" for n in others) + ".") if others else ""

    return (
        f"The correct tool is likely `{top}` — {top_desc} "
        f"Retry with name=`{top}` and the same arguments if they match its schema."
        f"{others_str}"
    )


def _build_resource_status(session_id: str, estimator, tool_round: int = 0, context_budget: int | None = None) -> str:
    """Build resource status for system prompt."""
    usage = db.get_session_usage(session_id)
    total_tokens = usage.get("total", 0)
    budget = context_budget if context_budget is not None else settings.context_budget
    pct = (total_tokens / budget * 100) if budget else 0
    remaining = settings.max_tool_rounds - tool_round
    base = (
        f"[RESOURCE STATUS] Session tokens: {total_tokens:,} ({pct:.0f}% of budget) | "
        f"Tool rounds remaining: {remaining}/{settings.max_tool_rounds}"
    )
    if remaining == 1:
        base += (
            "\nLAST ROUND (tools disabled): summarize what you finished and "
            "explicitly state what is unfinished. Do not attempt tool calls."
        )
    elif remaining == 2:
        base += (
            "\nCRITICAL: Next round has no tools. Write any deliverable file "
            "THIS round — the round after will be text-only synthesis."
        )
    elif 3 <= remaining <= 5:
        base += (
            "\nWARNING: Approaching round limit. Finish open tool work and "
            "begin writing any final deliverables (file_write) now."
        )
    return base
