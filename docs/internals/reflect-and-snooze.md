# Reflect and Snooze

Two non-obvious subsystems that run alongside the agent loop:

- **Reflect** — a quality-gate pass that runs after every turn, verifies whether the user's intent was actually fulfilled, and can trigger bounded retries if it wasn't.
- **Snooze** — idle-time housekeeping that runs in the background when no sessions are active, deduplicating memory, consolidating clusters, extracting user-profile facts, archiving old post-mortems — and, when enabled, running the [Dream](dream.md) introspection step.

Both are off the critical path of any single user request, but they shape how Pernix behaves over weeks and months. This page explains what each does and where to tune it.

---

## Reflect — the post-turn quality gate

Defined in `core/reflect.py`. Runs after the main agent loop completes. Cron, worker, and canary sessions keep it in-turn, before the session settles; for normal sessions the grade is deferred (`reflect_deferred_normal`, default true) and runs observe-only in the background, `reflect_defer_idle_s` (default 300s) after the session reaches `IDLE_READY`.

### Inputs

Reflect sees the **current attempt's transcript** with verbatim tool result bodies — sliced from the most recent `scout` role marker forward, so on a retry it sees only the work that just happened, not the whole growing session. The evidence blob is:

- The original user message that started the turn.
- A preamble with workspace files, termination history, scout's plan/approach, and a tool execution summary (counts, failures, last few errors).
- An `ATTEMPT TRANSCRIPT` section containing every assistant message, tool call, and tool result from this attempt — tool result bodies are kept verbatim up to a per-result cap (5000 chars), with longer results truncated and marked `[+N chars truncated]`.
- The agent's final response (echoed at the end for grounding).

This is what lets reflect verify factual claims against what the tools actually returned, instead of grading them against its own training-data priors.

### Refusals are not failures

The TOOL EXECUTION SUMMARY counts `failures` (the tool ran and failed) apart
from `refusals` (the harness declined the call: a scheduled job's
allow-list, a retry exclusion, a disabled tool, the approval gate). The
executor tags refusals at the source (`core.tools.executor.is_policy_refusal`)
and `core.agent.record_tool_outcome` keeps them out of `failures`, so reflect,
candor's `tool_ok`, telos' anomaly scan and synthesis never read "bash
failed 7 times" for seven calls bash was told not to make. Reflect still
sees them — as `policy refusal(s)` and REFUSED lines — and grades a breach
only when the user or the charter forbade the tool. The rubric also requires
that any requirement a non-pass verdict attributes to the user be quoted
from USER REQUEST; a requirement found only in the scout plan cannot justify
retry or escalate (a deferred escalate on a correct reply did exactly that).

### The grounding check

The evidence blob ends with a mechanical **GROUNDING CHECK**, computed in
Python from the same attempt slice the transcript was built from — never by
the model. It lists (a) identifiers the final response cites (`#ids`,
`ab-` batch ids, 12-hex session/hypothesis ids, backticked names) that
appear in no tool result and not in the user's message, and (b) markdown
table rows whose first pairing — the id and the name beside it — no single
tool result shows within ~1500 chars. The scout's plan is deliberately not
a source: it repeats the ids it was asked about, which would vouch for
exactly the rows the check exists to catch.

It is a flag, not a verdict. The rubric treats a table whose rows are
flagged as factually false under the materiality bar (retry,
`failure_cause: agent`, rows quoted in `what_failed`) unless the response
labels those cells as inferred / not retrieved; one incidental token is
not a retry; a reconstructed mapping cannot be graded above 0.6 while a row
is flagged. The flags are stored on the post-mortem (`payload.grounding`)
and quoted into the next attempt's retry context as GROUNDING FLAGS FROM
PRIOR ATTEMPT. Origin: box session dce9a6de7f81, where a five-row
id→policy table with every token real and every pairing invented passed at
0.95 — replayed, all five rows flag.

### Outputs — three verdicts plus a turn digest

| Verdict | Meaning | What happens |
|---|---|---|
| `pass` | The agent fulfilled the request | Done. Session goes to FINALIZING → IDLE_READY. |
| `retry` | The agent missed the intent | Cron/worker/canary: re-run the turn from scout, with reflect's lessons + the prior turn-digest appended to the system prompt. A normal session's deferred grade is observe-only — no re-run, a notification is raised instead (deterministic gates can still clamp). |
| `escalate` | Cannot fix automatically | Surface to the user with reflect's analysis. |

