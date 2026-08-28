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
from core.llm.budget import derive_max_output, derive_model_budget, ensure_model_known
from core.llm.client import get_llm_client
from core.llm.providers.salvage import salvage_tool_calls
from core.llm.router import OPENAI_FORMAT_PROVIDERS
from core.llm.semaphore import PRIORITY_ORCHESTRATOR, PRIORITY_WORKER
from core.llm.stream_ladder import stream_with_failover
from core.tools.executor import execute_tool_round
from core.tools.registry import get_registry
from db import models as db
from sessions.state import AgentSession

logger = logging.getLogger("pernix.agent")


# ---------------------------------------------------------------------------
# Stuck detection
# ---------------------------------------------------------------------------

_FILE_TOOLS = {"file_edit", "file_write", "file_read", "file_append"}
# Tools whose success changes what an identical follow-up call would do.
_MUTATING_TOOLS = frozenset({"file_write", "file_edit", "multiedit", "repl"})

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


_FALSIFIED_FIT_RE = re.compile(
    r"(?:\bno (?:match|formula|fit|clean pattern|linear (?:formula|pattern|fit))\b"
    r"|\bdoes not (?:fit|match|reproduce)\b"
    r"|\binconsistent\b"
    r"|\bFAIL(?:ED)?\b"
    r"|\b0 solutions\b"
    r"|\bnone (?:fit|match(?:ed)?)\b"
    r"|\bunsolved\b)",
    re.IGNORECASE,
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
    # Monotonic counter bumped by every successful workspace mutation
    # (file_write/file_edit/multiedit/repl). Signal 2 compares against it:
    # an identical call is only a cycle when NOTHING changed in between —
    # "edit the script, rerun the same command" is iteration, not a loop
    # (field case c93232a0521b: 4 false 'repeating tool calls' nudges on an
    # edit→rerun solver workflow with zero failed calls).
    mutation_epoch: int = 0
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
    # Signal 12 state: windowed reads/greps of the same file this turn.
    # ARC-3 field pattern: 12 sessions ground obfuscated 2,800-4,500-line
    # game sources through 50KB-truncated file_read windows and grep, while
    # rlm_process — built for exactly that — sat unused (1 use in 13
    # sessions, and it was the campaign's best single move). ≥5 windowed
    # passes over one file queues a one-time RLM pointer.
    file_read_counts: dict = field(default_factory=dict)  # path → count
    # Signal 13 state: consecutive repl/bash results that each falsified
    # another candidate fit (>=2 marker matches per result body).
    falsified_fit_streak: int = 0
    pending_hints: list = field(default_factory=list)  # one-time system hints

    def evaluate(self, content: str, tool_calls: list[dict] | None, tool_failures: dict, registry) -> tuple[float, int]:
        """Evaluate stuck signals. Returns (score 0-1, repeat_count)."""
        score = 0.0

        # Signal 1: Exact content repeat
        if content and content in self.content_history:
            score += 0.5
            self.behavioral_flags.add("content_repeat")

        # Signal 2: Tool call cycle — same calls with NO intervening
        # workspace mutation. A verbatim rerun after a file edit or repl
        # execution is legitimate iteration and must not count.
        if tool_calls:
            sig = tuple(sorted(f"{tc.get('name','')}:{_hash_args(tc.get('arguments',''))}" for tc in tool_calls))
            if any(s == sig and e == self.mutation_epoch for s, e in self.tool_call_history):
                score += 0.4
                self.behavioral_flags.add("tool_cycle")
            self.tool_call_history.append((sig, self.mutation_epoch))

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

        # Signal 12: Big-file grind — the same file read/grepped over and
        # over (windowed reads evade every exact-args check because offset
        # changes each call). Not a stuck score: the agent IS making
        # progress, just expensively. Queues a one-time rlm_process pointer
        # instead of a score bump.
        if tool_calls:
            import json as _json

            for tc in tool_calls:
                name = tc.get("name")
                if name not in ("file_read", "grep", "bash"):
                    continue
                try:
                    args = _json.loads(tc.get("arguments") or "{}")
                except (ValueError, TypeError):
                    continue
                if name == "bash":
                    # Agents read big files through bash (cat/sed/grep/head),
                    # which the tool-level counters never saw — the ARC sweep
                    # found 21 sessions grinding sources this way with the
                    # rlm hint unable to fire. Count the first path-looking
                    # token in read-ish commands.
                    import re as _re

                    cmd = args.get("command") or ""
                    if not _re.search(r"\b(?:cat|sed|grep|head|tail|less|awk)\b", cmd):
                        continue
                    m = _re.search(r"[\w./-]+\.(?:py|md|json|txt|log|csv)\b", cmd)
                    path = m.group(0) if m else ""
                else:
                    path = args.get("path") or args.get("file") or ""
                if not path:
                    continue
                self.file_read_counts[path] = self.file_read_counts.get(path, 0) + 1
                if self.file_read_counts[path] == 5 and "rlm_hint" not in self.behavioral_flags:
                    from config import settings as _settings

                    if getattr(_settings, "rlm_enabled", False):
                        self.behavioral_flags.add("rlm_hint")
                        self.pending_hints.append(
                            f"[harness hint] You have read/grepped '{path}' 5+ times "
                            "this turn. For whole-file analysis (summarize, extract "
                            "structure/mechanics, answer questions across all of it), "
                            "one rlm_process call handles the entire file and returns "
                            "the digest — usually cheaper than more windowed reads."
                        )

        # Signal 13: Hypothesis grind — a long unbroken streak of repl/bash
        # results that each falsify another candidate fit. ARC-2 field case
        # (c00f6c4db9ff): ~30 rounds of "no match / no formula / FAIL" over
        # per-object column formulas while the answer lived in a different
        # hypothesis CLASS entirely (a chained path — each element placed
        # relative to the previous one). Signal 12 watches file re-reads and
        # never fired. Not a stuck score: the agent is working, just inside
        # the wrong class. Queue a one-time class-escalation pointer.
        if self.falsified_fit_streak >= 8 and "hypothesis_grind_hint" not in self.behavioral_flags:
            self.behavioral_flags.add("hypothesis_grind_hint")
            self.pending_hints.append(
                "[harness hint] Your last several computational checks each falsified "
                "another candidate fit. Before more parameter search inside the same "
                "family, consider changing the CLASS of hypothesis: per-element rules "
                "-> relational rules (pairs/neighbors) -> sequential/recursive rules "
                "(each element depends on the previous output state) -> global "
                "constraint rules. Cheap structure probes often reveal the class in "
                "one pass: adjacency/chaining between output elements, symmetry, "
                "count conservation. Rendering the data another way (transpose, "
                "side-by-side panels) beats re-fitting."
            )

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
        # Signal 13 bookkeeping: falsified-fit streak over compute results.
        # Errors neither extend nor reset the streak — a traceback mid-grind
        # is not evidence the hypothesis class changed.
        if tool_name in ("repl", "bash") and not was_error:
            if len(_FALSIFIED_FIT_RE.findall(content or "")) >= 2:
                self.falsified_fit_streak += 1
            else:
                self.falsified_fit_streak = 0

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
        if tool_name in _MUTATING_TOOLS:
            self.mutation_epoch += 1
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
# Forced follow-up (spec Feature 9): keep an under-ambitious agent working
# ---------------------------------------------------------------------------

_FUTURE_INTENT_RE = re.compile(
    r"\b(?:i(?:'ll| will)|let me|i'?m going to|i am going to|next,? i|now i(?:'ll| will)"
    r"|proceed(?:ing)? to|going to (?:start|run|write|create|implement|check|fix|update))\b",
    re.IGNORECASE,
)
# A reply that ends by offering help is a finished answer, not abandoned work.
_COURTESY_CLOSER_RE = re.compile(
    r"let me know|feel free|if you (?:need|want|have|would like)|happy to help" r"|any (?:other )?questions",
    re.IGNORECASE,
)


def _announces_future_work(text: str) -> bool:
    """True when the reply's TAIL promises more work instead of doing it.

    Scoped to the last two sentences on purpose: an "I'll" three paragraphs
    up followed by a completed deliverable must not trigger. A trailing
    question or a courtesy closer means the model handed the turn back
    deliberately.
    """
    stripped = (text or "").strip()
    if not stripped or stripped.endswith("?"):
        return False
    sentences = re.split(r"(?<=[.!\n])\s+", stripped)
    tail = " ".join(sentences[-2:])[-300:]
    if _COURTESY_CLOSER_RE.search(tail):
        return False
    return bool(_FUTURE_INTENT_RE.search(tail))


def _build_followup_nudge(session_id: str, content: str, attempt: int, cap: int) -> str:
    """Data-driven nudge: name the unfinished thing, never a generic 'continue'.

    Runs in a thread (DB read)."""
    target = ""
    try:
        goal = db.get_active_goal(session_id)
        if goal and goal.get("status") == "active":
            target = f"The active goal is still open: {(goal.get('objective') or '')[:200]}"
    except Exception:
        pass
    if not target:
        window = content[-400:]
        m = _FUTURE_INTENT_RE.search(window)
        quoted = window[m.start() :][:160].strip() if m else content.strip()[-160:]
        target = f'You announced: "{quoted}"'
    return (
        f"[forced follow-up {attempt}/{cap}] You stopped calling tools but the turn "
        f"looks unfinished. {target}. Do that work NOW with concrete tool calls — or, "
        "if the task genuinely is complete, state that plainly and why no further "
        "action is needed."
    )


def _record_followup_outcome(session: AgentSession, session_id: str, acted: bool) -> None:
    """One aggregate ledger row: did the nudge produce tool calls?

    scout_signals (signal_type="forced_followup", subject="global") —
    successes = the agent acted, failures = it re-idled anyway. A week of
    live traffic answers "is this feature earning its keep" with one query,
    and a rising failure share is the adaptive layer's cue to narrow the
    trigger. Runs in a thread (DB write); never blocks the turn.
    """
    try:
        db.upsert_signal(
            "forced_followup",
            "global",
            delta_successes=1 if acted else 0,
            delta_failures=0 if acted else 1,
        )
    except Exception as e:
        logger.debug("forced_followup outcome record failed: %s", e)
    session.emit_event(
        {
            "type": "turn.forced_followup_outcome",
            "outcome": "acted" if acted else "re_idled",
        }
    )
    logger.info(
        "Session %s: forced follow-up outcome=%s",
        session_id,
        "acted" if acted else "re_idled",
    )


# ---------------------------------------------------------------------------
# Tool-call admission
# ---------------------------------------------------------------------------

# JSON-schema scalar type → accepted Python types. bool is checked before int
# everywhere below because bool subclasses int.
_JSON_TYPE_CHECKS: dict[str, tuple] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


def _coerce_json_type(value, expected: str):
    """Try to convert `value` to the schema type. Returns (ok, new_value).

    Only the benign, unambiguous conversions models actually produce: numeric
    strings for number/integer, "true"/"false" for booleans, scalars for
    string parameters. Anything else fails and the caller rejects the call.
    """
    try:
        if expected == "integer" and isinstance(value, str) and value.strip().lstrip("+-").isdigit():
            return True, int(value.strip())
        if expected == "integer" and isinstance(value, float) and value.is_integer():
            return True, int(value)
        if expected == "number" and isinstance(value, str):
            return True, float(value.strip())
        if expected == "boolean" and isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return True, value.strip().lower() == "true"
        if expected == "string" and isinstance(value, (int, float, bool)):
            return True, json.dumps(value) if isinstance(value, bool) else str(value)
    except (ValueError, TypeError):
        pass
    return False, value


def _validate_arg_types(tool_def, parsed_args: dict, notes) -> str | None:
    """Validate provided args against the tool's JSON schema, in place.

    Three outcomes per argument: passes as-is; coerced (noted via `notes`);
    or uncoercible — the whole call is rejected by returning an error string.
    Unknown parameters are dropped with a note instead of reaching the tool,
    where they would either TypeError or be silently swallowed.
    """
    props = (tool_def.parameters or {}).get("properties") or {}
    if not props:
        return None

    unknown = [k for k in parsed_args if k not in props]
    if unknown and (tool_def.parameters or {}).get("additionalProperties") is not True:
        for k in unknown:
            parsed_args.pop(k, None)
        notes(f"[note: ignored unknown parameter(s): {', '.join(sorted(unknown))}]")

    coerced: list[str] = []
    for key, value in list(parsed_args.items()):
        spec = props.get(key, {})
        expected = spec.get("type")
        # Union types ({"type": ["string","null"]}) and typeless properties:
        # accept as-is — precision there isn't worth false rejections.
        if not isinstance(expected, str) or expected not in _JSON_TYPE_CHECKS:
            continue
        if value is None:
            continue  # let the tool's own default/None handling decide
        accepted = _JSON_TYPE_CHECKS[expected]
        if isinstance(value, bool) and expected not in ("boolean", "string"):
            ok, new_value = False, value
        elif isinstance(value, accepted):
            ok, new_value = True, value
        else:
            ok, new_value = _coerce_json_type(value, expected)
            if ok:
                coerced.append(f"{key} ({type(value).__name__}→{expected})")
        if not ok:
            return f"parameter '{key}' expects {expected}, got {type(value).__name__} ({str(value)[:80]!r})"
        parsed_args[key] = new_value

        # Enum membership — a wrong enum string used to reach the tool and
        # burn a round on its "invalid value" error. Checked after coercion
        # so a numeric-string enum member still matches.
        enum_vals = spec.get("enum")
        if isinstance(enum_vals, list) and enum_vals and new_value not in enum_vals:
            allowed = ", ".join(repr(v) for v in enum_vals[:12])
            return f"parameter '{key}' must be one of [{allowed}], got {str(new_value)[:80]!r}"

        # Array item types — one wrong-typed element ([\"5\"] for integers)
        # otherwise TypeErrors inside the tool. Scalar item schemas only;
        # nested objects/arrays stay the tool's own concern.
        if expected == "array":
            item_type = (spec.get("items") or {}).get("type")
            if isinstance(item_type, str) and item_type in _JSON_TYPE_CHECKS:
                item_accepted = _JSON_TYPE_CHECKS[item_type]
                new_items: list = []
                items_coerced = False
                for i, item in enumerate(new_value):
                    if isinstance(item, bool) and item_type not in ("boolean", "string"):
                        item_ok = False
                    elif isinstance(item, item_accepted):
                        new_items.append(item)
                        continue
                    else:
                        item_ok, item = _coerce_json_type(item, item_type)
                        items_coerced = items_coerced or item_ok
                        if item_ok:
                            new_items.append(item)
                            continue
                    return (
                        f"parameter '{key}' expects an array of {item_type}; "
                        f"element {i} is {type(new_value[i]).__name__} ({str(new_value[i])[:60]!r})"
                    )
                if items_coerced:
                    parsed_args[key] = new_items
                    coerced.append(f"{key}[] (items→{item_type})")
    if coerced:
        notes(f"[note: coerced parameter type(s): {', '.join(coerced)}]")
    return None


class _ToolCallGate:
    """Everything that stands between a model's tool calls and the executor.

    One turn, one gate. Four filters run in order, and the order matters:

      1. intra-round exact dedup — the same call twice in one response
      2. cross-round exact dedup — a call this turn already answered
      3. semantic dedup — near-identical args on expensive tools
      4. correction + validation — alias known hallucinations, reject unknown
         names, parse the JSON arguments, check required parameters

    Dedup precedes validation because a duplicate of a *bad* call should be
    answered by the dedup stub, not produce two identical error messages.

    Every rejection writes a tool-role message (the model's only channel back
    — a role=system note gets stripped by normalize_for_openrouter and by
    Ollama's one-system rule), emits the matching tool.call event so the UI
    shows what the agent was told, records the failure for stuck detection,
    and drops the call. Nothing reaches the executor unvalidated, and the
    assistant message the caller persists carries only what survived, so the
    transcript never gains an orphaned tool_call id.
    """

    def __init__(self, *, registry, session: AgentSession, save_turn_msg, stuck, tool_failures: dict[str, list[str]]):
        self._registry = registry
        self._session = session
        self._save = save_turn_msg
        self._stuck = stuck
        self._tool_failures = tool_failures
        # hash → (round_num, truncated_result) for the cross-round hard dedup.
        self._cross_round: dict[str, tuple[int, str]] = {}
        # call id → correction notes (alias rewrites, coercions, dropped
        # params). Reset per admit(); read back by _record_round_results.
        self._notes: dict[str, list[str]] = {}

    async def admit(self, calls: list[dict], active_tools: list[str]) -> tuple[list[dict], dict[str, list[str]]]:
        """Return (executable calls, correction notes keyed by tool_call id).

        Each executable entry is {"tc": <raw call>, "parsed_args": <dict>}.
        Notes record what the gate rewrote (aliased name, coerced argument
        types, stripped unknown parameters); the caller prefixes them onto the
        eventual tool result so the model sees its call was corrected — a
        mid-conversation role=system note would be stripped by provider
        normalization, tool-role messages survive.
        """
        self._notes: dict[str, list[str]] = {}
        unique = await self._dedup(calls)
        unique = await self._semantic_dedup(unique)
        valid = await self._correct_names(unique, active_tools)
        return await self._parse_and_validate(valid), self._notes

    def _note(self, tc: dict, text: str) -> None:
        self._notes.setdefault(tc.get("id", ""), []).append(text)

    def _emit_intercepted(self, name: str, action: str, reason: str) -> None:
        """tool.call.intercepted: the gate acted on a call before execution.

        action ∈ aliased | coerced | stripped_params | rejected. Rejections
        also emit the usual tool.call error event (via _reject); this event is
        the gate-level signal the UI/adaptive layer can aggregate."""
        self._session.emit_event(
            {
                "type": "tool.call.intercepted",
                "name": name,
                "action": action,
                "reason": reason[:300],
            }
        )

    def remember_success(self, tool_name: str, raw_args, round_num: int, content: str) -> None:
        """Cache a successful call so an identical one next round short-circuits.

        Only successes are cached — a failed call stays eligible for retry.
        """
        if tool_name not in _CROSS_ROUND_DEDUP_EXCLUDED:
            self._cross_round[f"{tool_name}:{_hash_args(raw_args)}"] = (round_num, content[:200])
        # A successful file mutation invalidates cached bash results: the same
        # command can now yield a different outcome (e.g. re-running a script
        # the agent just fixed). Without this, "edit → re-run same command"
        # loops short-circuit to the stale pre-edit failure.
        if tool_name in _STATE_MUTATING_TOOLS:
            purged = _invalidate_bash_dedup(self._cross_round)
            if purged:
                logger.info("Cross-round dedup: cleared %d cached bash result(s) after %s", purged, tool_name)

    # -- filters ------------------------------------------------------------

    async def _dedup(self, calls: list[dict]) -> list[dict]:
        seen: set[str] = set()
        kept: list[dict] = []
        for tc in calls:
            key = f"{tc['name']}:{_hash_args(tc.get('arguments', ''))}"
            if key in seen:
                await self._save("tool", "(duplicate call — see previous result)", tool_call_id=tc.get("id", ""))
                continue
            seen.add(key)
            # Non-idempotent tools (repl — a repeated `next(pages)` MUST run
            # twice; the mutated state lives in the kernel namespace, invisible
            # to the file-based invalidators) always re-execute.
            tool_def = self._registry.get(tc["name"])
            idempotent = getattr(tool_def, "idempotent", True) if tool_def else True
            if key in self._cross_round and tc["name"] not in _CROSS_ROUND_DEDUP_EXCLUDED and idempotent:
                prior_round, prior_result = self._cross_round[key]
                await self._save(
                    "tool",
                    (
                        f"(already executed in round {prior_round} with identical arguments — "
                        f"prior result: {prior_result}. "
                        "Use this result and do not call again. "
                        "If you believe the state has changed, verify with file_read or glob instead.)"
                    ),
                    tool_call_id=tc.get("id", ""),
                )
                continue
            kept.append(tc)
        return kept

    async def _semantic_dedup(self, calls: list[dict]) -> list[dict]:
        """Drop near-identical calls to expensive tools (same model + images,
        different prompt wording)."""
        if len(calls) <= 1:
            return calls
        by_name: dict[str, list[dict]] = {}
        for tc in calls:
            by_name.setdefault(tc["name"], []).append(tc)
        out: list[dict] = []
        for name, group in by_name.items():
            if name not in _SEMANTIC_DEDUP_TOOLS or len(group) <= 1:
                out.extend(group)
                continue
            kept = [group[0]]
            for tc in group[1:]:
                if any(_is_near_duplicate_call(tc, k, name) for k in kept):
                    logger.info("Semantic dedup: skipping near-duplicate %s call", name)
                    await self._save(
                        "tool", "(near-duplicate call — see previous result)", tool_call_id=tc.get("id", "")
                    )
                else:
                    kept.append(tc)
            out.extend(kept)
        return out

    async def _correct_names(self, calls: list[dict], active_tools: list[str]) -> list[dict]:
        """H1: rewrite known hallucinated names, reject unknown ones.

        Aliases are recorded as notes keyed by call id so the caller can prefix
        the correction onto the eventual tool result — a role=system
        mid-conversation note is stripped by normalize_for_openrouter (and
        Ollama's one-system rule), so the correction has to travel back on the
        tool-role message that the provider keeps verbatim.
        """
        valid: list[dict] = []
        for tc in calls:
            tc["name"] = tc["name"].strip()
            aliased = _TOOL_ALIASES.get(tc["name"])
            # Only apply the alias if the target is registered, NOT disabled,
            # AND in the session's active_tools. Without the active-tools check,
            # an alias silently promotes a tool past the active-set gate (e.g.
            # scout didn't pick it, but the model hallucinated a matching name).
            # The is_disabled gate is defense-in-depth: the monotonic allowlist
            # already filters disabled, but an alias is exactly the kind of
            # back-channel that could resurrect one if a future refactor forgets.
            if (
                aliased
                and self._registry.exists(aliased)
                and not self._registry.is_disabled(aliased)
                and aliased in active_tools
            ):
                original_name = tc["name"]
                logger.info("tool.aliased: %s -> %s", original_name, aliased)
                tc["name"] = aliased
                self._note(tc, f"[note: tool name aliased {original_name} → {aliased}]")
                self._emit_intercepted(aliased, "aliased", f"model called '{original_name}'")
            if not self._registry.exists(tc["name"]):
                logger.warning("Hallucinated tool '%s' — not in registry", tc["name"])
                hint = _build_hallucinated_tool_hint(tc["name"], self._registry)
                full_error = f"Error: Tool '{tc['name']}' does not exist. {hint}"
                # The user benefits from seeing the full hint the agent was
                # given, not just "does not exist" — so the event carries it too.
                self._emit_intercepted(tc["name"], "rejected", "unknown tool name")
                await self._reject(tc, transcript_msg=full_error, event_msg=full_error, event_args={})
                continue
            valid.append(tc)
        return valid

    async def _parse_and_validate(self, calls: list[dict]) -> list[dict]:
        """C3 (JSON arguments parse) + H4 (required parameters present)."""
        parsed_calls: list[dict] = []
        for tc in calls:
            raw_args = tc["arguments"]
            if isinstance(raw_args, str):
                try:
                    parsed_args = json.loads(raw_args) if raw_args else {}
                except (json.JSONDecodeError, ValueError) as e:
                    logger.warning("Malformed tool arguments for '%s': %s", tc["name"], e)
                    await self._reject(
                        tc,
                        transcript_msg=(
                            f"Error: Could not parse arguments for tool '{tc['name']}'. "
                            f"JSON decode error: {e}. Please provide valid JSON arguments."
                        ),
                        event_msg=f"Error: Malformed JSON arguments: {e}",
                        event_args={},
                    )
                    continue
            else:
                parsed_args = raw_args if raw_args else {}

            tool_def = self._registry.get(tc["name"])
            if tool_def and tool_def.parameters:
                missing = [p for p in tool_def.parameters.get("required", []) if p not in parsed_args]
                if missing:
                    logger.warning("Tool '%s' missing required params: %s", tc["name"], missing)
                    schema_props = tool_def.parameters.get("properties", {})
                    param_hints = {
                        p: schema_props[p].get("description", schema_props[p].get("type", "unknown"))
                        for p in missing
                        if p in schema_props
                    }
                    await self._reject(
                        tc,
                        transcript_msg=(
                            f"Error: Tool '{tc['name']}' is missing required parameters: "
                            f"{', '.join(missing)}. "
                            f"Parameter details: {json.dumps(param_hints)}. "
                            f"You MUST retry this tool call with all required parameters included."
                        ),
                        event_msg=f"Error: Missing required parameters: {', '.join(missing)}",
                        event_args=_summarize_args(parsed_args),
                    )
                    continue

                # Argument type validation against the LIVE schema (spec
                # Feature 3, stage 1). Wrong-typed args used to reach the tool
                # and fail there — one whole round burned on a TypeError-shaped
                # error. Coerce the benign cases (numeric strings, "true");
                # reject the uncoercible with the expected type spelled out so
                # the model can fix the call in the same round.
                _notes_before = len(self._notes.get(tc.get("id", ""), []))
                type_error = _validate_arg_types(tool_def, parsed_args, notes=lambda t: self._note(tc, t))
                if type_error:
                    self._emit_intercepted(tc["name"], "rejected", type_error)
                    await self._reject(
                        tc,
                        transcript_msg=(
                            f"Error: Tool '{tc['name']}' argument type mismatch — {type_error}. "
                            "Retry with correctly typed arguments."
                        ),
                        event_msg=f"Error: Argument type mismatch: {type_error}",
                        event_args=_summarize_args(parsed_args),
                    )
                    continue
                for _new_note in self._notes.get(tc.get("id", ""), [])[_notes_before:]:
                    action = "stripped_params" if "unknown parameter" in _new_note else "coerced"
                    self._emit_intercepted(tc["name"], action, _new_note)

            parsed_calls.append({"tc": tc, "parsed_args": parsed_args})
        return parsed_calls

    async def _reject(self, tc: dict, *, transcript_msg: str, event_msg: str, event_args: dict) -> None:
        """Refuse one call: tell the model, tell the UI, count it as a failure."""
        await self._save("tool", transcript_msg, tool_call_id=tc.get("id", ""))
        self._session.emit_event(
            {
                "type": "tool.call",
                "name": tc["name"],
                "arguments": event_args,
                "result": event_msg,
                "full_result": event_msg,
                "truncated": False,
                "was_error": True,
                "latency_ms": 0,
            }
        )
        self._tool_failures.setdefault(tc["name"], []).append(_hash_args(tc.get("arguments", "")))
        self._stuck.mark_failure()


# ---------------------------------------------------------------------------
# Compaction
# ---------------------------------------------------------------------------


class _CompactionController:
    """One turn's compaction budget, and the three ways a turn spends it.

    The state machine explicitly models repeated PROCESSING↔COMPACTING
    round-trips per turn (compaction_count in the state log); the old
    single-shot boolean meant a long turn that needed a second compaction died
    with compaction_failed even when the deliverable was one round away. Allow
    up to ATTEMPT_LIMIT, but require each compaction to actually shrink the
    context — `stalled()` — so a no-op compactor can't burn all attempts in a
    tight loop.

    The three callers differ only in why they fired and whether a failed
    compaction should leave the session parked in COMPACTING:

      critical   — utilization over the critical threshold before the call.
                   Stays in COMPACTING on failure; the caller breaks the turn
                   with compaction_failed and _finalize_turn reads the state.
      proactive  — the compiler's needs_compaction flag, fired once per turn
                   before anything is wrong. Always returns to PROCESSING: no
                   one downstream is going to classify this as a failure.
      overflow   — the provider itself rejected the request as too long.
                   Same parking rule as critical.
    """

    ATTEMPT_LIMIT = 3

    def __init__(self, session: AgentSession, session_id: str):
        self._session = session
        self._session_id = session_id
        self.attempts = 0
        self._tokens_before_last: int | None = None

    @property
    def exhausted(self) -> bool:
        return self.attempts >= self.ATTEMPT_LIMIT

    def stalled(self, token_count: int) -> bool:
        """True when the last compaction failed to shrink the context.

        Retrying the compactor after it moved nothing is wishful; the caller
        bails to compaction_failed rather than burning the remaining attempts.
        """
        return self._tokens_before_last is not None and token_count >= self._tokens_before_last * 0.95

    async def run(
        self,
        payload,
        *,
        transition_reason: str,
        event_reason: str | None = None,
        restore_state_on_failure: bool = False,
    ) -> bool:
        """Compact once. Returns whether the compactor actually ran."""
        from sessions import state_v2 as _sv2

        event: dict = {"type": "context.compacting"}
        if event_reason:
            event["reason"] = event_reason
        self._session.emit_event(event)
        try:
            _sv2.transition(self._session, _sv2.SessionStateV2.COMPACTING, transition_reason)
        except Exception as _e:
            logger.error("%s transition failed: %s", transition_reason, _e)

        self._tokens_before_last = payload.token_count
        # history_budget is what the compiler actually reserved for history
        # after the fixed prefix and the output reservation. Without it the
        # compactor falls back to a fraction-of-budget heuristic and keeps a
        # different amount than the compiler will accept on the next compile.
        compacted = await compact_with_llm(
            self._session_id,
            payload.messages,
            history_budget=payload.history_budget,
        )
        self.attempts += 1
        self._session.touch()  # keep the reaper honest — COMPACTING can take seconds
        if compacted or restore_state_on_failure:
            try:
                _sv2.transition(self._session, _sv2.SessionStateV2.PROCESSING, "compact-done")
            except Exception as _e:
                logger.error("compact-done (%s) transition failed: %s", transition_reason, _e)
        return compacted


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

    Per turn: persist the user message, resolve the tool surface, then drive
    the tool loop — compile context, stream, salvage/validate/execute tool
    calls, repeat — until the model answers, suspends, or runs out of rounds.
    A loop that ended on tool results gets one final tools=None call so the
    turn closes with text addressed to the user.

    The loop body stays readable by delegating each concern to a collaborator
    that owns its own rules: `_ToolCallGate` (what may execute),
    `_CompactionController` (the turn's compaction budget), `StuckDetector`
    (are we going in circles), and `core.llm.stream_ladder` (retry/fallback,
    shared with the final-answer call). What remains here is the control flow
    those pieces hang off, and the exit classification —
    session.termination_reason — that every downstream hook reads.
    """
    registry = get_registry()
    client = get_llm_client()
    estimator = get_estimator()

    _sched_priority, _sched_created_at = await _scheduling_identity(session, session_id)
    await _resolve_active_goal(session, session_id)

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

    # Tool surface for this turn: scout's picks, widened and then narrowed by
    # rules the scout does not get to overrule.
    scout_text, active_tools = _resolve_tool_surface(session, session_id, registry)

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

    # Model-derived context budget (audit P2) and output cap: settings values
    # are fallbacks, not global truths — a 1M-context model should not
    # silently run at the fallback size. Derivation lives in
    # core/llm/budget.py (shared with the context introspection endpoint so
    # the status bar and the agent agree). Registry catalog lookup, no
    # network; both honor settings.context_auto.
    #
    # A model pulled onto the host after startup isn't in the registry yet,
    # which silently drops the budget to the manual fallback — small enough
    # to make every turn die in the compiler. Refresh once before deriving.
    if await ensure_model_known(effective_model):
        effective_model, model_supports_vision, model_supports_audio = _resolve_effective_model()
        _last_effective_model = effective_model
    _model_budget: int | None = derive_model_budget(effective_model)
    _max_output: int = derive_max_output(effective_model)

    # Tool loop state
    stuck = StuckDetector()
    tool_failures: dict[str, list[str]] = {}
    nudges_fired: set[str] = set()  # one harness nudge per pattern per turn
    gate = _ToolCallGate(
        registry=registry,
        session=session,
        save_turn_msg=_save_turn_msg,
        stuck=stuck,
        tool_failures=tool_failures,
    )
    did_tool_calls = False
    compaction = _CompactionController(session, session_id)
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

    # Round-cap continuation (ARC-3 sweep finding #2): the 100-round ceiling
    # became the binding constraint on deep work — agents burn cognition
    # budgeting rounds and abandon debuggable paths near the cap. A turn that
    # exhausts its rounds while healthy (tools ran, no error, no stuck spiral)
    # gets settings.round_cap_auto_continue fresh budgets, mirroring the
    # length-truncation continuation above.
    _round_cap_continues = 0

    # Forced follow-ups used this turn (spec Feature 9). Bounded by
    # settings.forced_followup_max_per_turn so a genuinely finished task is
    # never looped. _followup_pending tracks a fired nudge whose outcome
    # (acted / re-idled) hasn't been recorded yet.
    _forced_followups = 0
    _followup_pending = False

    # Last compiled prompt size (F13, field case 17683100ecf8): the only
    # token number the window actually constrains. One round stale by
    # construction — the status is built before this round's compile.
    _last_context_tokens: int | None = None

    # Surface RLM runs that ended after their turn died. A cancel or budget
    # teardown abandons the in-flight rlm_process dispatch, so the engine's
    # result — partial answer, iteration count, continue_from pointer — lands
    # nowhere (field case 2ccb9af9: 88 iterations visible only as a sidebar
    # "failed" chip). One compact system note per orphaned depth-0 run.
    if getattr(settings, "rlm_enabled", False):
        try:
            for _orphan in await asyncio.to_thread(db.get_unsurfaced_rlm_runs, session_id):
                _preview = (_orphan.get("answer_preview") or "").strip()
                _note = (
                    f"[background note] rlm_process run {_orphan['run_id']} ended after a "
                    f"previous turn was torn down: status={_orphan['status']}, "
                    f"{_orphan['iterations']} iterations, {_orphan['subcalls']} sub-calls."
                )
                if _preview:
                    _note += f" Partial answer preview: {_preview[:400]}"
                _note += (
                    f" (full trace: {_orphan['run_dir']}/trace.jsonl; to build on the partial "
                    f'work call rlm_process with continue_from="{_orphan["run_id"]}")'
                )
                await asyncio.to_thread(db.add_message, session_id, "system", _note)
                await asyncio.to_thread(db.mark_rlm_run_surfaced, _orphan["run_id"])
        except Exception:
            logger.debug("orphaned-RLM surfacing failed", exc_info=True)

    tool_round = 0
    while tool_round < settings.max_tool_rounds:
        # --- Pre-round checks ---
        _gate_action = await _pre_round_gate(session, session_id, tool_round)
        if _gate_action == "return":
            return
        if _gate_action == "break":
            break

        # Re-resolve the effective model each round so an in-turn switch_model
        # call (which writes session.model_override) actually moves the next
        # round's LLM call to the new provider/model.
        effective_model, model_supports_vision, model_supports_audio = _resolve_effective_model()
        if effective_model != _last_effective_model:
            # Same registry-miss guard as at turn start: an in-turn switch to
            # a freshly pulled model must not land on the manual fallback.
            if await ensure_model_known(effective_model):
                effective_model, model_supports_vision, model_supports_audio = _resolve_effective_model()
            _model_budget = derive_model_budget(effective_model)
            _max_output = derive_max_output(effective_model)
            await _announce_model_switch(session, session_id, _last_effective_model, effective_model, _baseline_model)
            _last_effective_model = effective_model

        # Build context (resource status is dynamic — includes remaining tool rounds).
        # Both run off-loop: resource status aggregates token_usage, and
        # compile_context loads the full message history + tokenizes — per
        # round, this was the single heaviest synchronous block on the loop.
        effective_budget = session.context_budget_override or _model_budget or settings.context_budget
        resource_status = await asyncio.to_thread(
            _build_resource_status,
            session_id,
            estimator,
            tool_round,
            context_budget=effective_budget,
            context_tokens=_last_context_tokens,
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
            max_output_tokens=_max_output,
            model_name=effective_model,
            turn_user_msg_id=_turn_user_msg_id,
        )

        _last_context_tokens = payload.token_count

        # Context health check
        utilization = payload.token_count / max(effective_budget, 1)
        if utilization > settings.context_critical_threshold:
            if not compaction.exhausted and not compaction.stalled(payload.token_count):
                logger.warning(
                    "Context critical (%.0f%%), attempting compaction-retry %d/%d",
                    utilization * 100,
                    compaction.attempts + 1,
                    compaction.ATTEMPT_LIMIT,
                )
                if await compaction.run(
                    payload, transition_reason="compact-critical", event_reason="critical_threshold"
                ):
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
        #
        # It still counts against the per-turn attempt budget and still records
        # the pre-compaction size. Without that, a compactor that fails to
        # shrink the context (e.g. the historical compacted_up_to=0 bug)
        # re-fires every tool round for the whole turn; counting it hands any
        # further attempts to the critical path above, which enforces the
        # stalled() guard and the attempt limit.
        if payload.needs_compaction and compaction.attempts == 0:
            await compaction.run(payload, transition_reason="compact-proactive", restore_state_on_failure=True)

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
        # _tried_fallback is declared before the outer loop (per-turn sticky).
        # If we already fell back this turn, skip straight to the fallback model
        # rather than re-attempting the rate-limited primary on every round.
        outcome = await stream_with_failover(
            client=client,
            session_id=session_id,
            emit=session.emit_event,
            messages=messages,
            base_messages=payload.messages,
            static_prefix_chars=payload.static_prefix_chars,
            tools=stream_tools,
            model=settings.fallback_model if _tried_fallback else model,
            max_output_cap=payload.effective_max_output,
            goal_id=session.active_goal_id,
            sched_created_at=_sched_created_at,
            sched_priority=_sched_priority,
            tried_fallback=_tried_fallback,
            # Only worth surfacing while we can still act on it; once the
            # budget is spent an overflow is just another fatal stream error.
            surface_context_overflow=not compaction.exhausted,
        )
        _tried_fallback = outcome.tried_fallback
        _stream_current_model = outcome.model
        collected_content = outcome.content
        collected_tool_calls = outcome.tool_calls
        _stream_finish_reason = outcome.finish_reason
        # Per-round assistant latency measured from the attempt that actually
        # answered, not the cumulative time across retries.
        _round_started_at = outcome.started_at
        _last_usage = outcome.usage or _last_usage

        if outcome.context_overflow is not None:
            # The provider rejected the request as too long. Compact and
            # restart this tool round against a re-compiled context.
            logger.warning(
                "API context overflow in session %s, attempting compaction-retry %d/%d",
                session_id,
                compaction.attempts + 1,
                compaction.ATTEMPT_LIMIT,
            )
            await compaction.run(payload, transition_reason="compact-overflow", event_reason="api_overflow")
            continue  # restart tool_round loop with compacted context

        if outcome.error:
            await _end_turn_on_stream_error(
                session=session,
                session_id=session_id,
                error=outcome.error,
                partial_content=collected_content,
                save_turn_msg=_save_turn_msg,
            )
            return

        # --- Length-truncation continuation ---
        if (
            _stream_finish_reason == "length"
            and not collected_tool_calls
            and collected_content
            and _length_continuation_count < LENGTH_CONTINUATION_LIMIT
        ):
            _length_continuation_count += 1
            await _continue_after_length_truncation(
                session=session,
                session_id=session_id,
                content=collected_content,
                tool_round=tool_round,
                attempt=_length_continuation_count,
                limit=LENGTH_CONTINUATION_LIMIT,
                save_turn_msg=_save_turn_msg,
            )
            tool_round += 1  # this counts as a round consumed
            continue  # back to top of tool_round loop

        # --- Native-format tool-call salvage ---
        # A model that wrote its tool call into the text stream instead of the
        # structured field still made the call; the provider layer knows the
        # wire formats and turns it back into one. Only consulted when the
        # structured field came back empty — a correctly framed call wins.
        if not collected_tool_calls and collected_content:
            salvaged = salvage_tool_calls(collected_content, registry.exists)
            if salvaged:
                logger.warning("Session %s: %s", session_id, salvaged.summary)
                collected_content = salvaged.content
                collected_tool_calls = salvaged.tool_calls

        # --- Response analysis ---
        if not collected_tool_calls:
            # A pending nudge answered with another tool-less reply = the
            # agent re-idled. Record before deciding whether to nudge again.
            if _followup_pending:
                _followup_pending = False
                await asyncio.to_thread(_record_followup_outcome, session, session_id, False)

            # Forced follow-up (spec Feature 9): the model narrated future work
            # ("Next, I'll…") and then went quiet. Ending the turn here means
            # paying for a full reflect retry to recover; one in-turn nudge is
            # strictly cheaper. The trigger is deliberately narrow — see
            # _announces_future_work — and bounded per turn.
            _followup_cap = max(0, int(getattr(settings, "forced_followup_max_per_turn", 1) or 0))
            if (
                getattr(settings, "forced_followup_enabled", False)
                and _forced_followups < _followup_cap
                and tool_round < settings.max_tool_rounds - 2
                and not session.cancel_requested
                and collected_content
                and _announces_future_work(collected_content)
            ):
                _forced_followups += 1
                await _save_turn_msg("assistant", collected_content)
                _nudge_text = await asyncio.to_thread(
                    _build_followup_nudge, session_id, collected_content, _forced_followups, _followup_cap
                )
                await asyncio.to_thread(db.add_message, session_id, "system", _nudge_text)
                session.emit_event(
                    {
                        "type": "turn.forced_followup",
                        "attempt": _forced_followups,
                        "max": _followup_cap,
                    }
                )
                logger.info(
                    "Session %s: forced follow-up %d/%d at round %d (model idled with future-intent tail)",
                    session_id,
                    _forced_followups,
                    _followup_cap,
                    tool_round,
                )
                _followup_pending = True
                session.touch()
                collected_content = ""
                tool_round += 1
                continue

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

        # A pending nudge answered with tool calls = the nudge worked.
        if _followup_pending:
            _followup_pending = False
            await asyncio.to_thread(_record_followup_outcome, session, session_id, True)

        # NOTE: We save the assistant message AFTER validation below,
        # so only validated tool_calls end up in the DB (prevents orphans).
        # The original collected_tool_calls may include hallucinated/malformed calls.

        # Stuck detection
        score, repeats = stuck.evaluate(collected_content, collected_tool_calls, tool_failures, registry)
        # One-time capability pointers queued by the detector (Signal 12's
        # rlm_process hint). Informational, not stuck-flow: they must not
        # touch score/repeats or the nudge budget.
        for _hint in stuck.pending_hints:
            await asyncio.to_thread(db.add_message, session_id, "system", _hint)
        stuck.pending_hints.clear()
        _stuck_action, _stuck_ask_user_continues = await _handle_stuck_signals(
            session_id=session_id,
            score=score,
            repeats=repeats,
            tool_calls=collected_tool_calls,
            active_tools=active_tools,
            nudges_used=_stuck_ask_user_continues,
            nudge_limit=STUCK_ASK_USER_LIMIT,
        )
        if _stuck_action == "nudge-and-retry":
            # Don't break — let one more round run so the agent can ask.
            collected_content = ""
            collected_tool_calls = []
            continue
        if _stuck_action == "stop":
            session.termination_reason = "round_ceiling"
            break

        # Filter, correct and validate before anything executes.
        parsed_calls, notes_by_id = await gate.admit(collected_tool_calls, active_tools)

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

        # Execute the surviving calls, then persist, emit and account for
        # every result in one pass.
        results = await _execute_round(parsed_calls, session=session, session_id=session_id, registry=registry)
        await _record_round_results(
            parsed_calls,
            results,
            session=session,
            notes_by_id=notes_by_id,
            save_turn_msg=_save_turn_msg,
            nudges_fired=nudges_fired,
            tool_failures=tool_failures,
            stuck=stuck,
            gate=gate,
            tool_round=tool_round,
            active_tools=active_tools,
        )

        session.touch()

        # A round can end the turn without the loop deciding to: cancelled,
        # or suspended on ask_user / await_workers.
        if await _post_round_gate(
            session,
            session_id,
            tool_round=tool_round,
            model=effective_model,
            usage=_last_usage,
            save_turn_msg=_save_turn_msg,
        ):
            return

        collected_content = ""
        collected_tool_calls = []
        tool_round += 1
        if (
            tool_round >= settings.max_tool_rounds
            and _round_cap_continues < int(getattr(settings, "round_cap_auto_continue", 0) or 0)
            and did_tool_calls
            and not session.cancel_requested
            and session.error is None
            and stuck.repeat_count < 3
        ):
            _round_cap_continues += 1
            logger.info(
                "Session %s: round cap reached mid-task — granting continuation %d/%d",
                session_id,
                _round_cap_continues,
                int(settings.round_cap_auto_continue),
            )
            try:
                from core.llm.client import extend_session_budget

                base = float(settings.llm_session_timeout) if settings.llm_session_timeout > 0 else 0.0
                if base > 0:
                    extend_session_budget(session_id, base)
            except Exception as _ext_err:
                logger.debug("Round-cap continuation budget extend failed: %s", _ext_err)
            await asyncio.to_thread(
                db.add_message,
                session_id,
                "system",
                f"[round cap reached — the harness granted one continuation "
                f"({settings.max_tool_rounds} more rounds). Use it to FINISH: "
                "complete the task or wrap up honestly with verified state. "
                "No further continuations follow this one.]",
            )
            tool_round = 0

    # If we exit the tool loop without returning (max rounds hit, or stuck break),
    # make one final response call with tools=None to get a clean text answer.
    # Natural while-loop exhaustion wasn't tagged by an in-loop branch above —
    # classify it now so downstream hooks can tell "round ceiling" from "complete".
    if session.termination_reason is None:
        session.termination_reason = "round_ceiling"
    if did_tool_calls:
        _last_usage = await _stream_final_answer(
            session=session,
            session_id=session_id,
            client=client,
            scout_text=scout_text,
            resource_status=resource_status,
            supports_vision=model_supports_vision,
            supports_audio=model_supports_audio,
            context_budget=session.context_budget_override or _model_budget or settings.context_budget,
            max_output=_max_output,
            model=effective_model,
            turn_user_msg_id=_turn_user_msg_id,
            save_turn_msg=_save_turn_msg,
            sched_created_at=_sched_created_at,
            sched_priority=_sched_priority,
            tried_fallback=_tried_fallback,
            last_usage=_last_usage,
        )
        return

    session.emit_event(
        {
            "type": "stream.done",
            "usage": _last_usage.__dict__ if _last_usage else {},
            "model": effective_model,
        }
    )


async def _scheduling_identity(session: AgentSession, session_id: str) -> tuple[int, float]:
    """(priority, created_at) for the LLM semaphore's fairness ordering.

    Workers rank below their orchestrating parent. created_at lives on the DB
    row, not the in-memory AgentSession — the old `session.created_at` raised
    AttributeError on every turn, was swallowed, and age-based scheduling
    fairness never worked (every session ranked float("inf")).
    """
    priority = PRIORITY_WORKER if session.session_type == "worker" else PRIORITY_ORCHESTRATOR
    try:
        from datetime import datetime as _dt

        row = await asyncio.to_thread(db.get_session, session_id)
        created_at = _dt.fromisoformat((row or {}).get("created_at", "").replace("Z", "+00:00")).timestamp()
    except Exception:
        created_at = float("inf")
    return priority, created_at


async def _resolve_active_goal(session: AgentSession, session_id: str) -> None:
    """Stamp the live goal id on the session for token_usage attribution.

    Once per turn (plan 3b). Workers keep the id they inherited at spawn so a
    fan-out's spend lands on the parent's goal; everyone else re-resolves, so a
    goal created or completed between turns is respected.
    """
    if not settings.goals_enabled or session.session_type == "worker":
        return
    try:
        goal_row = await asyncio.to_thread(db.get_active_goal, session_id)
        session.active_goal_id = (goal_row or {}).get("id")
    except Exception:
        session.active_goal_id = None


async def _pre_round_gate(session: AgentSession, session_id: str, tool_round: int) -> str:
    """Decide whether the next tool round may start. "run" | "break" | "return".

    "break" leaves the loop through the final-answer path (the turn has work
    worth summarizing); "return" abandons it outright.
    """
    # Cooperative cancellation checkpoint
    if session.cancel_requested:
        logger.info("Session %s: cancel requested, exiting agent loop", session_id)
        session.termination_reason = "cancelled"
        return "return"

    # In-turn goal budget checkpoint (audit P5): budgets used to be checked
    # only BETWEEN turns, so a single turn could overshoot token/time budgets
    # without bound. Every third round is enough — the between-turns check
    # remains the authoritative settlement.
    if settings.goals_enabled and session.active_goal_id and tool_round > 0 and tool_round % 3 == 0:
        try:
            exceeded = await asyncio.to_thread(_goal_budget_exceeded, session_id, session.active_goal_id)
        except Exception as _e:
            logger.debug("In-turn goal budget check failed: %s", _e)
            exceeded = None
        if exceeded:
            logger.info("Session %s: goal budget exceeded mid-turn (%s), ending turn", session_id, exceeded)
            session.emit_event({"type": "goal.budget_exceeded", "reason": exceeded})
            session.termination_reason = "budget_exhausted"
            return "break"

    # Pause checkpoint (for workers). The pause/resume round-trip is modelled
    # explicitly in the state machine: PROCESSING→PAUSE_REQUESTED (set by
    # pause_worker HTTP/tool) → PAUSED (observed here) → PROCESSING (on
    # resume). A cancel during pause wakes the loop and triggers the
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
        # Re-check cancel after resume (cancel_worker may have fired while we
        # were paused).
        if session.cancel_requested:
            logger.info("Session %s: cancel observed after resume", session_id)
            session.termination_reason = "cancelled"
            return "return"

    return "run"


async def _post_round_gate(
    session: AgentSession,
    session_id: str,
    *,
    tool_round: int,
    model: str,
    usage,
    save_turn_msg,
) -> bool:
    """True when this round ended the turn. The caller returns immediately.

    Suspension (ask_user / await_workers) terminates as "complete", not as an
    error: the work is paused, not failed, and something outside the agent
    loop resumes it — a fresh prompt() for an answer, _resume_from_workers for
    workers.
    """
    if session.cancel_requested:
        logger.info("Session %s: cancel requested after tool round %d", session_id, tool_round)
        session.termination_reason = "cancelled"
        return True

    # ask_user pause: the user's answer arrives as a new manager.prompt() call
    # which continues this turn.
    if session.waiting_for_input:
        logger.info("Session %s: ask_user posted — suspending agent loop", session_id)
        await save_turn_msg("assistant", "I've asked you a question and am waiting for your response.")
    elif session.waiting_for_workers:
        # await_workers(suspend=True): the parent suspends until the watched
        # workers complete; resume is automatic via _resume_from_workers().
        logger.info("Session %s: workers-dispatched — suspending agent loop", session_id)
    else:
        return False

    session.emit_event({"type": "stream.done", "usage": usage.__dict__ if usage else {}, "model": model})
    session.termination_reason = "complete"
    return True


async def _announce_model_switch(
    session: AgentSession,
    session_id: str,
    previous: str,
    current: str,
    baseline: str,
) -> None:
    """Record a mid-turn model change everywhere the UI reads from.

    The SSE event drives a live "<orig> ⇄ <override>" indicator; the persisted
    model_divider row is what makes the switch still visible after a page
    reload, since live events are never replayed from the DB.
    """
    override_active = session.model_override is not None
    session.emit_event(
        {
            "type": "model.override",
            "from": baseline,
            "to": current if override_active else None,
            "active": override_active,
        }
    )
    logger.info(
        "Session %s: effective model changed mid-turn '%s' -> '%s' (override_active=%s)",
        session_id,
        previous,
        current,
        override_active,
    )
    try:
        await asyncio.to_thread(
            db.add_message,
            session_id,
            "model_divider",
            "",
            metadata=json.dumps({"from": previous, "to": current, "active": override_active, "baseline": baseline}),
        )
    except Exception as _e:
        logger.debug("model_divider persist failed: %s", _e)
    session.emit_event(
        {
            "type": "model.divider",
            "from": previous,
            "to": current,
            "active": override_active,
            "baseline": baseline,
        }
    )


def _resolve_tool_surface(session: AgentSession, session_id: str, registry) -> tuple[str, list[str]]:
    """Decide which tools this turn may call, and the scout text to prepend.

    Scout proposes; five rules dispose, in order: builtins are always present,
    the monotonic allowlist re-adds anything this session already used
    successfully, co-occurrence pulls in siblings, a scheduled job's
    allowed_tools (when set) intersects the result, and reflect's retry
    exclusions are subtracted last so they beat all four.
    """
    scout_report = session.last_scout_report
    if not scout_report:
        # No scout report available. Nothing to substitute: SOUL/RULES/SESSIONS
        # arrive via the compiler's fixed-prefix directives block on every
        # turn, so there is no identity to recover here — only the per-task
        # curation is missing, and no deterministic text can stand in for that.
        # A scheduled job's allow-list still applies — this path must not be
        # the one that hands a constrained cron run the full tool surface.
        names = set(t.name for t in registry.enabled_tools())
        _allow = getattr(session, "tool_allowlist", None)
        if _allow:
            names &= set(_allow)
        return "", sorted(names)

    active: set[str] = set(scout_report.get_tool_names())
    # Always ensure core tools are available
    for t in registry.enabled_tools():
        if t.source == "builtin":
            active.add(t.name)
    # Monotonic allowlist: if the agent successfully used an extension tool in
    # a prior turn of this session, keep it in the schema. Prevents scout from
    # silently narrowing the surface between turns (e.g. dropping
    # install_package after a successful pip install), forcing the agent to
    # re-discover tools it has already proven it needs.
    try:
        for tname in _prior_turn_tool_names(session_id):
            # Skip disabled — a tool the user toggled off between turns must
            # NOT be re-promoted by the monotonic allowlist; otherwise
            # disabling a previously-used tool has no effect on the next turn.
            if registry.exists(tname) and not registry.is_disabled(tname):
                active.add(tname)
    except Exception as _e:
        logger.debug("Monotonic allowlist lookup failed for %s: %s", session_id, _e)
    # Pull in co-occurring siblings so e.g. spawn_worker brings
    # get_worker_result / check_workers / await_workers into the schema.
    active = registry.expand_cooccurrence(active)
    # Scheduled-job tool allow-list (E1, field case 0ba19fdbc823): when the
    # dispatching cron/heartbeat job declares allowed_tools, the schema is
    # intersected with it AFTER the builtin force-add, the monotonic allowlist
    # and cooccurrence expansion — a job's charter outranks all three. A tool
    # the model can't see is a tool it can't drift onto; the executor enforces
    # the same set as a backstop.
    allowlist = getattr(session, "tool_allowlist", None)
    if allowlist:
        dropped = active - set(allowlist)
        if dropped:
            logger.debug("Job allow-list dropped %d tools from schema: %s", len(dropped), sorted(dropped))
        active &= set(allowlist)
    # Retry effector (audit P1f): tools reflect disabled for this retry attempt
    # are removed from the schema entirely — overriding builtins and the
    # monotonic allowlist. The executor enforces this too.
    excluded = session.turn.retry_excluded_tools or set()
    if excluded:
        active -= excluded
    # Deterministic order for prompt cache
    return scout_report.to_system_prompt_section(), sorted(active)


async def _execute_round(parsed_calls: list[dict], *, session: AgentSession, session_id: str, registry) -> list:
    """Run one round's validated tool calls."""
    if not parsed_calls:
        return []
    # Announce before execution — tool.call is emitted only AFTER a tool
    # returns (it carries the result), so without this the user stares at a
    # static badge for the entire runtime of a slow bash/search call
    # wondering if it's stuck.
    for item in parsed_calls:
        session.emit_event(
            {
                "type": "tool.start",
                "name": item["tc"]["name"],
                "arguments": _summarize_args(item["parsed_args"]),
            }
        )
    return await execute_tool_round(
        [{"name": item["tc"]["name"], "arguments": item["parsed_args"]} for item in parsed_calls],
        context={"session_id": session_id},
        registry=registry,
    )


def record_tool_outcome(turn, result) -> None:
    """Fold one tool result into the turn's cumulative and per-attempt summaries.

    `failures` means the tool ran and failed. A policy refusal — the harness
    declining the call (job allow-list, retry exclusion, disabled tool,
    approval gate; see core.tools.executor.is_policy_refusal) — is counted as
    `refusals` with its own `refusal_errors` previews and never as a failure:
    candor's tool_ok, telos' anomaly scan, synthesis and the reflect summary
    all read `failures` as "the tool is unreliable", which a refusal is not.
    The stuck detector still sees refusals (a model that keeps calling a
    forbidden tool IS stuck) — that is handled by the caller.
    """
    from core.tools.executor import is_policy_refusal

    refused = is_policy_refusal(result)
    entry = turn.tool_summary.setdefault(
        result.tool_name,
        {"calls": 0, "failures": 0, "refusals": 0, "errors": [], "total_latency_ms": 0},
    )
    entry.setdefault("refusals", 0)  # summaries restored from before this key existed
    entry["calls"] += 1
    entry["total_latency_ms"] += result.latency_ms
    if refused:
        entry["refusals"] += 1
        preview = result.content[:300] if result.content else "refused"
        previews = entry.setdefault("refusal_errors", [])
        if preview not in previews:
            previews.append(preview)
    elif result.was_error:
        entry["failures"] += 1
        err_preview = result.content[:500] if result.content else "unknown"
        if err_preview not in entry["errors"]:
            entry["errors"].append(err_preview)

    # Per-attempt view (C2): reflect_count is the number of retries granted
    # so far, so it doubles as this attempt's zero-based index — it is
    # incremented by _maybe_reflect BEFORE the retry attempt re-enters here.
    attempt_idx = turn.reflect_count
    attempts = turn.tool_summary_attempts
    while len(attempts) <= attempt_idx:
        attempts.append({})
    a_entry = attempts[attempt_idx].setdefault(result.tool_name, {"calls": 0, "failures": 0, "refusals": 0})
    a_entry.setdefault("refusals", 0)
    a_entry["calls"] += 1
    if refused:
        a_entry["refusals"] += 1
    elif result.was_error:
        a_entry["failures"] += 1


async def _record_round_results(
    parsed_calls: list[dict],
    results: list,
    *,
    session: AgentSession,
    notes_by_id: dict[str, list[str]],
    save_turn_msg,
    nudges_fired: set[str],
    tool_failures: dict[str, list[str]],
    stuck: StuckDetector,
    gate: _ToolCallGate,
    tool_round: int,
    active_tools: list[str],
) -> None:
    """Persist, emit and account for one round's tool results.

    Five consumers read from the same pass: the transcript (what the model
    sees next round), the UI event stream, the stuck detector, the cross-round
    dedup cache, and the turn's tool summary that reflect grades against.
    Two results also feed back into the schema — discover_tools and
    create_tool/update_tool widen active_tools in place so a newly found or
    newly written tool is callable on the very next round.
    """
    from core.harness.nudges import evaluate as _nudge_eval

    for item, result in zip(parsed_calls, results):
        tc = item["tc"]
        tool_meta = json.dumps({"was_error": result.was_error, "latency_ms": result.latency_ms})

        # If the gate corrected this call (aliased name, coerced argument
        # types, dropped unknown params), prefix the notes onto the tool
        # result so the model sees the rewrite on its next turn. (A separate
        # role=system note would be stripped by normalize_for_openrouter;
        # tool-role messages are preserved.)
        stored_content = result.content
        gate_notes = notes_by_id.get(tc.get("id", ""))
        if gate_notes:
            stored_content = "\n".join(gate_notes) + "\n" + stored_content

        # Harness-side mid-turn nudge: if this tool result matches a known
        # failure signature (bot detection, 4xx/5xx from public web, SSRF block
        # on a public-looking domain), append a one-shot hint pointing at the
        # skill that solves it. The hint piggybacks on the tool-role message
        # because role=system mid-conversation gets stripped by provider
        # normalization.
        nudge = _nudge_eval(result.tool_name, stored_content, nudges_fired)
        if nudge:
            stored_content = stored_content + "\n\n" + nudge
            logger.info("harness.nudge fired tool=%s pattern hint appended", result.tool_name)

        await save_turn_msg(
            "tool",
            stored_content,
            tool_call_id=tc.get("id", ""),
            latency_ms=result.latency_ms,
            metadata=tool_meta,
        )
        # rlm_process results carry their run id in the header; a result that
        # reached save_turn_msg IS surfaced — mark the row so the orphan
        # sweep above never re-announces a run the agent already saw.
        if result.tool_name == "rlm_process":
            import re as _re_rlm

            _rm = _re_rlm.search(r"RLM run ([0-9a-f]{6,})", stored_content or "")
            if _rm:
                try:
                    await asyncio.to_thread(db.mark_rlm_run_surfaced, _rm.group(1))
                except Exception:
                    logger.debug("mark_rlm_run_surfaced failed", exc_info=True)

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
            gate.remember_success(result.tool_name, tc.get("arguments", ""), tool_round, result.content)
        # Semantic-streak observation (signals 8-10): records the result body's
        # "low info" status and hostname for the search-spiral / bot-wall /
        # same-domain-grind signals. Cheap bookkeeping only.
        stuck.observe_result(
            tool_name=result.tool_name,
            args=item["parsed_args"],
            content=result.content,
            was_error=result.was_error,
        )

        # Cumulative + per-attempt tool execution summary for reflect,
        # candor, telos and synthesis.
        record_tool_outcome(session.turn, result)

        # Dynamic tool expansion via discover_tools
        if result.tool_name == "discover_tools" and not result.was_error:
            _expand_tools_from_discovery(result.content, active_tools)

        # A newly created/updated custom tool goes straight into active_tools
        # so it appears in the LLM schema on the next round without requiring
        # a separate discover_tools call.
        if result.tool_name in ("create_tool", "update_tool") and not result.was_error:
            _inject_created_tool(item["parsed_args"].get("name", ""), active_tools)


async def _handle_stuck_signals(
    *,
    session_id: str,
    score: float,
    repeats: int,
    tool_calls: list[dict],
    active_tools: list[str],
    nudges_used: int,
    nudge_limit: int,
) -> tuple[str, int]:
    """Decide what a stuck score means for this round, and tell the model.

    Returns (action, nudges_used) where action is one of:
      "proceed"         — run the round normally (a mild-repetition nudge may
                          still have been written to the transcript)
      "nudge-and-retry" — asked the agent to call ask_user; discard this
                          round's calls and give it another round to comply
      "stop"            — end the tool loop

    Prefer asking the user over silent exhaustion: when ask_user is active,
    direct the agent to call it with a concrete question. But cap the nudging
    — an LLM that ignores the ask_user hint keeps the loop spinning, emitting
    another nudge and burning more LLM time each round (observed: 16 in a row
    before the agent self-corrected). Past the cap, fall through to the same
    "summarize and stop" path used when ask_user isn't available at all.
    """
    if repeats < 3:
        # Not stuck this round — reset the consecutive-nudge counter so a
        # later separate stuck episode gets the full nudge budget.
        if repeats >= 1 and score > 0.3:
            names = sorted({tc.get("name", "") for tc in (tool_calls or [])})
            await asyncio.to_thread(
                db.add_message,
                session_id,
                "system",
                f"You are repeating tool calls ({', '.join(names) if names else 'unknown'}). "
                "Do NOT retry the same operation. "
                "Review what you have already accomplished and proceed to the next unfinished step.",
            )
        return "proceed", 0

    logger.warning("Session %s stuck (score=%.1f, repeats=%d)", session_id, score, repeats)
    ask_user_available = "ask_user" in (active_tools or [])
    if ask_user_available and nudges_used < nudge_limit:
        nudges_used += 1
        recent_tool_names = sorted({tc.get("name", "") for tc in (tool_calls or [])})
        await asyncio.to_thread(
            db.add_message,
            session_id,
            "system",
            "You appear to be stuck in a loop "
            f"(recently used: {', '.join(recent_tool_names) or 'n/a'}). "
            "Do NOT retry the same approach. Call ask_user with a specific "
            "clarifying question that names what you tried, what failed, and "
            "what you need from the user to proceed. After ask_user returns, "
            "use the answer to pick a new strategy.",
        )
        return "nudge-and-retry", nudges_used

    if ask_user_available:
        # Hit the cap. Emit a final, distinct system message so the transcript
        # records why we gave up nudging, then stop.
        logger.warning(
            "Session %s stuck cap reached (%d consecutive nudges ignored), force-breaking loop",
            session_id,
            nudges_used,
        )
        await asyncio.to_thread(
            db.add_message,
            session_id,
            "system",
            f"Stuck-detection nudged you {nudges_used} "
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
    return "stop", nudges_used


async def _end_turn_on_stream_error(
    *,
    session: AgentSession,
    session_id: str,
    error: str,
    partial_content: str,
    save_turn_msg,
) -> None:
    """Terminate the turn after the retry/fallback ladder ran out of options.

    SOFT-LAND for LLM budget exhaustion: when the per-session LLM time budget
    (settings.llm_session_timeout, default 1800s) trips mid-turn, the agent has
    typically already produced visible work in this turn (prior assistant
    rounds, tool calls, files). Hard-erroring the turn means reflect runs
    against a transcript that looks broken, even when the actual deliverable is
    fine. Detect the LLMSessionTimeoutError specifically and route to a clean
    termination (BUDGET_EXHAUSTED) so reflect grades the partial output as a
    near-pass instead of "session crashed."
    """
    budget_exhausted = "exceeded the" in error and "LLM time limit" in error
    if partial_content:
        mid = await save_turn_msg("assistant", partial_content, partial=1)
        session.emit_event(
            {
                "type": "partial.saved",
                "content_preview": partial_content[:100],
                "message_id": mid,
            }
        )
    if budget_exhausted:
        # Inject a system message documenting the soft-land so the reflect
        # evidence shows it AND the user-visible transcript explains the
        # truncation point. We do NOT set session.error here — that's reserved
        # for genuine failures.
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
            "LLM budget exhausted in session %s — soft-landing as BUDGET_EXHAUSTED instead of error: %s",
            session_id,
            error,
        )
        session.termination_reason = "budget_exhausted"
        session.emit_event({"type": "stream.budget_exhausted", "message": error})
        return
    logger.error("LLM stream error in session %s: %s", session_id, error)
    session.error = error
    session.termination_reason = "error"
    session.emit_event({"type": "stream.error", "error": error})
    # A stream error kills the turn AND its grade: errored sessions skip
    # reflect, so nothing downstream ever says what happened (field case
    # ae952f40e3d1: 61 rounds of work ended mid-flight with no final message,
    # no verdict, no notification — the session just went quiet). Leave a
    # durable trace a human will actually see.
    try:
        sess_row = await asyncio.to_thread(db.get_session, session_id)
        title = (sess_row or {}).get("title") or session_id[:12]
        await asyncio.to_thread(
            db.add_notification,
            session_id=session_id,
            title=f"{title}: turn ended on a stream error",
            body=(
                f"The LLM stream failed after retries and fallback: {error[:300]} — "
                "the turn's partial work is in the transcript, but it was not graded "
                "(reflect skips errored turns). Reply in the session to resume."
            ),
            urgency="high",
        )
    except Exception as _ne:
        logger.debug("stream-error notification failed for %s: %s", session_id, _ne)


async def _continue_after_length_truncation(
    *,
    session: AgentSession,
    session_id: str,
    content: str,
    tool_round: int,
    attempt: int,
    limit: int,
    save_turn_msg,
) -> None:
    """Let the model finish a response the max_tokens cap cut off.

    finish_reason="length" with content and no tool calls means it stopped
    mid-thought. Saving the partial and asking it to continue costs one extra
    agent round; letting reflect notice the truncation instead costs a full
    reflect + scout + agent re-execution (~50s) for the same text.
    """
    logger.info(
        "Session %s: LLM hit max_tokens (finish_reason=length) round=%d, continuing (attempt %d/%d)",
        session_id,
        tool_round,
        attempt,
        limit,
    )
    # Save the partial as the assistant message (no partial=1 flag — this
    # isn't an error path, the response IS valid, just unfinished).
    await save_turn_msg("assistant", content)
    # The next round sees its own truncated assistant message in the
    # transcript and picks up from where it stopped.
    await asyncio.to_thread(
        db.add_message,
        session_id,
        "system",
        "Your previous response was cut off because it hit the "
        "max_tokens limit. Continue from exactly where you stopped — "
        "do not repeat what you already wrote.",
    )
    session.emit_event({"type": "stream.length_continuation", "attempt": attempt, "max": limit})
    session.touch()


async def _stream_final_answer(
    *,
    session: AgentSession,
    session_id: str,
    client,
    scout_text: str,
    resource_status: str,
    supports_vision: bool,
    supports_audio: bool,
    context_budget: int,
    max_output: int,
    model: str,
    turn_user_msg_id: int | None,
    save_turn_msg,
    sched_created_at: float,
    sched_priority,
    tried_fallback: bool,
    last_usage,
):
    """One tools=None call to turn a finished tool loop into a text answer.

    Reached when the loop ran tools and then stopped — round ceiling, stuck
    break, or budget — so the transcript ends on tool results with nothing
    addressed to the user. Recompiling without schemas is what forces text:
    a model handed tools at the end will keep calling them.

    Returns the usage to report; emits stream.done either way (an errored
    final response still ends the turn, and the UI has to stop streaming).
    """
    logger.info("Tool loop ended, generating final response (tools=None)")
    payload = await asyncio.to_thread(
        compile_context,
        session_id=session_id,
        tool_schemas=None,  # no tools — force text response
        scout_report_text=scout_text,
        resource_status=resource_status,
        supports_vision=supports_vision,
        supports_audio=supports_audio,
        context_budget=context_budget,
        max_output_tokens=max_output,
        model_name=model,
        turn_user_msg_id=turn_user_msg_id,
    )
    messages = payload.messages
    provider = client.resolve_provider(model)
    if provider in OPENAI_FORMAT_PROVIDERS:
        messages = normalize_for_openrouter(messages)
        messages = attach_cache_breakpoints(messages, model, provider, payload.static_prefix_chars)

    # Honor the tool loop's sticky failover: once a turn has failed over,
    # re-attempting the known-bad primary for the final response just burns
    # the backoff ladder again before landing on the same fallback.
    started_on_fallback = tried_fallback and settings.fallback_model != ""
    final = await stream_with_failover(
        client=client,
        session_id=session_id,
        emit=session.emit_event,
        messages=messages,
        base_messages=payload.messages,
        static_prefix_chars=payload.static_prefix_chars,
        tools=None,  # no tools — force a text response
        model=settings.fallback_model if started_on_fallback and settings.fallback_model else model,
        max_output_cap=payload.effective_max_output,
        goal_id=session.active_goal_id,
        sched_created_at=sched_created_at,
        sched_priority=sched_priority,
        tried_fallback=started_on_fallback,
        label="Final response stream",
    )
    usage = final.usage or last_usage
    if final.content:
        await save_turn_msg("assistant", final.content)
    if final.error is not None:
        logger.error("Final response error: %s", final.error)
        session.emit_event({"type": "stream.error", "error": final.error})
        # The turn is over either way; the caller's model attribution is the
        # one it started with, since the ladder never produced an answer.
        session.emit_event(
            {
                "type": "stream.done",
                "usage": usage.__dict__ if usage else {},
                "model": model,
            }
        )
        return usage

    session.emit_event(
        {
            "type": "stream.done",
            "usage": usage.__dict__ if usage else {},
            "model": final.model,
        }
    )
    return usage


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


def _build_resource_status(
    session_id: str,
    estimator,
    tool_round: int = 0,
    context_budget: int | None = None,
    context_tokens: int | None = None,
) -> str:
    """Build resource status for system prompt.

    Two token numbers in DIFFERENT units (field case 17683100ecf8): the old
    display divided lifetime session spend — which re-counts the re-sent
    context on every call — by the context window, so any long tool loop
    read "over budget" within a dozen rounds (that session showed 1,299%
    while its largest prompt filled 36% of the window). The agent
    panic-quit 2-3 rounds short of a win over a number that constrained
    nothing. Now: context fullness is the window-relative percentage,
    lifetime spend is a plain informational count, and tool rounds are
    named as the only binding limit.
    """
    usage = db.get_session_usage(session_id)
    total_tokens = usage.get("total", 0)
    calls = usage.get("calls", 0)
    budget = context_budget if context_budget is not None else settings.context_budget
    if context_tokens is not None and budget:
        window = (
            f"Context window: {context_tokens:,}/{budget:,} tokens "
            f"({context_tokens / budget * 100:.0f}% full, auto-compacted)"
        )
    else:
        window = f"Context window: {budget:,} tokens (auto-compacted)"
    remaining = settings.max_tool_rounds - tool_round
    base = (
        f"[RESOURCE STATUS] {window} | "
        f"Session spend so far: {total_tokens:,} tokens over {calls} LLM call(s) "
        f"(informational only — not a limit) | "
        f"Tool rounds remaining: {remaining}/{settings.max_tool_rounds} (the only binding limit)"
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
