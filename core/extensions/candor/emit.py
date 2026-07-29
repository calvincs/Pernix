"""Pernix — Candor emission: map turn-end state to observation dicts.

Purely mechanical (no LLM). Everything here derives from state the session
already tracks: last_tool_summary, termination_reason, and the reflect
verdict. Emission is delta-tracked against what earlier reflect-retry
attempts of the same turn already emitted, so a retry never double-observes
the first attempt's tool calls.

Observation dict shape (consumed by CandorBridge.record):
    {pred, args, stmt_type, outcome|value, ctx, actor, ts}
"""

from __future__ import annotations

import re

# A single runaway turn shouldn't flood the store with hundreds of identical
# outcomes; the statistical shape survives clamping.
_PER_TOOL_CLAMP = 25

# Ordered: first match wins. Buckets form the open vocabulary of
# tool_failure_mode — Candor reserves probability mass for values it has
# never seen, so adding buckets later is safe.
_ERROR_BUCKETS: list[tuple[str, re.Pattern]] = [
    ("timeout", re.compile(r"time[d\s-]*out|deadline|timeout", re.I)),
    ("auth", re.compile(r"401|403|unauthorized|forbidden|api.?key|authenticat|permission denied", re.I)),
    ("rate_limit", re.compile(r"429|rate.?limit|quota|too many requests", re.I)),
    ("not_found", re.compile(r"404|not found|no such file|command not found|does not exist|unknown tool", re.I)),
    ("invalid_args", re.compile(r"invalid|missing (?:required )?(?:argument|parameter|field)|validation|schema", re.I)),
    ("network", re.compile(r"connection|unreachable|dns|refused|reset by peer|ssl|certificate", re.I)),
]


def classify_error(error_text: str) -> str:
    for bucket, pattern in _ERROR_BUCKETS:
        if pattern.search(error_text or ""):
            return bucket
    return "other"


def build_turn_observations(
    *,
    tool_summary: dict,
    already_emitted: dict,
    termination_reason: str | None,
    reflect_verdict: str | None,
    failure_cause: str | None,
    model: str,
    session_kind: str,
    is_retry: bool,
    ts_ms: int,
    max_obs: int = 200,
) -> tuple[list[dict], dict]:
    """Build this attempt's observations and the updated per-turn ledger.

    already_emitted: {tool: {"calls": n, "failures": n, "errors": n}} from
    prior attempts of the same turn. Returns (observations, new_emitted) —
    the caller stores new_emitted keyed by turn id.
    """
    base_ctx = {"model": model or "default", "kind": session_kind or "normal"}
    observations: list[dict] = []

    # Turn outcome + reflect verdict first: small, and they must survive the cap.
    if termination_reason:
        observations.append(
            {
                "pred": "turn_ok",
                "args": ["*"],
                "stmt_type": "frequency",
                "outcome": termination_reason == "complete",
                "ctx": {**base_ctx, "retry": "yes" if is_retry else "no"},
                "actor": "agent:pernix",
                "ts": ts_ms,
            }
        )
    if reflect_verdict:
        observations.append(
            {
                "pred": "reflect_verdict",
                "args": ["*"],
                "stmt_type": "categorical",
                "value": reflect_verdict,
                "ctx": {**base_ctx, "failure_cause": failure_cause or "none"},
                "actor": "verifier:reflect",
                "ts": ts_ms,
            }
        )

    new_emitted: dict = {}
    for tool, entry in (tool_summary or {}).items():
        calls = int(entry.get("calls", 0))
        failures = int(entry.get("failures", 0))
        errors = entry.get("errors") or []
        prev = already_emitted.get(tool, {"calls": 0, "failures": 0, "errors": 0})

        d_calls = max(0, calls - int(prev.get("calls", 0)))
        d_failures = max(0, min(failures - int(prev.get("failures", 0)), d_calls))
        d_successes = d_calls - d_failures

        per_tool = {"pred": "tool_ok", "args": [tool], "stmt_type": "frequency"}
        aggregate = {"pred": "tool_ok", "args": ["*"], "stmt_type": "frequency"}
        for outcome, count in ((True, min(d_successes, _PER_TOOL_CLAMP)), (False, min(d_failures, _PER_TOOL_CLAMP))):
            for _ in range(count):
                observations.append(
                    {**per_tool, "outcome": outcome, "ctx": dict(base_ctx), "actor": "agent:pernix", "ts": ts_ms}
                )
                observations.append(
                    {
                        **aggregate,
                        "outcome": outcome,
                        "ctx": {**base_ctx, "target": tool},
                        "actor": "agent:pernix",
                        "ts": ts_ms,
                    }
                )

        # Newly seen error previews → failure-mode buckets (open vocabulary).
        for err in errors[int(prev.get("errors", 0)) :]:
            observations.append(
                {
                    "pred": "tool_failure_mode",
                    "args": [tool],
                    "stmt_type": "categorical",
                    "value": classify_error(str(err)),
                    "ctx": dict(base_ctx),
                    "actor": "agent:pernix",
                    "ts": ts_ms,
                }
            )

        new_emitted[tool] = {"calls": calls, "failures": failures, "errors": len(errors)}

    return observations[:max_obs], new_emitted
