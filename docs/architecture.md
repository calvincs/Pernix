# How Pernix Works

A guided tour of what happens between "you send a message" and "the agent answers." Read this in order — it goes from the bird's-eye view down to the implementation details. If you want the formal state-machine specification with file:line citations, that lives in [internals/state-machine.md](internals/state-machine.md); this document is the conceptual companion.

---

## Bird's-Eye View

When you send a message to Pernix, it goes through five distinct phases:

```
  ┌──────────┐   ┌──────────┐   ┌─────────────┐   ┌──────────┐   ┌──────────┐
  │ Session  │──▶│  Scout   │──▶│ Agent Loop  │──▶│ Reflect  │──▶│Post-hooks│
  │ accepts  │   │ (plan)   │   │ (act)       │   │ (verify) │   │(cleanup) │
  └──────────┘   └──────────┘   └─────────────┘   └──────────┘   └──────────┘
                       ↓               ↓                ↓
                  fast model    main model        background
                  fresh ctx     full context      model
```

1. **Session** — your message is queued onto a session (a persistent conversation thread)
2. **Scout** — a fast planning agent reads the request, picks tools and skills, drafts an approach
3. **Agent Loop** — the main model executes the plan, calling tools and streaming responses
4. **Reflect** — a verification pass checks whether the user's intent was actually fulfilled
5. **Post-hooks** — auto-titling, memory distillation, worker cleanup happen in the background

The agent is also doing things you don't see: **compaction** trims old messages when context fills up, **workers** can run sub-agents in parallel on different models, and **Snooze** runs maintenance during idle time.

The rest of this document explains each piece.

---

## The Five Core Concepts

### 1. Sessions

A **session** is one persistent conversation thread. It has:

- A unique ID
- A list of messages (append-only — never mutated)
- A state (where it is in its lifecycle — see the state machine below)
- A queue of pending messages (if you send another while one is processing)
- Optional per-session overrides (model, context budget)

Sessions live in `data/sessions.db` (SQLite). They survive restarts. You can have many sessions open at once and switch between them in the UI.

> **Why sessions instead of one big chat?** Most agentic work fits naturally into discrete projects: "write this report," "analyze these logs," "build this feature." Each session has its own memory recall scope, its own workers, and its own state. You don't accumulate cross-task contamination.

### 2. Scout — the planner

Before the main agent runs, a **scout** runs first. Scout is a smaller, faster model (you configure it via `scout_model`) running in a fresh context window. Its job is to plan.

What scout does:

- Reads the user's new message
- Searches your persistent memory for relevant prior facts
- Lists available tools, skills, and workflows
- Decides which tools the main agent should be aware of, which skills to load, and what the high-level approach should be
- Submits a `ScoutReport` — the structured plan handed off to the main agent

Why this matters:

- The main agent doesn't see every tool definition every turn — only the ones scout decided are relevant. This keeps prompts focused and saves tokens.
- Scout has a fresh context, so its judgment isn't biased by long conversation history.
- If you have 30 skills installed, scout decides which 1–2 to load full instructions for, rather than always paying that token cost.

Scout is a real LLM agent — it can call its own (read-only) tools to investigate before submitting the report. It's defined in `core/scout/runner.py`.

### 3. The Agent Loop — the worker

Once scout finishes, the **main agent** takes over. This is where the user-facing response comes from.

The loop:

1. Compile the request payload: system prompt + identity files (SOUL/RULES/AGENTS) + memory recall results + scout report + conversation history + selected tool schemas
2. Stream a response from the LLM
3. If the response includes tool calls, execute them, append results to the conversation, and loop back to step 1
4. If the response is a final text answer (no tool calls), the loop exits
5. If the loop hits `max_tool_rounds` (default 10), it terminates with a "round ceiling" reason

While running, the loop emits SSE events: every token, every tool call, every tool result. The UI uses these to show real-time progress.

The main agent is defined in `core/agent.py`.

### 4. Reflect — the quality gate

After the main agent loop ends, **Reflect** checks whether the user's intent was actually fulfilled. It's a separate LLM call that sees the **current attempt's transcript** with verbatim tool result bodies — sliced from the most recent `scout` role marker forward — alongside the user's original ask, scout's plan, and a tool-execution summary. Tool result bodies are kept verbatim up to a 5000-char cap so reflect can verify factual claims against what the tools actually returned, not against its own training-data priors.