When the verdict is `retry` or `escalate`, reflect emits a structured **turn digest** alongside the verdict. The digest is reflect's own record of what happened during the attempt: scout's plan summary, the tool calls that mattered (with verbatim `result_excerpt` of each), the agent's final response, key findings, and a one-line "what was tried." It is stored inside the post-mortem's `payload_json` and carried forward to the *next* scout invocation as `PRIOR ATTEMPT DIGEST` — so scout-N+1 can plan around real evidence, not just reflect's free-form summary. On `pass`, the digest is omitted by default (set `reflect_emit_digest_on_pass = true` to opt in for debugging or audit).

Reflect-N never sees attempt-(N-1)'s transcript directly. The chain is: each cycle slices its own transcript by scout marker, produces its own digest, and that digest — not the raw transcript — is what the next scout reads.

Alongside the verdict, a `retry` may carry **`retry_without_tools`** — up to five tool names to withhold from the next attempt. Names are validated against the live tool registry (hallucinated ones are dropped) and each is truncated to 80 chars. The exclusion is enforced twice: the names are subtracted from the schema slice the model is shown — overriding both the builtin set and the monotonically-growing allowlist — and the executor refuses them with an error result if the model calls one regardless. It is the one *mechanical* effector reflect has: "stop reaching for `browse_web` on this" becomes a constraint rather than a suggestion. The set clears at the next genuine user turn.

### Bounded retries

Each `retry` increments `retry_index` on the same `turn_id`. When `retry_index` reaches `reflect_max_retries` (default 2), retries stop — total of 3 attempts. The next reflect verdict at that point can only be `pass` or `escalate`. This applies to cron/worker/canary sessions; a normal session's deferred grade is observe-only, so it gets zero reflect-driven retries by default.

For worker sessions, `reflect_max_retries_worker` is a separate cap (default 2). Workers run on tighter budgets so you might want fewer retries there.

Two guards can stop retries before that cap:

- **Time budget.** If the session's remaining LLM time headroom is less than another attempt plausibly needs, reflect is skipped with `reflect.budget_exhausted {remaining_s, needed_s}`.
- **The cross-retry circuit breaker.** From the second retry onward, the last two post-mortems for the turn are compared. If both have verdict `retry`, the *same* `failure_cause`, and reasoning text at least 0.7 similar (`difflib.SequenceMatcher` over lowercased reasoning + diagnostic), the retry is refused. Pernix emits `reflect.circuit_breaker {attempts, signature, reasoning}`, writes a `notice` row into the transcript, and raises a notification titled "Retry stopped — same failure repeating". The counter is per-turn, so the two compared post-mortems always belong to the current turn.

  The reasoning: a retry is worth paying for when the next attempt might differ. Two byte-similar failures in a row are evidence that it won't — the loop is stuck on something a third identical attempt cannot fix, and continuing just spends budget to produce the same failure a third time.

### When reflect skips

- `reflect_enabled = false` — disabled entirely.
- `reflect_min_messages` (default 3) — turns shorter than this skip the gate. A one-liner ("yes thanks") doesn't need verification.

### Failure classification

When reflect returns `retry` or `escalate`, it also classifies the *cause*:

| Cause | What |
|---|---|
| `scout` | Scout's plan missed something the request wanted |
| `agent` | Agent ignored the plan or hallucinated |
| `skill` | A loaded skill led the agent astray |
| `task` | The task was misframed (often falls back to a `pass` after retry) |
| `env` | Environmental — tool failure, network, model timeout |

The cause feeds into Snooze for offline analysis. Repeated `agent`-class failures on a particular kind of prompt might trigger Snooze to extract a lesson into `pernix.lessons.md`.

### Tuning

| Setting | Default | When to change |
|---|---|---|
| `reflect_enabled` | `true` | Set `false` for raw debugging; the gate adds latency |
| `reflect_max_retries` | `2` | Tighten if your provider is expensive, loosen if you tolerate slower turns for higher quality |
| `reflect_max_retries_worker` | `2` | Set lower for workers if they're cheap to fail-fast |
| `reflect_min_messages` | `3` | Lower if even short turns deserve verification |
| `reflect_deferred_normal` | `true` | Defer the grade for normal sessions to an observe-only background pass; set `false` to restore the in-turn retry-capable gate for them |
| `reflect_defer_idle_s` | `300` | Quiet period after the session reaches `IDLE_READY` before a deferred grade runs |
| `reflect_emit_digest_on_pass` | `false` | Have reflect emit a turn digest even on `pass` verdicts (audit/debug). Default off — pass turns omit the digest to save output tokens. |
| `reflect_digest_max_chars_per_excerpt` | `2000` | Per-call cap on each tool result excerpt inside the turn digest. Defensive trim at parse time regardless of what the model emits. |
| `reflect_full_transcript` | `false` | **Deprecated.** Reflect now always sees the per-attempt transcript; this flag is a no-op. |
| *(reflect model)* | — | Reflect runs on the Primary role (`llm_model`); on failure it retries once on Backup (`fallback_model`) |

