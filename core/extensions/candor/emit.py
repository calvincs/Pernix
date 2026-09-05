"""Pernix — Candor emission: map turn-end state to observation dicts.

Purely mechanical (no LLM). Everything here derives from state the session
already tracks: session.turn.tool_summary, termination_reason, and the reflect
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


def build_memory_observations(
    *,
    file_name: str,
    event: str,
    source: str,
    ts_ms: int,
) -> list[dict]:
    """Map a user-model memory mutation to user_fact attestation observations.

    Only `user.*` memory files count — that namespace is the user model. The
    ledger carries file slugs and counts, never entry prose, so no PII enters
    the append-only chain. Semantics: p(user_fact(area)) is the share of
    attestations in that area that have stood unrevised — earned stability,
    not a confidence score for any single fact.

    Events: "attest" (entry added → True), "revise" (entry superseded →
    False + True: the old formulation died, a corrected one replaced it),
    "forget" (entry deleted → False).
    """
    if not file_name.startswith("user."):
        return []
    slug = file_name[len("user.") :] or file_name
    outcomes = {"attest": [True], "revise": [False, True], "forget": [False]}.get(event)
    if not outcomes:
        return []
    # The user attests their own facts; agent-derived writes and later
    # revisions are the system speaking.
    actor = "human:user" if (event == "attest" and source == "user") else "agent:pernix"
    ctx = {"origin": source or event}
    observations: list[dict] = []
    for outcome in outcomes:
        for args, extra in (([slug], {}), (["*"], {"target": slug})):
            observations.append(
                {
                    "pred": "user_fact",
                    "args": args,
                    "stmt_type": "frequency",
                    "outcome": outcome,
                    "ctx": {**ctx, **extra},
                    "actor": actor,
                    "ts": ts_ms,
                }
            )
    return observations


def build_experience_observations(
    *,
    experience: dict,
    model: str,
    session_kind: str,
    is_retry: bool,
    ts_ms: int,
) -> list[dict]:
    """Map reflect's experience read to interaction-quality observations.

    Purely mechanical over the already-sanitized experience dict (reflect's
    _sanitize_experience enforces the schema). The ledger carries labels and
    booleans only — user_observations prose and the free-form note stay in
    the post-mortem, never the append-only chain (same PII rule as
    build_memory_observations).

    Semantics mirror turn_ok: one observation per reflect attempt, with the
    retry flag in ctx so calibration can separate first tries from retries.
    """
    if not experience:
        return []
    base_ctx = {"model": model or "default", "kind": session_kind or "normal", "retry": "yes" if is_retry else "no"}
    observations: list[dict] = []

    sentiment = experience.get("user_sentiment")
    if sentiment and sentiment != "unknown":
        observations.append(
            {
                "pred": "user_sentiment",
                "args": ["*"],
                "stmt_type": "categorical",
                "value": str(sentiment)[:40],
                "ctx": dict(base_ctx),
                "actor": "verifier:reflect",
                "ts": ts_ms,
            }
        )

    # Booleans absent from the dict were never answered — no observation.
    # Polarity: frequency preds must read True = healthy, because the intel
    # brief surfaces LOW p as degradation. clarification_loop=True is the bad
    # outcome, so it inverts into no_clarification_needed at this boundary.
    for key, pred, invert in (
        ("clarification_loop", "no_clarification_needed", True),
        ("first_response_sufficient", "first_response_sufficient", False),
    ):
        val = experience.get(key)
        if isinstance(val, bool):
            observations.append(
                {
                    "pred": pred,
                    "args": ["*"],
                    "stmt_type": "frequency",
                    "outcome": (not val) if invert else val,
                    "ctx": dict(base_ctx),
                    "actor": "verifier:reflect",
                    "ts": ts_ms,
                }
            )

    # Open vocabulary, like tool_failure_mode — new friction labels are safe.
    for label in (experience.get("friction") or [])[:6]:
        observations.append(
            {
                "pred": "friction_mode",
                "args": ["*"],
                "stmt_type": "categorical",
                "value": str(label)[:40],
                "ctx": dict(base_ctx),
                "actor": "verifier:reflect",
                "ts": ts_ms,
            }
        )
    return observations


def build_gate_observations(
    *,
    gates: list[dict],
    attempt: int,
    model: str,
    session_kind: str,
    reflect_mode: str,
    ts_ms: int,
) -> list[dict]:
    """Map one attempt's deterministic gate verdicts to observations.

    gates: [{"name", "passed", "excerpt"}] — one entry per gate that ran on
    THIS attempt (gates re-run on every reflect retry, and each attempt is a
    genuine observation: a gate that failed attempt 1 and passed attempt 2
    observed two different things).

    Semantics mirror tool_ok / tool_failure_mode. gate_ok carries the verdict
    either way — a ledger that only ever sees passes calibrates to p=1 and
    predicts nothing — and a failure additionally lands a gate_failure_mode
    bucket. The excerpt is classified, never carried raw: the append-only
    chain holds labels and booleans only (same rule as
    build_memory_observations), and a free-form categorical value would mint
    a fresh category per stack trace. The prose lives in the TELOS trace.

    reflect_mode rides in ctx because the two grading regimes are different
    retry loops: since bfbaadd an interactive turn's reflect grade is
    observe-only, so the gate is the only mechanical retry path it has.
    """
    base_ctx = {
        "model": model or "default",
        "kind": session_kind or "normal",
        "retry": "yes" if attempt > 1 else "no",
        "reflect_mode": reflect_mode or "sync",
    }
    observations: list[dict] = []
    for gate in gates:
        name = str(gate.get("name") or "")
        if not name:
            continue
        passed = bool(gate.get("passed"))
        observations.append(
            {
                "pred": "gate_ok",
                "args": [name],
                "stmt_type": "frequency",
                "outcome": passed,
                "ctx": dict(base_ctx),
                "actor": "agent:pernix",
                "ts": ts_ms,
            }
        )
        if not passed:
            observations.append(
                {
                    "pred": "gate_failure_mode",
                    "args": [name],
                    "stmt_type": "categorical",
                    "value": classify_error(str(gate.get("excerpt") or "")),
                    "ctx": dict(base_ctx),
                    "actor": "agent:pernix",
                    "ts": ts_ms,
                }
            )
    return observations


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
        # A tool that was unavailable by design never ran, so it is evidence
        # about the session, not about the tool: netted out of the calls
        # denominator so it becomes neither a tool_ok success nor a failure.
        # Without this, ask_user's unattended non-answers were emitted as
        # tool_ok(ask_user)=False and the reliability producer minted a
        # routing hint steering scout away from ever asking the user.
        calls = max(0, int(entry.get("calls", 0)) - int(entry.get("unavailable") or 0))
        failures = min(int(entry.get("failures", 0)), calls)
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
