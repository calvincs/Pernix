# Refactor plan — 2026-08 systems audit follow-through

Status: **IMPLEMENTED** (2026-08-07, branch `next-phase-features`, 11 commits
`a1f3271..`). All phases landed. One supersession: operator direction replaced
Phase 3's `critical_model` tier with a three-role scheme — Primary
(`llm_model`: agent turns + compaction/reflect/eval), Background
(`background_model`: scout/titles/distill/idle work/RLM subs), Backup
(`fallback_model`: used when Primary or Background fail, any provider).
Deferred to next cycle: full Dream→Telos store merge; state-machine v2
stages 2–5 (dual-write removal).

Source: whole-system audit of 2026-08-07 (six parallel code audits + live field check
on box). Full report: claude.ai artifact "Pernix — next-phase-features Systems Audit";
knowledge-base file `pernix.audit.next-phase-2026-08`.

Baseline at start: **93,083 py LOC · 1,939 tests · check.sh green.**
Exit criteria: fewer LOC, fewer overlapping mechanisms, all checks green, deployed to
box and live-verified. Nothing here adds a new subsystem; every change either fixes a
defect, removes code, or wires an existing organ to an existing artery.

## Phase 1 — Correctness (P0/P1 bug ledger + field-check promotions)

| # | Fix | Files |
|---|---|---|
| 1a | Hybrid RRF score scale broken for absolute thresholds; remove `_ripgrep_fallback` | `core/memory/search.py`, `internal_recall.py` |
| 1b | Semaphore priority inversion: compaction/reflect acquire with no session identity | `core/context/compaction.py`, `core/reflect.py` |
| 1c | Event-loop blockers: telos post-task hook (candor `predict_sync` + sync FS), distill `is_duplicate`, executor kernel-bind path | `core/telos/anomaly.py`, `core/memory/distill.py`, `core/tools/executor.py` |
| 1d | Tripwire after-window slides + dismiss not durable + queue-time vs apply-time; all-rejected batch marked applied; no human delete for entries; low/high merged into one proposal; candor hints never retire | `core/adaptive/*`, `api/routers/adaptive.py`, `core/snooze.py` |
| 1e | RLM recursion concurrency per-broker (3^depth); nested run holds parent slot; `RLMCaps.max_depth` dead; 3 inconsistent size claims in one prompt; `_call_one` NameError in finally | `core/extensions/rlm/*` |
| 1f | **Cross-retry circuit breaker** (field: 10 identical retries) + mechanical lesson effector (per-retry tool exclusions) | `sessions/manager.py`, `sessions/hooks.py`, `core/reflect.py`, `core/agent.py` |
| 1g | Kernel: roundtrip deadline before lock; spawn failure unwrapped; binding limited to 4 tools; payload paths unreadable (`allowed_read_roots`); 1h spill TTL; kernel settings unbounded in settings API | `core/extensions/rlm/child_env.py`, `core/kernel/*`, `core/tools/*`, `api/routers/health.py` |
| 1h | Telos: L2 freeze dead-end; ack resets ladder; `novelty_entropy` ignores window; `mint_id` race; serendipity lifetime-share | `core/telos/*` |
| 1i | Failover requires different provider (Ollama→Ollama = none); final response ignores sticky fallback | `core/agent.py`, `core/llm/router.py` |
| 1j | Continuation exhaustion silent; `goal_complete` wrong workspace; `add_gate` lacks scope; heartbeat sessions not reap-protected | `sessions/manager.py`, `core/tools/builtin/goal_tools.py`, `core/extensions/evaluation/`, `maintenance.py` |
| 1k | Config/docs drift: undeclared `max_inline_attach_bytes`; `adaptive_auto_apply` comment/default contradiction; stale comments; RLM child RLIMIT dup constants | `config.py`, docs |

## Phase 2 — Caps modernization

- `apply_view_pruning`: budget-pressure-gated, configurable, emits an event (was:
  unconditional stub of tool results >300 chars beyond last 10 messages).
- `context_budget`: derived per-session from the model registry's `context_length`
  (global setting becomes a fallback/ceiling).
- Raise weak-model-era defaults to match how the system is actually operated (box
  overrides observed live): `max_tool_rounds` 10→50, cloud concurrency 2→4, memory
  recall 5→10, grep/glob caps, scout instruction-file reads, compaction summarizer
  input caps + output tokens.
- Degradation surfacing: reflect excerpt-ladder and view-pruning emit events; memory
  ingest no longer truncates at write time.

## Phase 3 — Model roles: split by criticality, not phase

- New optional `critical_model` (default: `llm_model`). Consumers: compaction,
  reflect (chain `reflect_model → critical → llm`; the `→ scout` edge is **removed**),
  eval, refine/adaptive proposal authoring.
- `scout_model`/`background_model` remain the fast/offline tier. No rename churn.
- Docs corrected: `background_model` no longer described as "titles + distillation"
  while running the session's permanent memory.

## Phase 4 — Prune & consolidate (net-negative LOC)

- Delete `schedule_workflow` scheduling wrapper (dead `extra_meta`; LLM-mediated
  trigger) — replaced by direct `run_workflow` dispatch from cron.
- Delete snooze Activity 2b + `core/snooze_reflect.py` (superseded by `core/refine.py`).
- Heartbeat idle branch delegates to the cron dispatch path (dedupe).
- Legacy state remnants: `SessionState.DELETED`, `is_openrouter_model`,
  `FairLLMSemaphore` alias, ignored `release(session_id=)` param, dead coalesce guard.
- Split `core/snooze.py` (2,792 lines): memory-store surgery → `core/memory/sweeps.py`,
  retention pruners → `core/retention.py`; snooze keeps lifecycle + ladder.
- Remove tests that only exercise deleted code.

## Phase 5 — Actuation (wire existing organs, no new subsystems)

- Telos ports: minted questions/spend bind to the active `session_goals` row and real
  `token_usage.goal_id`; supported claims flow through `queue_producer_edits("telos", …)`;
  open questions/alarms injected via the context compiler (flag-gated; byte-identical
  when off).
- Autonomy corridor: cron sessions may carry goals; in-turn token-budget check at
  round boundaries (`budget_exhausted`); goal-continuation sessions are
  snooze-transparent.
- Memory-edit effector: validated dream contradiction/stale findings become
  approvable targeted consolidations instead of empty-payload proposals (field
  finding: 72/75 pending proposals had no effector).
- **Dream→Telos merge scope note:** this cycle implements the convergence bridge
  (shared adaptive output path, telos gains dream's banned-claim filter + evidence
  dedup, memory-edit effector). The full store/lifecycle unification is deferred to
  the next cycle — it is the highest-regression-risk item in the audit and should
  land alone, not amid forty other changes.

## Phase 6 — Verify, deploy, live-test

1. `./check.sh` green (black, ruff, flake8, pytest ≥63% cov); LOC delta reported.
2. Deploy per box runbook: backups → 0 busy sessions → `git pull --ff-only` →
   `docker compose up -d --build` → sha256 host↔container verify → log sweep.
3. Live verification (agent-driven): chat turn + reflect, kernel bind + `repl` slice,
   RLM bounded run, adaptive entry delete + tripwire behavior, telos hook timing,
   circuit-breaker in the running interpreter, canary sweep status.
4. Failures loop back: fix → redeploy → re-test.