### Post-mortems

When reflect classifies a failure, the analysis is stored as a **post-mortem** record in the `post_mortems` table. These accumulate; Snooze sweeps them past `post_mortem_retention_days` (default 90) and synthesizes patterns into long-term memory.

---

## Snooze — idle-time housekeeping

Defined in `core/snooze.py`. Runs every `snooze_interval_ticks` (default 10 ticks ≈ 10 minutes), but only when no sessions are actively processing.

### What it does

Each cycle walks an ordered ladder of activities. `core/snooze.py` owns the lifecycle, the idle gate, and the ladder; the work itself lives next to the store it touches — memory-store surgery in `core/memory/sweeps.py`, retention pruners in `core/retention.py`. Later activities only run if the cycle isn't cancelled first, so the ordering is also a priority order:

| # | Activity | What |
|---|---|---|
| 1 | Catch-up distillation | Review sessions that ended without a turn digest (max 1 LLM call). |
| 2 | User insight extraction | Pull recurring preferences and facts into `user.profile.md`. |
| 2c | Skill requirements install | Hash-triggered: a skill whose `requirements.txt` changed gets its packages installed into the workspace venv (one skill per cycle, no LLM), then the registry rescans so the health flag clears. |
| 2d | Space suggestions | When `space_suggest_enabled`: at most once per `space_suggest_scan_interval_hours` and only after ten new ordinary sessions, one background-model call groups the last `space_suggest_window_days` of chats by kind of work; clusters pass a code gate (size and spread floors, no conversational clusters, declined-topic suppression, two per scan, five pending) and land as pending `space_suggestions` rows plus a bell notification. Never creates a space itself (`core/space_suggest.py`). |
| 3 | Memory deduplication | Every `snooze_dedup_interval_days` (default 7) per file: find near-duplicate entries; merge them. |
| 3b | Cross-file consolidation | Every `snooze_consolidation_interval_hours` (default 24): cluster related entries into the same file using `snooze_consolidation_cluster_threshold` (default 0.55). |
| 3c | Entry re-routing | Move entries filed in the wrong memory file. |
| 4 | Tag enrichment | Backfill missing `@tags:` on memory entries (no LLM). |
| 5 | Index reconciliation | Check the FTS5 index against the markdown files; reindex if stale. When `embedding_model` is set, also the embedding sweep: batch-embed new/stale entries for [semantic retrieval](../guides/memory-and-recall.md#semantic-retrieval) (every cycle, no-op when nothing is pending). |
| 6 | File splitting | Split memory files that have grown too large. |
| 7 | Cron cleanup | Prune old cron runs and their sessions. |
| 8 | Staleness pruning | Age out stale lessons and superseded facts. |
| 9 | Skill co-occurrence | Update which skills tend to load together. |
| 10 | Signal synthesis | Fold post-mortems into tool/skill performance counters (and, per model, into the routing counters behind scout's `[MODEL ROUTING INTEL]` brief). |
| 11 | Post-mortem TTL | Archive post-mortems past `post_mortem_retention_days` (default 90). |
| 12a | RLM run cleanup | Delete `data/workspace/rlm/<run_id>/` dirs + `rlm_runs` rows older than `rlm_run_retention_days` (default 30). Running runs are never touched. |
| 12b | Candor maintenance | When `candor_enabled`: run the admission gate, drain the observation buffer, checkpoint the store. When `adaptive_enabled` too: queue `routing_hint` edits for tools whose calibrated reliability regressed (the Candor producer). |
| 12c | Canary cleanup | When `canary_enabled`: prune `canary_runs` rows and their sessions past `canary_retention_days` (default 30), and nudge once per canary whose `last_reviewed` is over 90 days old. Never dispatches sweeps — those are enqueued for the next idle window. |
| 12d | Canary suite auto-maintenance | When `canary_auto_maintain`: promote vetted auto-admitted canaries, tag flapping ones flaky, retire long-green ones to quarantine, purge the quarantine (no LLM). A failing canary is never auto-mutated. |
| 13 | Refine pass | Whole-session refine (`core/refine.py`) — the single session-improvement rung: skill-edit proposals + lessons from any idle session, not gated on the reflect verdict. |
| 14 | Dream step | Idle-time introspection (`core/dream/`) — see [dream.md](dream.md). Only when `dream_enabled`. |
| 14b | Distillation coverage audit | When `distill_audit_enabled`: audit distillation coverage against one sampled raw transcript per run, under a daily budget; misses land in Candor and are written back to memory (`core/memory/audit.py`). |
| 16 | Telos step | The teleological slow loops (`core/telos/`) — see [telos.md](telos.md). Only when `telos_enabled`. |
| 15 | Adaptive layer | When `adaptive_enabled`: drain pending auto-applies (safe here — the idle window means no session's cached prefix is mid-turn), enqueue post-batch canary sweeps, evaluate the tripwire (no LLM) — see [canary-and-adaptive.md](canary-and-adaptive.md). |

Activity 16 running *before* 15 is deliberate, not a typo: Telos may queue adaptive edits (a `supported` claim becomes a `routing_hint`), so the adaptive drain has to come after it or those edits wait a whole cycle.

Numbering has gaps because it is historical, not ordinal — an activity that is removed does not renumber the ones after it, so cross-references in code and logs stay valid. Activity 2b (a separate snooze-reflect pass) was removed and folded into Activity 13.

A cycle runs until the ladder **completes** — there is no per-task time slice. Two things end it early:

- **User activity.** A new prompt, cron fire, or shutdown sets the cycle's cancel event, which aborts even in-flight LLM awaits. The interrupted activity records a watermark and resumes on the next cycle.
- **The hang backstop.** `snooze_max_cycle_seconds` (default 900) is runaway protection, not a budget — it only fires if an activity genuinely hangs. Local (Ollama) background models get 4x headroom, since slow local inference is normal there, not a hang.

### Cooperative scheduling

Snooze checks `idle_minutes` against `snooze_cooldown_minutes` (default 5). A session that just went idle won't trigger Snooze immediately — there's a cooldown so you don't get housekeeping running 30 seconds after every chat. Cycles also skip entirely when nothing happened since the last one — no activity, no work to review.

When a new session starts mid-cycle, Snooze yields immediately: the cancel event aborts the in-flight activity (including a pending LLM call), the activity records its watermark, and the next cycle picks up where it left off. Your work always wins.

For debugging, a localhost-only `POST /api/admin/snooze-cycle` triggers a cycle on demand, skipping the cadence and cooldown checks (but never the real gates — active sessions still refuse it). It also returns an `idle_blockers()` diagnostic explaining why a cycle *wouldn't* run.

### The cluster threshold

`snooze_consolidation_cluster_threshold` (default 0.55) controls how aggressive consolidation is. Two memory entries with a pair-similarity score above this threshold get clustered. Lower values = more aggressive merging (smaller, fewer files); higher values = more conservative (more files, less merging).

If you find your memory store getting cluttered with similar-but-not-quite-duplicate entries, try lowering this. If you find consolidation merging things that should stay separate, raise it.

### Disabling Snooze

`snooze_enabled = false` shuts it off. The memory-index health check (one of the things Snooze keeps fresh) can be run manually:

```bash
curl -X POST http://localhost:8090/api/memory/maintenance
```

This checks the FTS5 index against the markdown files and reindexes if stale. The LLM-backed tasks (dedup, consolidation, profile extraction) only run inside Snooze cycles.

---

## How Reflect and Snooze interact

- Reflect produces post-mortems (per-turn failure analyses).
- Snooze sweeps post-mortems and synthesizes patterns into long-term memory.
- The next time scout searches memory, lessons learned from prior failures may surface in recall — making the agent better at avoiding repeat mistakes.

This is the slow feedback loop. You won't see it move the needle on a single turn; over weeks it does.

---

## Observability

- **Reflect activity** is emitted as `reflect.start` / `reflect.done` SSE events (the verdict rides on `reflect.done`), plus `reflect.skipped`, `reflect.retry`, `reflect.escalate`, `reflect.exhausted`, `reflect.budget_exhausted`, `reflect.circuit_breaker`, `reflect.deferred_scheduled` and `reflect.deferred` markers. The UI's **State timeline** shows them inline with the turn.
- **Snooze cycle output** is logged to `data/logs/pernix.log`. Each cycle records what it ran and how long it took.
- **`POST /api/memory/maintenance`** runs the memory-index health check and returns the result inline.

If reflect or snooze is misbehaving, those are the first places to look.