It produces a verdict — one of three values:

| Verdict | Meaning |
|---|---|
| `pass` | The agent fulfilled the request. Done. |
| `retry` | The agent missed the intent. Try again with these lessons. |
| `escalate` | Cannot fix automatically — surface this to the user. |

On `retry` or `escalate`, Reflect also emits a structured **turn digest** alongside the verdict (scout plan summary, tool calls with verbatim `result_excerpt` per call, key findings, what was tried). The digest is persisted inside the post-mortem and carried forward to the *next* Scout invocation as `PRIOR ATTEMPT DIGEST` — so Scout-N+1 plans against real evidence from the previous attempt, not just a free-form summary. Reflect-N never sees attempt-(N-1)'s transcript directly; only the digest crosses the boundary. This is bounded by `reflect_max_retries` (default 2), so a single user request can result in up to three Scout/Agent/Reflect cycles before giving up.

Reflect also classifies the *cause* of any failure (scout / agent / skill / task / env). This data feeds into Snooze for offline analysis.

Reflect is defined in `core/reflect.py`. Disable it with `reflect_enabled = false` in settings.

### 5. Snooze — idle housekeeping

When no sessions are actively processing, **Snooze** runs background maintenance. Every ~10 minutes (`snooze_interval_ticks`), it checks whether to run, and if so, performs:

- **Memory deduplication** — finds near-duplicate entries in your memory files and merges them
- **Memory consolidation** — clusters semantically related entries into the same file
- **User profile extraction** — pulls preferences and recurring patterns into a profile memory
- **Post-mortem cleanup** — old failure logs get summarized and archived

Snooze stops immediately when you start a new session — your work always takes priority. It's defined in `core/snooze.py`.

---

## The Session State Machine

Everything above is governed by a 10-state state machine (defined in `sessions/state_v2.py`). Each session is in exactly one state at any time. State transitions are logged to a `session_state_log` table and emitted as SSE `session.state_changed` events for the UI to follow along.

### The 10 active states

| State | What's happening |
|---|---|
| `IDLE_READY` | No active turn; waiting for a prompt |
| `SCOUTING` | Scout is running |
| `PROCESSING` | Main agent loop is running |
| `COMPACTING` | Context is being trimmed (proactive, critical, or after API overflow error) |
| `PAUSE_REQUESTED` | A worker was asked to pause; it'll observe the pause at the next round boundary |
| `PAUSED` | A worker is parked, waiting for resume signal |
| `CANCELLING` | User pressed cancel; agent is being torn down |
| `FINALIZING` | Post-hooks running (auto-title, distillation, worker cleanup) |
| `AWAITING_USER` | Agent paused via `ask_user` and is waiting for input |
| `AWAITING_WORKERS` | Parent session blocked on one or more spawned workers |

The enum also defines several **terminal/error markers** (`COMPLETE`, `ROUND_CEILING`, `COMPACTION_FAILED`, `CANCELLED`, `ERROR`, `SCOUT_ERROR`, `BUDGET_EXHAUSTED`) used as turn outcomes rather than active session states.

### A typical turn (the happy path)

```
IDLE_READY → SCOUTING → PROCESSING → FINALIZING → IDLE_READY
              (plan)      (act)        (cleanup)
```

### A turn with reflection-driven retry

```
IDLE_READY → SCOUTING → PROCESSING → FINALIZING → SCOUTING → PROCESSING → FINALIZING → IDLE_READY
                                       ↑
                               Reflect said "retry"
```

The retry has the same `turn_id` as the original attempt but `retry_index = 1`. Both runs are visible in the timeline UI.

### A turn that needs more info

```
IDLE_READY → SCOUTING → PROCESSING → AWAITING_USER → SCOUTING → PROCESSING → FINALIZING → IDLE_READY
                          (ask_user)    ↑                ↑
                                  user answers     a NEW turn_id
                                                   begins
```

When the agent calls the `ask_user` tool, the session enters `AWAITING_USER` and stops consuming LLM resources. As soon as you answer (via the UI or `POST /api/questions/{id}/answer`), a new turn begins from Scout.

### A turn that hits its context budget

```
IDLE_READY → SCOUTING → PROCESSING → COMPACTING → PROCESSING → FINALIZING → IDLE_READY
                                       ↑
                               context > 75% full
                               (configurable)
```

