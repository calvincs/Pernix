# Reflect and Snooze

Two non-obvious subsystems that run alongside the agent loop:

- **Reflect** — a quality-gate pass that runs after every turn, verifies whether the user's intent was actually fulfilled, and can trigger bounded retries if it wasn't.
- **Snooze** — idle-time housekeeping that runs in the background when no sessions are active, deduplicating memory, consolidating clusters, extracting user-profile facts, archiving old post-mortems — and, when enabled, running the [Dream](dream.md) introspection step.

Both are off the critical path of any single user request, but they shape how Pernix behaves over weeks and months. This page explains what each does and where to tune it.

---

## Reflect — the post-turn quality gate

Defined in `core/reflect.py`. Runs after the main agent loop completes, before the session returns to `IDLE_READY`.

### Inputs

Reflect sees the **current attempt's transcript** with verbatim tool result bodies — sliced from the most recent `scout` role marker forward, so on a retry it sees only the work that just happened, not the whole growing session. The evidence blob is:

- The original user message that started the turn.
- A preamble with workspace files, termination history, scout's plan/approach, tool execution summary (counts, failures, last few errors), and any workflow run statuses.
- An `ATTEMPT TRANSCRIPT` section containing every assistant message, tool call, and tool result from this attempt — tool result bodies are kept verbatim up to a per-result cap (5000 chars), with longer results truncated and marked `[+N chars truncated]`.
- The agent's final response (echoed at the end for grounding).

This is what lets reflect verify factual claims against what the tools actually returned, instead of grading them against its own training-data priors.

### Outputs — three verdicts plus a turn digest

| Verdict | Meaning | What happens |
|---|---|---|
| `pass` | The agent fulfilled the request | Done. Session goes to FINALIZING → IDLE_READY. |
| `retry` | The agent missed the intent | Re-run the turn from scout, with reflect's lessons + the prior turn-digest appended to the system prompt. |
| `escalate` | Cannot fix automatically | Surface to the user with reflect's analysis. |

When the verdict is `retry` or `escalate`, reflect emits a structured **turn digest** alongside the verdict. The digest is reflect's own record of what happened during the attempt: scout's plan summary, the tool calls that mattered (with verbatim `result_excerpt` of each), the agent's final response, key findings, and a one-line "what was tried." It is stored inside the post-mortem's `payload_json` and carried forward to the *next* scout invocation as `PRIOR ATTEMPT DIGEST` — so scout-N+1 can plan around real evidence, not just reflect's free-form summary. On `pass`, the digest is omitted by default (set `reflect_emit_digest_on_pass = true` to opt in for debugging or audit).

Reflect-N never sees attempt-(N-1)'s transcript directly. The chain is: each cycle slices its own transcript by scout marker, produces its own digest, and that digest — not the raw transcript — is what the next scout reads.

### Bounded retries

Each `retry` increments `retry_index` on the same `turn_id`. When `retry_index` reaches `reflect_max_retries` (default 2), retries stop — total of 3 attempts. The next reflect verdict at that point can only be `pass` or `escalate`.

For worker sessions, `reflect_max_retries_worker` is a separate cap (default 2). Workers run on tighter budgets so you might want fewer retries there.

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
| `reflect_emit_digest_on_pass` | `false` | Have reflect emit a turn digest even on `pass` verdicts (audit/debug). Default off — pass turns omit the digest to save output tokens. |
| `reflect_digest_max_chars_per_excerpt` | `2000` | Per-call cap on each tool result excerpt inside the turn digest. Defensive trim at parse time regardless of what the model emits. |
| `reflect_full_transcript` | `false` | **Deprecated.** Reflect now always sees the per-attempt transcript; this flag is a no-op. |
| `reflect_model` | empty | Override which model runs reflect; empty uses `background_model` |

### Post-mortems

When reflect classifies a failure, the analysis is stored as a **post-mortem** record in the `post_mortems` table. These accumulate; Snooze sweeps them past `post_mortem_retention_days` (default 90) and synthesizes patterns into long-term memory.

---

## Snooze — idle-time housekeeping

Defined in `core/snooze.py`. Runs every `snooze_interval_ticks` (default 10 ticks ≈ 10 minutes), but only when no sessions are actively processing.

### What it does

Each cycle walks an ordered ladder of activities (`core/snooze.py`). Later activities only run if the cycle isn't cancelled first, so the ordering is also a priority order:

| # | Activity | What |
|---|---|---|
| 1 | Catch-up distillation | Review sessions that ended without a turn digest (max 1 LLM call). |
| 2 | User insight extraction | Pull recurring preferences and facts into `user.profile.md`. |
| 2b | Skill improvements | Propose skill edits + lessons from session reflects. |
| 3 | Memory deduplication | Every `snooze_dedup_interval_days` (default 7) per file: find near-duplicate entries; merge them. |
| 3b | Cross-file consolidation | Every `snooze_consolidation_interval_hours` (default 24): cluster related entries into the same file using `snooze_consolidation_cluster_threshold` (default 0.55). |
| 3c | Entry re-routing | Move entries filed in the wrong memory file. |
| 4 | Tag enrichment | Backfill missing `@tags:` on memory entries (no LLM). |
| 5 | Index reconciliation | Check the FTS5 index against the markdown files; reindex if stale. |
| 6 | File splitting | Split memory files that have grown too large. |
| 7 | Cron cleanup | Prune old cron runs and their sessions. |
| 8 | Staleness pruning | Age out stale lessons and superseded facts. |
| 9 | Skill co-occurrence | Update which skills tend to load together. |
| 10 | Signal synthesis | Fold post-mortems into tool/skill performance counters. |
| 11 | Post-mortem TTL | Archive post-mortems past `post_mortem_retention_days` (default 90). |
| 12 | Workflow run cleanup | Delete workflow run dirs beyond keep-10-per-workflow or older than 30 days. |
| 12a | RLM run cleanup | Delete `data/workspace/rlm/<run_id>/` dirs + `rlm_runs` rows older than `rlm_run_retention_days` (default 30). Running runs are never touched. |
| 12b | Candor maintenance | When `candor_enabled`: run the admission gate, drain the observation buffer, checkpoint the store. |
| 13 | Refine pass | Whole-session refine — broader-gate sibling of Activity 2b. |
| 14 | Dream step | Idle-time introspection (`core/dream/`) — see [dream.md](dream.md). Only when `dream_enabled`. |

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

- **Reflect activity** is emitted as `reflect.start` / `reflect.done` SSE events (the verdict rides on `reflect.done`), plus `reflect.skipped` / `reflect.exhausted` markers. The UI's timeline drawer shows them inline with the turn.
- **Snooze cycle output** is logged to `data/logs/pernix.log`. Each cycle records what it ran and how long it took.
- **`POST /api/memory/maintenance`** runs the memory-index health check and returns the result inline.

If reflect or snooze is misbehaving, those are the first places to look.
