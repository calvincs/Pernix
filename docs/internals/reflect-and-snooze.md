# Reflect and Snooze

Two non-obvious subsystems that run alongside the agent loop:

- **Reflect** — a quality-gate pass that runs after every turn, verifies whether the user's intent was actually fulfilled, and can trigger bounded retries if it wasn't.
- **Snooze** — idle-time housekeeping that runs in the background when no sessions are active, deduplicating memory, consolidating clusters, extracting user-profile facts, and archiving old post-mortems.

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

For worker sessions, `reflect_max_retries_worker` is a separate cap (default inherits from `reflect_max_retries`). Workers run on tighter budgets so you might want fewer retries there.

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
| `reflect_max_retries_worker` | inherits | Set lower for workers if they're cheap to fail-fast |
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

| Task | Cadence | What |
|---|---|---|
| Memory deduplication | Every `snooze_dedup_interval_days` (default 7) per file | Find near-duplicate entries; merge them. |
| Memory consolidation | Every `snooze_consolidation_interval_hours` (default 24) | Cluster semantically related entries into the same file using `snooze_consolidation_cluster_threshold` (default 0.55). |
| User profile extraction | Periodic | Pull recurring preferences and facts into `user.profile.md`. |
| Post-mortem cleanup | Past retention | Synthesize patterns from accumulated post-mortems; archive old ones. |

Each task is bounded by `snooze_max_cycle_seconds` (default 60). If a task can't finish in that window, it yields and resumes on the next tick.

### Cooperative scheduling

Snooze checks `idle_minutes` against `snooze_cooldown_minutes` (default 5). A session that just went idle won't trigger Snooze immediately — there's a cooldown so you don't get housekeeping running 30 seconds after every chat.

When a new session starts, Snooze yields immediately (`asyncio.CancelledError` propagates up, the in-progress task records its progress, and the next snooze tick picks up where it left off).

### The cluster threshold

`snooze_consolidation_cluster_threshold` (default 0.55) controls how aggressive consolidation is. Two memory entries with a pair-similarity score above this threshold get clustered. Lower values = more aggressive merging (smaller, fewer files); higher values = more conservative (more files, less merging).

If you find your memory store getting cluttered with similar-but-not-quite-duplicate entries, try lowering this. If you find consolidation merging things that should stay separate, raise it.

### Disabling Snooze

`snooze_enabled = false` shuts it off. You can always run maintenance manually:

```bash
curl -X POST http://localhost:8090/api/memory/maintenance
```

This runs the same set of tasks immediately.

---

## How Reflect and Snooze interact

- Reflect produces post-mortems (per-turn failure analyses).
- Snooze sweeps post-mortems and synthesizes patterns into long-term memory.
- The next time scout searches memory, lessons learned from prior failures may surface in recall — making the agent better at avoiding repeat mistakes.

This is the slow feedback loop. You won't see it move the needle on a single turn; over weeks it does.

---

## Observability

- **Reflect verdicts** are emitted as `reflect.verdict` SSE events. The UI's timeline drawer shows them inline with the turn.
- **Snooze cycle output** is logged to `data/logs/snooze.log` (or wherever your log config points). Each cycle records what it ran and how long it took.
- **`POST /api/memory/maintenance`** triggers a Snooze cycle and returns the result inline — useful for testing tuning changes.

If reflect or snooze is misbehaving, those are the first places to look.