Compaction is non-destructive: original messages are kept in the database. It's a *view transform* — old messages are replaced with a compact summary in the prompt, but you can still scroll back through the full history in the UI.

For the complete transition graph (every edge with its trigger reason), see [internals/state-machine.md §0.2](internals/state-machine.md).

### Boot-time reconciliation

On every startup, the session manager sweeps the database for sessions stuck in `PROCESSING` or `AWAITING_WORKERS` — both indicate a server crash mid-turn. Those sessions are reset to `IDLE_READY` immediately so the reaper's 5-minute tick doesn't have to find them first. A log line is emitted for each session recovered:

```
INFO pernix.api  Reconciled 2 stuck PROCESSING session(s) at startup
```

### Reaper rules (summary)

The maintenance heartbeat runs every 60 seconds; the reaper fires every 5 ticks (5 minutes). Each state has its own timeout rule:

| State | Reaper action | Threshold |
|-------|--------------|-----------|
| `PROCESSING` | Unstick → `IDLE_READY` (only if no background tasks running) | 300s idle |
| `FINALIZING` | Force-unstick → `IDLE_READY` (only if no background tasks running) | 120s idle |
| `COMPACTING` | Force-unstick via `compaction-failed` → `FINALIZING` | 120s idle |
| `CANCELLING` | Force-unstick → `IDLE_READY` | 30s idle |
| `PAUSE_REQUESTED` | Unstick → `IDLE_READY` | 60s idle |
| `PAUSED` | Safety net cancel | 24h idle or parent deleted |
| `AWAITING_USER` | Unstick → `IDLE_READY` (if no question row exists) | 1800s idle |
| `AWAITING_WORKERS` | Resume or timeout | 3600s idle |
| `IDLE_READY` | Reap from memory | 1800s idle, no subscribers |

The `FINALIZING` and `PROCESSING` checks both guard with `not session.has_background_tasks` — post-hooks (reflect, distillation, auto-title) hold a background reference while running, so the reaper never cuts them short.

---

## How Messages Are Queued

Sessions are single-threaded — only one turn runs at a time per session. If you send another message while one is processing, it gets queued.

Two special cases:

**Rapid-fire combining.** If you send a follow-up within 3 seconds of the previous one, Pernix folds it into the running turn's database row instead of starting a new turn. This avoids the "I sent two messages and got confused" problem when you're typing quickly.

**Backpressure.** If the queue depth exceeds `max_pending_messages` (default 10), further submissions get rejected with a `session.queue_full` event.

---

## Workers — parallel sub-agents

A regular session has one agent loop running at a time. But the agent can spawn **workers** — sub-agents that run in their own session, in parallel, on whatever model is best for their task.

Use cases:

- Have a powerful but slow model do the high-level planning, while delegating individual file edits to a fast cheap model
- Run three workers in parallel to investigate three different aspects of a problem, then synthesize their findings
- Use a vision-capable model for one task and a code-specialist for another, all in the same conversation

Workers are flat — a worker can't spawn its own workers. The parent session waits for workers to complete (or explicitly checks on them via `check_workers`), then folds their deliverables back into its own context.

Workers can be paused at round boundaries via `pause_worker` and resumed later. Useful for "wait, don't keep going, let me think."

Workers are defined in `core/extensions/orchestration/__init__.py`.

---

## Memory — what the agent remembers

Pernix has two distinct kinds of memory:

### Short-term: the conversation
Every message in a session is stored in `data/sessions.db`. The agent sees these directly when running. Compaction can summarize old turns when context fills, but the originals stay in the database.

### Long-term: the memory store
Persistent facts, decisions, and lessons live as **markdown files** in `data/memories/`. Each file groups related entries (e.g., `user.profile.md`, `pernix.decisions.md`, `pernix.debugging.md`).

Memory is searched at the start of each turn via BM25 full-text search. Top-scoring entries get injected into the system prompt. The agent can also read or write entries directly using memory tools.

The memory store is **append-only and human-readable**. You can open the markdown files in any editor and read what your agent has learned about you. You can also delete or edit them — they're just files.

Memory entries are scored, and old "lessons" decay over time (entries tagged as lessons get progressively deweighted after 14, 60, and 180 days). Deduplication runs during Snooze.

---

## Compaction in detail

Compaction has three triggers:

| Trigger | When | What happens |
|---|---|---|
| **Proactive** | Context utilization > 75% | Background — compacted next turn |
| **Critical** | Context utilization > 85% | Mid-turn — compact immediately to make room |
| **Overflow** | Provider returns "context too long" error | Reactive — compact and retry the same request |

Compaction algorithm (high level): keep the most recent ~51K tokens verbatim, replace older messages with an LLM-summarized digest. The original messages stay in the database; only the *view* sent to the next LLM call is changed.

This means:

- The UI always shows full history
- You can scroll back through everything
- Compaction is repeatable — if a turn fails, the next turn can re-compact from scratch

Implementation: `core/context/compaction.py` and `core/context/compiler.py`.

---

## LLM Routing

Pernix supports multiple providers simultaneously. The router (`core/llm/router.py`) decides which provider handles each request based on:

1. **Model name** — `anthropic/claude-sonnet-4.6` clearly maps to OpenRouter; `qwen3:32b` maps to Ollama
2. **Registry** — a runtime catalog populated from both providers' `/v1/models` endpoints
3. **Conflict policy** — when both providers have a model with the same name, Ollama wins (local, free, lower latency) unless you've explicitly added the model to `openrouter_models`

If OpenRouter returns a rate-limit, quota, or context-overflow error, the router falls back to your configured `fallback_model` on Ollama. This is automatic — the user doesn't see the failure.

Concurrency is controlled per-provider via semaphores: `llm_max_concurrent` for Ollama, `openrouter_max_concurrent` for OpenRouter. Workers and the main session all share these slots fairly.

---

## What Gets Persisted

| Data | Location | Format |
|---|---|---|
| Sessions, messages, tool calls | `data/sessions.db` | SQLite |
| State machine transition log | `data/sessions.db` (`session_state_log` table) | SQLite |
| Memory entries (long-term) | `data/memories/*.md` | Markdown |
| Memory search index | `data/memories/_index.db` | SQLite FTS5 |
| Workspace files (agent's working dir) | `data/workspace/` | Filesystem |
| Settings | `data/settings.json` | JSON |
| API keys | `.env` | dotenv |
| Skills | `data/skills/` | Markdown + scripts |
| Agent identity | `data/agent/SOUL.md`, `RULES.md`, `AGENTS.md` | Markdown |

Everything except `.env` and `settings.json` can be wiped with `python run.py --rebuild`.

---

## Events — How the UI Stays in Sync

Pernix uses Server-Sent Events (SSE) for real-time updates. The UI maintains one persistent connection per session at `GET /api/sessions/{id}/events` and processes events as they arrive.

Key event types:

- `session.state_changed` — every state transition
- `scout.start` / `scout.done` — scout phase boundaries
- `stream.token` — every streamed text token
- `tool.call.start` / `tool.call.result` — every tool invocation
- `turn.complete` — turn finished
- `ask_user` — agent is waiting for input
- `worker.*` — worker lifecycle (spawned, done, error, paused, resumed)

Every event has a `_seq` (monotonically increasing sequence number). On reconnect, the client sends `Last-Event-ID: <last_seq>` and the server replays anything it missed.

Full event catalog: [api.md](api.md#real-time-events-sse).

---

## Where to Read Next

Now that you have the conceptual model, drill into specific areas:

- **[configuration.md](configuration.md)** — every knob and dial
- **[authoring/writing-skills.md](authoring/writing-skills.md)** — write your own capabilities
- **[api.md](api.md)** — full REST + SSE reference
- **[internals/state-machine.md](internals/state-machine.md)** — the formal architecture spec with file:line citations and complete state transition graph

If you want to read the code:

| Concept | Start here |
|---|---|
| Session orchestration | `sessions/manager.py`, `sessions/state_v2.py` |
| Scout | `core/scout/runner.py` |
| Main agent loop | `core/agent.py` |
| Reflect | `core/reflect.py` |
| Compaction | `core/context/compaction.py`, `core/context/compiler.py` |
| LLM routing | `core/llm/router.py`, `core/llm/registry.py` |
| Workers | `core/extensions/orchestration/__init__.py` |
| Memory store | `core/memory/store.py` |
| Snooze | `core/snooze.py` |

The codebase is intentionally small and readable. Pick a thread and pull on it.
