# Pernix Agent Harness — Request Architecture

Complete walkthrough of how requests flow through the harness: session lifecycle, the agent turn loop, sub-agent workers, and the loop inside workers. File:line citations reference the repo at the time of writing.

> **True state machine (v2)** — amended 2026-04-20, legacy layer deleted 2026-08-07
>
> The "five states + orthogonal flags" model this document originally described is **gone from the code**, not merely superseded. The only state machine is `sessions.state_v2`: a ten-state enum, one mutator (`transition()`), one persisted column (`sessions.state_v2`, migration v16), one log table (`session_state_log`, migration v13), and one `session.state_changed` SSE event per transition. The old 5-value enum, the `session.state` mirror field it lived in, and the bridge tables that translated between them were deleted along with the redundant per-transition write to the legacy `sessions.state` column (which still exists in the schema — see the note in `db/database.py` — but is no longer maintained).
>
> §0 below is the current specification. §§1–5 are retained for the parts of the turn pipeline that did not change (scout, reflect, snooze, cancel, workers); the subsections that documented the deleted enum and its orthogonal flags have been removed.

---

## 0. State Machine v2 (current)

### 0.1 States (10)

| State | Purpose | Accepts new prompt? |
|---|---|---|
| `IDLE_READY` | No active turn; ready for any prompt | Yes |
| `SCOUTING` | Scout agent running (fresh context, fast model) | Queued |
| `PROCESSING` | Main agent loop running | Queued |
| `COMPACTING` | Context compaction in-flight (proactive / critical / API overflow) | Queued |
| `PAUSE_REQUESTED` | Pause asked for; the agent hasn't reached its next pre-round gate yet | Queued |
| `PAUSED` | Session parked at `await session.pause_event.wait()` | Queued |
| `CANCELLING` | `cancel_requested` observed; post-hooks skipped, transient | **Rejected** |
| `FINALIZING` | Post-hooks running (title, distill, reflect, eval, worker finalize) | Queued |
| `AWAITING_USER` | `ask_user` posted a question; turn terminated cleanly | Rejected (answer via `/api/questions/{id}/answer`) |
| `AWAITING_WORKERS` | Parent session parked while watched workers run; auto-resumes when they settle | Queued |

Deleted from the legacy enum: `ERROR` (folded into `FINALIZING` with `termination_reason="error"`), `DELETED` (was never set).

### 0.2 Transition graph

```
IDLE_READY       → SCOUTING          reason=prompt-arrived
SCOUTING         → PROCESSING        reason=scout-done
SCOUTING         → FINALIZING        reason=scout-error       (term=scout_error)
SCOUTING         → CANCELLING        reason=cancel-requested
PROCESSING       → COMPACTING        reason=compact-{proactive|critical|overflow}
COMPACTING       → PROCESSING        reason=compact-done
COMPACTING       → FINALIZING        reason=compaction-failed|agent-error
COMPACTING       → CANCELLING        reason=cancel-requested
PROCESSING       → AWAITING_USER     reason=ask-user
PROCESSING       → PAUSE_REQUESTED   reason=pause-requested   (workers and main sessions)
PROCESSING       → CANCELLING        reason=cancel-requested
PROCESSING       → FINALIZING        reason=loop-complete|round-ceiling|agent-error
PAUSE_REQUESTED  → PAUSED            reason=pause-observed
PAUSE_REQUESTED  → CANCELLING        reason=cancel-during-pause
PAUSED           → PROCESSING        reason=resume
PAUSED           → CANCELLING        reason=cancel-during-pause
CANCELLING       → IDLE_READY        reason=cancel-complete|cancel-timeout
FINALIZING       → SCOUTING          reason=reflect-retry|eval-retry  (same turn_id, retry_index++)
FINALIZING       → IDLE_READY        reason=turn-complete|finalize-error
AWAITING_USER    → SCOUTING          reason=answer-received    (new turn_id, parent_turn_id=prev)
AWAITING_USER    → CANCELLING        reason=cancel-requested
AWAITING_USER    → IDLE_READY        reason=question-dismissed
PROCESSING       → AWAITING_WORKERS  reason=workers-dispatched
AWAITING_WORKERS → SCOUTING          reason=workers-complete
AWAITING_WORKERS → IDLE_READY        reason=worker-timeout
AWAITING_WORKERS → CANCELLING        reason=cancel-requested
(6 states)       → IDLE_READY        reason=reaper-unstick     (SCOUTING, PROCESSING, PAUSE_REQUESTED, PAUSED, AWAITING_USER, AWAITING_WORKERS — no such edge from COMPACTING, CANCELLING, FINALIZING or IDLE_READY)
(any active)     → IDLE_READY        reason=cancel-timeout     (cancel raced the turn's own exit)
```

The escape-hatch rows are shorthand: `TRANSITIONS` in `sessions/state_v2.py` spells out one edge per source state rather than a wildcard, so an unexpected `(from, reason)` pair is still detected and logged as an invariant violation.

No more `force_state()`. If a situation needs to "force," the edge is in the graph with an explicit reason (e.g. `reaper-unstick`, `cancel-timeout`, `finalize-error`).

### 0.3 State log (migration v13)

Table: `session_state_log` (append-only). Columns: `session_id, turn_id, parent_turn_id, retry_index, compaction_count, from_state, to_state, reason, termination_reason, reflect_count, eval_count, timestamp_ms, elapsed_ms`.

- `turn_id` increments on `prompt-arrived`, `answer-received` and `workers-complete`. An answer turn gets `parent_turn_id = <previous turn_id>` so the UI can group multi-round dialogs.
- `retry_index` increments on `reflect-retry` and `eval-retry`; all rows in a retry share the same `turn_id`.
- `compaction_count` increments each time PROCESSING → COMPACTING happens within a retry.
- Retention: `retention.prune_cron()` (`core/retention.py:33-54`) prunes rows older than 30 days while keeping the most recent 500 per session, so the last turn of every session stays inspectable regardless of age. Snooze's Activity 7 (`cron_cleanup`) and maintenance's 24h fallback own the cadence; the sweep itself lives in `core/retention.py`, not `core/snooze.py`.

### 0.4 SSE events

- `session.state_changed {from, to, reason, turn_id, retry_index, compaction_count, termination_reason, parent_turn_id}` — emitted for every transition. Canonical lifecycle signal.
- `session.prompt_rejected {reason: "awaiting_user" | "cancelling" | "queue_full"}` — emitted when a prompt is refused.
- `worker.done {worker_id, termination_reason, error}` — emitted on parent session when a worker's full turn settles.

Existing payload-detail events (`scout.start`, `stream.token`, `tool.call.*`, `context.compacting`, `reflect.*`, `turn.complete`, `worker.started`) remain and continue to fire. Later additions on the same stream:

- `reflect.circuit_breaker {attempts, signature, reasoning}` (`sessions/hooks.py:1275`) — a reflect `retry` was refused because the last two attempts failed identically.
- `goal.budget_exceeded {reason}` (`core/agent.py:1460`) — the in-turn budget checkpoint tripped mid-round; the loop breaks with `termination_reason="budget_exhausted"`.
- `goal.continuation {goal_id, ordinal, budget}` (`sessions/manager.py:676`) — an auto-continuation turn was enqueued for the active goal.
- `context.view_pruned {stubbed}` (`core/context/compiler.py:1201`) — budget-gated view pruning stubbed N oversized tool results out of the compiled view.
- `session.message_combine_skipped {message_id, reason}` (`sessions/manager.py:1109`) — a rapid-fire message could not be folded into the in-flight turn and was queued instead.
- `gates.done {attempt, total, failed, names_failed}` (`sessions/hooks.py:407`) — deterministic goal gates finished.

Every one of these must also appear in `EVENT_TYPES` in `static/js/sse.js`: `EventSource` only dispatches to listeners registered by exact name, so an unlisted event is silently dropped, `_lastSeq` never advances for it, and the client's gap detection fires a spurious soft reload on the next subscribed event.

### 0.5 Flag disposition

| Flag (was) | Now |
|---|---|
| `post_hooks_complete` | Derived read-only property: `True` iff `state == IDLE_READY` |
| `waiting_for_input` | Derived read-only property: `True` iff `state == AWAITING_USER` |
| `cancel_requested` | Kept as cooperative bool signal (HTTP cancel endpoint sets it outside the lock) |
| `pause_event` | Kept as asyncio primitive (mirrors `state ∈ {PAUSE_REQUESTED, PAUSED}`) |
| `reflect_retry_requested` / `eval_retry_requested` | Kept as internal post-hooks gates |
| `has_background_tasks` | Kept (orthogonal — snooze/distill, not turn state) |
| `termination_reason` | Kept, typed as `TerminationReason` enum, copied into state_log |

### 0.6 Prompt acceptance policy

Enforced in `Manager.prompt()` (`sessions/manager.py`):
- **IDLE_READY** / **AWAITING_USER** → run now. AWAITING_USER routes via `answer-received` automatically because `_run_agent_safe` detects the starting state.
- **SCOUTING** / **PROCESSING** / **COMPACTING** / **PAUSE_REQUESTED** / **PAUSED** / **FINALIZING** → queue on `pending_messages`.
- **CANCELLING** → reject with `session.prompt_rejected{reason:"cancelling"}`.

### 0.7 Reaper rules (10-state)

Sessions/manager.py `reap_idle_sessions()`:

| State | Rule |
|---|---|
| IDLE_READY | Reap from memory if `idle > max_idle`, no subscribers, no background refs |
| SCOUTING | Reap if `idle > 2×max_idle` and no subscribers |
| PROCESSING | Force IDLE_READY (reason=reaper-unstick) at 5 min if no background refs |
| COMPACTING | Force FINALIZING (compaction-failed) at 120s |
| PAUSE_REQUESTED | Force IDLE_READY at 60s |
| PAUSED | Never reap for inactivity. Safety net: force IDLE_READY at 24h or if parent deleted |
| CANCELLING | Force IDLE_READY (cancel-timeout) at 30s |
| FINALIZING | Force IDLE_READY (finalize-error) at 120s |
| AWAITING_USER | Never reap for inactivity. Force IDLE_READY only if the question row is gone and `idle > max_idle` |
| AWAITING_WORKERS | Auto-resumes when watched workers settle; forced IDLE_READY only when the watch-set is empty and idle ≥ `max_idle` (1800s) |

### 0.8 API / observability

- `GET /api/sessions/{id}/status` — now includes `state` (new 10-value enum, defined in `sessions/state_v2.py:SessionStateV2`), `compat_status` (legacy 3-value for CLI compat), `turn_id`, `retry_index`, `termination_reason`.
- `GET /api/sessions/{id}/state-log?since_id=<id>&limit=<n>` — paginated replay of the transition log.
- `POST /api/sessions/{id}/workers/{wid}/pause` / `/resume` — HTTP wrappers over the state-machine-aware `pause_worker` / `resume_worker` tools.
- Frontend timeline drawer (`openTimeline()` in `static/js/components/modals/timeline.js:57`) reads from `/state-log` and subscribes to `session.state_changed` for live updates.

---

## Table of Contents

1. [Session Lifecycle](#1-session-lifecycle)
2. [Agent Turn Loop](#2-agent-turn-loop-tool-calls-within-a-session)
3. [Sub-Agent Workers](#3-sub-agent-workers)
4. [Loop Inside a Worker](#4-loop-inside-a-worker)
5. [Summary Table](#5-summary-table)

> **Archive.** Sections 1-5 predate the v2 state machine. Their descriptions of the turn pipeline — scout, reflect, ask_user, cancel, snooze, worker orchestration — still hold, and the file:line citations are a useful map even where they have drifted. Their descriptions of *state* do not: read every mention of a state name, a transition, or the `post_hooks_complete` / `waiting_for_input` gates against §0, which is authoritative.

---

## 1. Session Lifecycle

### 1.1 Entry points

Three ways a request lands in a session:

- **REST** — `POST /api/chat` at `api/routers/chat.py:149-184` → `Manager.get_or_create()` (`sessions/manager.py:32-47`) → stores user message → `Manager.prompt()`
- **Cron / snooze** — idle-time scheduler creates sessions with `session_type="cron"` for memory consolidation/distillation
- **Worker spawn** — a parent session calls `spawn_worker` (`core/extensions/orchestration/__init__.py:49`) which creates a child session with `session_type="worker"` and `parent_session_id` set

### 1.2 The lock and the queue (`manager.py:804`)

`Manager.prompt()` acquires `session.lock` (asyncio.Lock at `state.py:80`), then:

- **ready** → clear reflect/eval retry flags, clear `error`/`termination_reason`, launch `_run_agent_safe()` (`manager.py:147-176`). Which states count as ready, and which reject rather than queue, is §0.6.
- **busy** → append to `session.pending_messages` deque, emit `session.queued` (`manager.py:137-145`)
- **queue full** (`len >= max_pending_messages`) → reject with `session.queue_full` event (`manager.py:127-135`)

When the turn finishes, `_process_pending()` (`manager.py:503-517`) drains the next queued message under the same lock.

Note: `manager.prompt()` also **cancels snooze** unconditionally (`manager.py:118-121`: `get_snooze().request_cancel()`) — user work always preempts background consolidation.

### 1.3 Full turn timeline (with every phase)

```
prompt() arrives
  ├─ cancel snooze                                (manager.py:118-121)
  └─ acquire session.lock
      ├─ IDLE/ERROR? → reset flags, launch _run_agent_safe   (manager.py:126, 147-176)
      └─ else → enqueue or reject (queue_full)

_run_agent_safe:                                  (manager.py:1124)
  IDLE → SCOUTING                                 (manager.py:188)
       → run_scout                                [scout.start / scout.done]
  SCOUTING → PROCESSING                           (manager.py:228)
       → run_agent()                              [stream tokens, tool calls]

       ┌─ INSIDE the agent loop, per-round checks (agent.py:807-825):
       │    · cancel_requested → return (termination="cancelled")
       │    · waiting_for_input → persist "waiting" assistant msg,
       │                           set termination="complete", return
       │                           (ask_user suspends the TURN, not the state)
       └─ normal exit → termination ∈
              {complete, round_ceiling, compaction_failed}

  except:
       CancelledError → termination="cancelled"
       Exception     → state → ERROR, termination="error"

  finally:                                        (manager.py:251-331)
    force IDLE if in (PROCESSING | SCOUTING | ERROR)

    ── POST-HOOKS PHASE (state==IDLE, post_hooks_complete==False) ──
    if not cancelled:                             (manager.py:263)
      while True:                                 (manager.py:272-295)
        _run_post_hooks()  → title, distill, reflect, eval
        if reflect_retry_requested and count < cap and queue empty:
          _run_agent_retry()  → SCOUTING → PROCESSING → IDLE again
          continue
        if eval_retry_requested and count < cap and queue empty:
          _run_agent_retry()
          continue
        break
    ────────────────────────────────────────────────────────────────

    restore per-session model override             (manager.py:300-308)
    if worker: _finalize_worker                    (manager.py:314-319)
    post_hooks_complete = True                     (manager.py:324)
    emit turn.complete                             (manager.py:327)
    _process_pending()                             (manager.py:330-331)
```

> **Archival note.** The `ERROR` branch drawn above is the pre-v2 shape — that state was deleted; errors now land in `FINALIZING` with `termination_reason="error"` (see §0.1).

### 1.4 Reflect: a retry-loop INSIDE the turn

When reflect returns `retry`, `_run_agent_retry()` (`manager.py:1834`) re-enters `SCOUTING → PROCESSING → IDLE` **within the same user turn**, re-feeding `reflect_lessons` into the scout (`manager.py:452-454`). Bounded by `reflect_max_retries` (or `reflect_max_retries_worker` — tighter — for workers: `manager.py:265-269`). Eval retries have their own independent budget (`settings.eval_max_retries`) with the same mechanic. So a single user turn can cycle `IDLE → SCOUTING → PROCESSING → IDLE` up to `1 + reflect_retries + eval_retries` times before settling.

### 1.5 Reflect verdicts and how they manifest

Reflect emits `{verdict, reasoning, failure_cause, confidence, strategy}` (`core/reflect.py:78-101`). Consequences:

- `pass` → loop breaks, turn settles; worker auto-stamp header becomes `# AUTO-STAMPED (reflect=pass ...)` (`manager.py:409-410`)
- `retry` → `reflect_retry_requested=True`, retry loop re-runs scout+agent with `reflect_lessons` prepended
- `escalate` → no retry; worker auto-stamp becomes `# ESCALATED (reflect verdict: escalate)` (`manager.py:397-403`), signalling parent not to trust the output
- retries exhausted with verdict still `retry` → `# UNVERIFIED (reflect verdict: retry, retries exhausted)` (`manager.py:404-408`)
- reflect never ran (disabled or crashed) → `# UNVERIFIED (no reflect verdict recorded — quality not gated)` (`manager.py:411-413`)

Non-reflect terminal conditions also get distinct headers:
- `round_ceiling` / `compaction_failed` → `# INCOMPLETE` (`manager.py:390-391`)
- `cancelled` → `# CANCELLED` (`manager.py:392-393`)
- `error` → `# ERROR (worker exited with error: …)` (`manager.py:394-396`)

### 1.6 `ask_user` is a turn terminator, not a pause

When the agent calls `ask_user`:

1. `dialog_tools.py` records the question and transitions the session to `AWAITING_USER` (`ask-user`)
2. Agent loop notices **after the round completes**: writes a placeholder assistant message, emits `stream.done`, sets `termination_reason="complete"`, returns
3. Post-hooks run normally (reflect treats it as a complete turn)
4. The session parks in `AWAITING_USER` — a real state, not a flag on an idle session
5. When the user answers: `POST /api/questions/{id}/answer` calls `manager.prompt()` with the answer (`api/routers/questions.py:52`) — **a brand-new turn**, entered via `answer-received` and chained to the previous one by `parent_turn_id`

The turn genuinely terminates; nothing is blocked waiting. `waiting_for_input` survives only as a read-only property meaning `state == AWAITING_USER`, so consumers that need to tell "done" from "parked on a question" have an honest signal.

### 1.7 Snooze is process-global, not session-local

Snooze (`core/snooze.py`) is a singleton background runner doing memory consolidation, user-insight extraction, dedup, and FTS5 reconciliation. It is NOT a session state. Interaction points:

- Any user message arrival cancels snooze (`manager.py:118-121`: `get_snooze().request_cancel()`)
- Snooze skips cycles when any session is non-idle (`core/snooze.py:165-179` checks every session against the v2 enum — `IDLE_READY`, `AWAITING_USER` and `AWAITING_WORKERS` count as idle; `manager.py:2435` provides `has_active_work()` which also trips on `has_background_tasks`)
- When snooze is distilling a specific session it acquires `session.add_background_ref()` (`manager.py:527`) so the reaper won't sweep the session out from under it
- Uses `settings.background_model` exclusively — no primary-model cost

### 1.8 Cooperative cancel

`session.cancel_requested` is checked at several points inside the agent loop (`agent.py:808`, and at round boundaries). On trip:

- agent returns with `termination_reason="cancelled"` (`agent.py:810`)
- `finally` block **skips post-hooks entirely** (`manager.py:263`: `if not _was_cancelled and not session.cancel_requested`)
- skips `_process_pending()` too (`manager.py:330`) — queued messages don't auto-run after a cancel

### 1.9 Reaper (background correction, `reap_idle_sessions` at `manager.py:2475`)

Not part of a normal turn, but part of the lifecycle:

- **Stuck PROCESSING** (>5 min idle, no background tasks) → force-reset to IDLE with a visible `system` event ("Session was stuck in processing and has been reset.") — handles the case where the agent task died without reaching `finally`
- **Stuck SCOUTING / ERROR** (≥ `2×max_idle` idle, no subscribers) → session reaped from memory (not deleted from DB)
- **Truly IDLE** (no subscribers, no background tasks, idle ≥ `max_idle`) → reaped from memory
- Protected IDs (`protected_ids` set) are never reaped

### 1.10 Visual: what a session looks like

```
                 ┌────────────────────────────────────────────────────┐
                 │                   state == IDLE                    │
                 │  ┌────────────────────────────────────────────┐    │
                 │  │ post_hooks_complete=True                   │    │
                 │  │ waiting_for_input=False                    │    │
                 │  │ has_background_tasks=False                 │    │
                 │  │   → truly done, eligible for reap          │    │
                 │  │   → snooze may run (global)                │    │
                 │  └────────────────────────────────────────────┘    │
                 │  ┌────────────────────────────────────────────┐    │
                 │  │ post_hooks_complete=False                  │    │
                 │  │   → reflect/title/distill in flight        │    │
                 │  │   → check_workers reports "unknown"        │    │
                 │  │   → may loop back into SCOUTING on retry   │    │
                 │  └────────────────────────────────────────────┘    │
                 │  ┌────────────────────────────────────────────┐    │
                 │  │ waiting_for_input=True                     │    │
                 │  │   → turn terminated by ask_user            │    │
                 │  │   → next POST to /answer starts new prompt │    │
                 │  │     which clears the flag                  │    │
                 │  └────────────────────────────────────────────┘    │
                 └────────────────────────────────────────────────────┘
                                 │  ▲                           ▲
                    new prompt   │  │ finally:                  │ /answer POST
                    or retry     │  │ force IDLE                │ triggers new prompt
                                 ▼  │                           │
                            SCOUTING ├─► PROCESSING ────────────┤
                                │   │          │
                                └──► ERROR ◄───┘
                                      │
                                      └── finally force → IDLE
                                          (ERROR accepts new prompts)
```

> **Archival note.** The `ERROR` state in this diagram is pre-v2 — it was deleted; errors are now `FINALIZING` with `termination_reason="error"` (see §0.1).

---

## 2. Agent Turn Loop (tool calls within a session)

### 2.1 The loop (`core/agent.py:256-514`)

Iterates while `tool_round < settings.max_tool_rounds` (default 50); when the cap is hit on a healthy turn (tools were called, no error, not stuck), `round_cap_auto_continue` (default 1) grants one fresh round budget before `round_ceiling`:

1. **Pre-round gates** (`agent.py:333-341`) — check `session.cancel_requested`, await `session.pause_event` (for worker pause/resume)
2. **Compile context** (`agent.py:347-355`) — `compile_context()` in `core/context/compiler.py` returns compacted messages + active tool schemas + scout-report section + resource header (rounds left, token budget). Compaction runs as a view transform; stored messages are never mutated.
3. **LLM stream** (`agent.py:405-500`) — `client.chat_stream()` emits `stream.token` events, merges partial tool-call deltas by id, handles `FailoverError` (context overflow triggers compaction-retry, rate-limit falls back to Ollama)
4. **Response check** (`agent.py:502-514`)
   - No tool calls → save assistant message, `termination_reason="complete"`, exit
   - Tool calls → proceed to validate + execute
5. **Dedup + stuck detection** (`agent.py:113-405`; dedup in `_ToolCallGate`, `agent.py:523`) — hash-dedup exact repeats; semantic dedup for expensive tools in `_SEMANTIC_DEDUP_TOOLS = {"call_model"}` (same model + same images = near-duplicate regardless of prompt wording — `agent.py:441`). `StuckDetector` evaluates **10 signals** per round:

   | # | Signal | Weight | Detection |
   | - | ------ | ------ | --------- |
   | 1 | `content_repeat` | 0.5 | Exact assistant content seen in last 5 rounds |
   | 2 | `tool_cycle` | 0.3 | Same sorted set of (name,args-hash) seen in last 10 rounds |
   | 3 | `error_loop` | 0.4 | Same (name, args-hash) that previously failed |
   | 4 | `noop_loop` | 0.2 | No tools + content matches meta-commentary heuristic (`_is_meta_commentary`, `agent.py:244-249`) |
   | 5 | `hallucinated_tool` | 0.3 | Tool name not in registry |
   | 6 | `failure_drift` | 0.3 | Unrelated tool calls for ≥3 rounds after an unresolved failure |
   | 7 | `file_edit_loop` | 0.4 | Same (tool, file_path) failed ≥3 times with different args (bypasses Signal 3) |

   If `score > 0.3` with `repeat_count ≥ 3` (`agent.py:527-553`): prefers the **ask-user help path** when `ask_user` is available, otherwise breaks with `termination_reason="round_ceiling"`.
6. **Execute tools** via executor (safety gate below), append results, check cancel, check `waiting_for_input`, loop back

### 2.2 Scout agent (`core/scout/runner.py`)

- **Model** — the Background role, `settings.background_model` (fast, cheap; empty ⇒ `llm_model`), **fresh context** (no main convo history — session brief only). When scout exhausts its retries it makes one last attempt on the Backup role, `settings.fallback_model` (`runner.py:1265-1290`), before falling through to a deterministic stub report
- **Tools** — read-only discovery, 8 tools: `search_memory`, `search_sessions`, `search_post_mortems`, `search_adaptive`, `search_skills`, `search_tools`, `read_skill_instructions`, `submit_report` (`runner.py:270-395`)
- **Budget** — 6 rounds max (`SCOUT_MAX_ROUNDS`, `runner.py:957`); must call `submit_report` by round 5 (`runner.py:150`)
- **Output** — `ScoutReport` with `recommended_tools`, `recommended_skills` (0-3), `approach_guidance`, `deliverables_plan` (used by Reflect), optional `recommended_model`. SOUL.md/RULES.md/SESSIONS.md are NOT part of the report: the context compiler injects those files whole into the fixed prefix of every system prompt (`_build_agent_directives_block`) — scout reads them to shape its plan but never retypes them
- **Caching** — report cached on `session.last_scout_report`; retries (reflect/eval) re-run scout with `reflect_lessons` prepended

### 2.3 Tool routing (`core/tools/registry.py`, `core/extensions/`)

- 36 **builtin tools** always registered (`file_read`, `file_write`, `bash`, `ask_user`, etc.); `call_model` is a model_mgmt extension, and `spawn_worker` / `get_worker_result` are orchestration extensions
- **Extensions** conditionally registered by feature flag: browser, vcs, orchestration, planning, skillmaker, toolmaker
- Agent sees only the schema slice for `active_tools` — scout-picked plus a monotonically-growing allowlist (`agent.py:349`)
- `discover_tools()` during the loop can expand `active_tools` mid-turn (`agent.py:802-803`)

### 2.4 Safety gate (`core/tools/executor.py:302-320`)

Each `ToolDef` carries `safety_level ∈ {safe, caution, dangerous}`. Dangerous tools require `ask_user` confirmation **unless** `auto_approve_dangerous=true`. **Workers hit the exact same check — no privilege escalation.** Workers also have an additional gate: tools listing `"worker"` in `denied_session_types` (e.g. `spawn_worker` itself) are blocked for worker sessions (`executor.py:262-300`).

### 2.5 Reflect + post-hooks (`core/reflect.py`, `sessions/hooks.py`)

After the agent loop exits, `_run_post_hooks()` (`manager.py:519-537`) runs — gated on `state == IDLE` and queue empty:

1. **Auto-title** first exchange (`hooks.py:94-150`)
2. **Distillation** via snooze
3. **Reflect** (`hooks.py:51-52`) — judges turn against `deliverables_plan`. Output verdict `∈ {pass, retry, escalate}`
   - `retry` → sets `session.reflect_retry_requested`, caller runs another agent loop (capped by `reflect_max_retries`, or `reflect_max_retries_worker` for workers)
   - `escalate` → sets termination reason, worker gets escalation header stamped
   - `pass` → done
4. **Evaluation** (optional QA) (`hooks.py:55-56`)
5. **Worker auto-stamp** (`_finalize_worker`, `manager.py:339-432`) — if worker didn't write `.worker_{id[:12]}_summary.md`, generate one; header encodes reflect verdict as described in §1.5
6. **Restore model override** if `switch_model` was called mid-turn (`manager.py:300-308`) — done AFTER all retries so retries run on the switched model
7. Set `session.post_hooks_complete=True`, emit `turn.complete`, then `_process_pending()`

### 2.6 Compaction (nod)

Compaction is the context-budget management layer. Full details live in `core/context/compaction.py` and `core/context/compiler.py`; the essentials relevant to the turn loop:

- **Three triggers** all surface through the agent loop:
  - **Proactive** (`agent.py:377-379`) — utilization exceeds `settings.compaction_threshold` (default 0.75 of history budget); emits `context.compacting` and compacts in-line
  - **Critical** (`agent.py:359-375`) — utilization exceeds `settings.context_critical_threshold` (default 0.85); last-resort compaction before breaking the turn; one-retry budget per turn
  - **API overflow** (`agent.py:465-471`) — `FailoverError(reason=CONTEXT_OVERFLOW)` returned from an actual LLM call; retries the same `tool_round` after compaction
- **Non-destructive** — stored messages are never mutated. Three-phase view:
  1. `apply_view_pruning()` — stubs oversized tool results **in a new list**. Budget-gated: it engages only when history chars exceed `view_prune_pressure` (0.5) of the char-equivalent budget, keeps the last `view_prune_keep_recent` (30) messages intact, and only stubs results larger than `view_prune_min_chars` (2000). Emits `context.view_pruned {stubbed}`
  2. `exclude_orphans()` (`compaction.py:83-110`) — filters tool messages whose `tool_call_id` has no matching assistant call, view only
  3. `compact_with_llm()` (`compaction.py:117-216`) — **appends** a new `role="compaction"` marker message containing the summary; originals stay in the DB
- **Boundary** (`compaction.py:130-139`) — token count accumulates newest-to-oldest until exceeding `settings.compaction_keep_tokens` (default 51k); everything **before** that boundary is summarized. Recent turns are preserved in full.
- **Metadata column** — `messages.metadata` (v5+), JSON blob `{compacted_up_to, original_count}` written by `db.add_compaction()` (`db/models.py:248-262`). Pre-v5 rows fall back to the `tool_calls` column (`compiler.py:310`).
- **Compilation** — on each turn `compile_context()` reads the most recent compaction marker and assembles the view by **filtering** summarized history in favor of the marker — it does not delete anything (`compiler.py:305-315`).
- **Failure** — if compaction itself errors (e.g. the `background_model` call fails), the agent loop sets `termination_reason="compaction_failed"` and breaks (`agent.py:484`). Downstream hooks classify the turn as INCOMPLETE.

### 2.7 LLM routing (nod)

The LLM layer is abstracted by `ProviderRouter` (`core/llm/router.py`) sitting behind `core/llm/client.py`. Essentials:

- **Providers** — three: `OllamaProvider` (local, always available, `settings.llm_base_url`), `OpenRouterProvider` (remote, opt-in, requires `OPENROUTER_API_KEY`) and `OpenAIProvider` (native OpenAI-compatible, `settings.openai_base_url`, key via `OPENAI_API_KEY`). Held in the name-keyed `self._providers` map (`router.py:108-113`); selection is model-driven via `router.registry.resolve_provider(model)` (`router.py:149-152`), which returns `"ollama" | "openrouter" | "openai"`. `get_provider()` falls back to Ollama when the resolved remote provider is unavailable (`router.py:124-131`).
- **Per-provider semaphores** (`router.py:94-106`, `self._semaphores` at `:114-118`) — independent `SessionAwareLLMScheduler` instances, each constructed with `session_timeout = settings.llm_session_timeout` (default 1800s; `0` means unlimited):
  - Ollama capacity = `settings.llm_max_concurrent` (default 1)
  - OpenRouter capacity = `settings.openrouter_max_concurrent` (default 4)
  - OpenAI capacity = `settings.openai_max_concurrent` (default 4)
  - The `semaphore_stats` property (`router.py:137-147`) sums `available`/`waiting`/`capacity` across all three and nests per-provider stats under the provider name; this is what `spawn_worker`'s saturation check consults
- **Role slots** — three chat-model roles since the 2026-08 consolidation (`config.py:61-75`). `scout_model`, `reflect_model`, `critical_model`, `rlm_root_model` and `rlm_sub_model` no longer exist:

  | Setting | Role | Consumers |
  |---|---|---|
  | `llm_model` | **Primary** (required) | agent turns; every quality-critical call — compaction summaries, reflect verdicts, eval; RLM root |
  | `background_model` | **Background** (empty ⇒ Primary) | scout, auto-title, distill/ingest, snooze activities, dream, telos, RLM sub-calls |
  | `fallback_model` | **Backup** (empty ⇒ no backup) | used whenever a Primary *or* Background call fails: stream failover, provider failover, scout's last resort, one-shot retry |

  `embedding_model` is not a chat role — it names a local Ollama embedding model and setting it is what switches memory search from lexical to hybrid.
- **Per-session override** — `session.model_override` is read at turn start; registry resolves bare names to provider-qualified IDs. Enables workers (or `switch_model`) to swap models without touching global config. Paired with `context_budget_override` so concurrent sessions on different-sized models don't clobber each other. Per-request overrides (`switch_model`, `spawn_worker(model=)`, worker specs) are the task-scoped axis and are orthogonal to the three role slots.
- **Typed failover** — `FailoverReason` enum (`core/llm/errors.py:8-18`): `RATE_LIMIT`, `OVERLOADED`, `TIMEOUT`, `CONTEXT_OVERFLOW`, `AUTH`, `MODEL_NOT_FOUND`, `FORMAT_ERROR`, `UNKNOWN`. Classification via `classify_http_error()` (`errors.py:43-64`): 429/402 → `RATE_LIMIT`, 502/503 → `OVERLOADED`, 408 → `TIMEOUT`, 400 + context-keyword → `CONTEXT_OVERFLOW`.
- **Behavior by reason**:
  - `RATE_LIMIT` / `OVERLOADED` on a remote provider → automatic fallback to Ollama running `settings.fallback_model`, via `_fallback_chat()` / `_fallback_stream()` (`router.py:253-305`). Eligibility is `_fallback_eligible()` (`router.py:120-122`): any provider that is not already Ollama. Fallback **sanitizes messages** first: strips vision blocks, converts tool→user messages (`router.py:25-81`) since Ollama may not support them
  - `CONTEXT_OVERFLOW` → no fallback; surfaces to agent loop which runs compaction-retry (see §2.6)
  - `TIMEOUT` / `AUTH` / `MODEL_NOT_FOUND` / `FORMAT_ERROR` / `UNKNOWN` → no fallback; surfaces as hard error → `termination_reason="error"`
- **Same-provider Backup failover** — above the router, the streaming agent loop switches to `settings.fallback_model` once its retry budget is spent and emits `stream.fallback` (`agent.py:1005-1022` for the tool loop, `:1772-1790` for the final response). The only requirement is that the backup differs from the model currently in flight — **a different model on the same provider counts**, so an Ollama-primary/Ollama-backup configuration has real failover. The switch re-runs `attach_cache_breakpoints()` for the new model so stale Anthropic cache parts are flattened when the backup is not `anthropic/*`.
- **One-shot Backup retry** — `chat_with_backup()` (`core/llm/client.py:154-169`) wraps every non-streaming call site (compaction, reflect, titles, eval, distill): try `model`, and on any exception retry exactly once on `settings.fallback_model` when it is set and different. Because it goes back through `client.chat()`, the backup is routed by the registry — it can land on a different provider or the same one.

### 2.8 Events / SSE (`core/events.py`, `sessions/state.py:153-173`)

Single SSE stream (`GET /api/sessions/{id}/events`) fed by:

- `session.emit_event()` — per-session
- `JobEventBus.emit()` (`events.py:26-42`) — global

Every event carries `_seq` for gap detection, `timestamp`, `session_id`. Types include:

- `scout.start`, `scout.done`
- `stream.token`, `stream.done`, `stream.error`
- `tool.start`, `tool.call`
- `context.compacting`
- `worker.started`, `worker.done`, `worker.failed`
- `session.queued`, `session.queue_full`
- `turn.complete` — fires AFTER post-hooks, signals `post_hooks_complete=True`
- `dialog.answered` — on user answer to `ask_user` (`questions.py:60-63`)
- `system` — catch-all for reaper notices etc.

---

## 3. Sub-Agent Workers

### 3.1 What a worker is

A **child session** spawned by a parent. Properties:

- Fresh message history (nothing shared with parent convo)
- Its own `session.model_override` (can differ from parent's model)
- Runs concurrently with siblings, bounded by `settings.max_concurrent_workers`
- Results surfaced through a stable API (`get_worker_result`, `get_worker_transcript`)
- **Cannot spawn sub-workers** (flat hierarchy, enforced in executor)

### 3.2 Spawn flow (`core/extensions/orchestration/__init__.py:49`)

1. **LLM-slot saturation check** (`:46-59`) — count parent's active workers vs. `_get_semaphore_stats()["capacity"]`; if already saturated, return a warning string *before* creating a session ("additional workers will queue and likely timeout")
2. **Validate `model`** (if specified) — resolve + check provider routing before creating anything (`:64-81`). A bare name routed to Ollama is rejected if Ollama doesn't have the model (avoids a non-obvious wrong-backend hit)
3. **Acquire `_spawn_lock`** (threading.Lock at `:24`) — **TOCTOU guard**: count active workers under the lock and call `create_session` inside the same critical section so concurrent `spawn_worker` calls cannot both pass the `max_concurrent_workers` check (`:86-100`)
4. **Build worker system prompt** (`:103-145`) — task description, auto-summary filename convention `.worker_{id[:12]}_summary.md`, model announcement if override, attachment hint (see §3.3)
5. **Gate worker before `manager.prompt` runs** (`:152`) — immediately set `worker_session.post_hooks_complete = False`; `manager.prompt` will flip it itself, but without this assignment there is a tiny window where `check_workers` could see the newly-created worker as IDLE+complete
6. **Schedule start** — `asyncio.run_coroutine_threadsafe(manager.prompt(worker_id, task))` and append worker_id to parent's list via `loop.call_soon_threadsafe()` (`:165-173`) so `worker_ids` is only mutated on the event-loop thread
7. **Emit `worker.started`** to parent

### 3.3 Attachment visibility (`orchestration:111-144`)

Workers share the parent's workspace directory, so attachment **bytes** are always reachable via `file_read` / `bash`. But **auto-inlined vision blocks** only happen when the worker's own model is vision-capable. Spawn logic parses the last user message in the parent for `[attached: filename]` markers and injects a hint into the worker's system prompt:

- If the worker has a `model` override → hint says "images inlined if your model supports vision; otherwise use `file_read` or `call_model(image_path=...)`"
- If the worker inherits the default → hint says "default model may not support vision; re-spawn with `model=<vision-capable>` to inline, or use `file_read`/`call_model`"

This is the only mechanism by which the worker becomes aware of attachments — parent's conversation history is not shared.

### 3.4 Full worker-management tool catalog

All orchestration tools are registered with `denied_session_types={"worker"}` (`:1334`) so a worker cannot call any of them — hierarchy stays flat.

| Tool | File | Purpose |
| ---- | ---- | ------- |
| `spawn_worker` | `:49` | Create a new worker session. Returns worker ID. Safety: `caution`. |
| `check_workers` | `:359` | Status of all workers. A worker is "done" only when `state ∈ {idle, unknown} AND post_hooks_complete=True` (`:224`). Annotates "finalizing (reflect/post-hooks)" when `state==idle` but `post_hooks_complete==False` (`:230-231`). Triggers cross-pollination (§3.6) if some workers are done and others still running. |
| `await_workers` | `:657` | Block up to 30 min (hardcoded `max_wait=1800`, `:457`). Polls every 3 s using `asyncio.run_coroutine_threadsafe(asyncio.sleep(3), loop).result(3.5)` — never blocks the event loop (`:500-505`). Snapshots `worker_ids` per iteration (`:463`) to avoid `RuntimeError` if the loop appends while iterating. Returns early if any worker is stalled past `stale_threshold` (default 120 s). |
| `get_worker_result` | `:487` | Final summary with quality-gate header (see §1.5). Lookup order: per-worker summary file → legacy `summary.md` → last assistant message. Max 3000 chars read. If the summary file already starts with `#` (sentinel from `_finalize_worker`), returned as-is — avoids double-stamping. |
| `get_worker_transcript` | `:586` | Full message stream, one line per message: `[role] content`, truncated to `max_chars` (default 30k). Role-aware formatting for `assistant:tool_calls`, `tool`, `reflect` (parses verdict), `scout` (parses approach), `system`, `user`. |
| `message_worker` | `:982` | Fire-and-forget follow-up message into a running worker — internally just `manager.prompt(worker_id, message)` so the worker either picks it up on its current turn (queued) or starts a new turn. Safety: `caution`. |
| `cancel_worker` | `:1034` | Calls `session.task.cancel()` which raises `CancelledError` in the worker's `_run_agent_safe`. Worker's post-hooks are **skipped entirely** (`manager.py:263`) — no reflect, no auto-stamp, `termination_reason="cancelled"`. Safety: `caution`. |
| `pause_worker` | `:1048` | Calls `session.pause_event.clear()`. Agent loop awaits the event at its pre-round gate (`agent.py:333-341`), so the worker pauses **at the next tool-round boundary**, not mid-tool. |
| `resume_worker` | `:1083` | Calls `session.pause_event.set()`. Worker resumes from the awaited gate. |
| `retry_worker` | `:1125` | Cancel the old worker, then `spawn_worker` with a composed task that embeds the old worker's output (truncated to 2000 chars) + optional `reason` + `new_instructions`. Useful for UNVERIFIED/ESCALATED workers. |

### 3.5 Why `post_hooks_complete` matters here

`state == IDLE` alone lies during the post-hooks window (reflect may still be running, may trigger a retry that sends the session back to SCOUTING → PROCESSING → IDLE). If `check_workers()` treated plain IDLE as "done", parents would race against reflect and read half-verified output. The gate makes the API honest: "done" means **every** retry ran and reflect committed a verdict row. Both `check_workers` (`:220-224`) and `await_workers` (`:470-474`) enforce this.

### 3.6 Cross-pollination — sharing findings between live siblings

`cross_pollinate()` (`orchestration:1157`) is a LogAct-inspired supervisor pattern: when one worker finishes, inject its finding into the conversation history of any still-running siblings, so they don't redo the same discovery.

**Triggered naturally** from `check_workers()` (`:252-258`) when there's a mix of done + running workers — **not on a polling timer**.

Mechanics:
- Classifies workers as `completed` (idle + has output) vs `running` (scouting/processing)
- **Quality gate** (`:649-656`): only cross-pollinates from workers whose reflect verdict is `pass`. Escalated / retry / missing-reflect workers are **skipped** — historical bug: a preamble-only worker's output got broadcast and poisoned siblings with empty context
- **Dedup** via `session_messages` table (`:631-640`): `(sender_id, recipient_id, message_type="cross_pollinate")` uniquely identifies a delivery; repeated calls don't re-send
- **Delivery** (`:671-685`): injects a `system` message into the running worker's session via `db.add_message(rwid, "system", ...)`, formatted as `[Sibling worker finding — "{title}"] {summary[:300]} ...`. On the worker's next context compile, the finding appears as part of its conversation.

### 3.7 Worker cancel / pause semantics (important for consumers)

- **Cancel** is terminal and bypasses the quality gate. No reflect runs (`manager.py:263`), no auto-stamp header is written (the auto-stamp *would* write `# CANCELLED` if invoked — but cancel skips the whole post-hooks phase, so the file simply doesn't exist unless the worker wrote one itself). `get_worker_result` falls through to its "no reflect row" branch and stamps `# CANCELLED (worker stopped before reflect ran)` at read time (`orchestration:324-325`).
- **Pause / resume** is non-destructive: worker sits at the pre-round gate awaiting `pause_event`. Any tools already in flight complete; no new round begins. `check_workers` will report the worker as `processing` with growing `idle_seconds` — **pause does not change state**. If left paused past stall thresholds, `await_workers` will flag it as stalled.

### 3.8 Session deletion cascades

`manager.delete_session()` (`manager.py:83-101`) recursively deletes all `worker_ids` before removing itself, cancels their tasks, and deletes their auto-summary files (`manager.py:95-97`). Deletion is not a state: `delete_session` cancels the task, pops the session from the in-memory dict, and removes the DB row. (The pre-v2 enum carried a `DELETED` value that no code path ever set; it was deleted with the rest of that enum.)

### 3.9 Same safety gate, no privilege escalation

`executor.py:302-320` runs for both parent and worker sessions; dangerous tools without `auto_approve_dangerous` are refused regardless of `session_type`. Workers **cannot** bypass the gate by virtue of being "internal." Additionally, workers hit the `denied_session_types` gate (`executor.py:262-300`) which blocks all orchestration tools so the hierarchy stays flat.

---

## 4. Loop Inside a Worker

### 4.1 Same loop as a main session

A worker runs the **identical** pipeline as a user-facing session: `_run_agent_safe()` → scout → agent loop → reflect → post-hooks. It is not a simplified path. Differences:

- Worker reflect uses `reflect_max_retries_worker` (tighter) — `manager.py:265-269`
- Worker's post-hooks include `_finalize_worker` — stamps summary file if worker didn't write its own
- Worker typically has no queued user messages (it gets one prompt and completes)
- Worker's system prompt includes task description + summary-file convention (`orchestration:103-145`)

### 4.2 Sub-worker recursion is blocked (`executor.py:262-300`)

```python
if session_type and session_type in tool.denied_session_types:
    return _refusal(name, f"Error: Tool '{name}' cannot be used in {session_type} sessions.")
```

Tools with `"worker"` in `denied_session_types`: every orchestration tool (`spawn_worker`, `await_workers`, `check_workers`, `get_worker_result`, `get_worker_transcript`, …). This enforces a flat hierarchy — parent → workers, no deeper nesting.

### 4.3 What a worker can use

- All core tools (`file_read`, `file_write`, `bash`, `call_model`, `ask_user`, …) — including `ask_user`: the question is posted to the global question registry and routed to **whichever user is watching the parent's UI**. From the worker's perspective the mechanism is identical to a main session — turn terminates with `waiting_for_input=True`, user's answer arrives as a new `manager.prompt()` into the worker.
- All enabled extension tools (browser, vcs, planning, skillmaker, toolmaker)
- Skills (scout auto-injects top-1)
- Shared workspace (reads/writes visible to parent), but its deliverable is isolated in `.worker_{id[:12]}_summary.md`

### 4.4 Result handoff

Parent reads results via `get_worker_result()` which already encodes the reflect verdict in a header (`# AUTO-STAMPED`, `# UNVERIFIED`, `# ESCALATED`, `# INCOMPLETE`, `# CANCELLED`, `# ERROR`, or no header for a worker-written summary). Parent can branch on trustworthiness — use-as-is / inspect transcript (`get_worker_transcript`) / retry (`retry_worker`) / escalate to user — without re-running its own quality check.

### 4.5 Cross-turn state

Workers are typically single-turn (spawn → complete). But nothing stops a parent from calling `message_worker()` to send a follow-up prompt after a worker finishes — this re-enters `manager.prompt()` on the worker, which kicks off another full `_run_agent_safe` cycle (scout → loop → reflect → post-hooks). Cross-pollination messages are injected the same way, but as `system` role so the worker doesn't treat them as a new user turn.

---

## 5. Summary Table

| Area                   | Primary files                                    | Key mechanism                                                                  |
| ---------------------- | ------------------------------------------------ | ------------------------------------------------------------------------------ |
| Session lifecycle      | `sessions/state_v2.py`, `sessions/manager.py`    | 10-state machine + asyncio.Lock + `pending_messages` deque (see §0)            |
| Session data           | `sessions/state.py`                              | `AgentSession`: event plumbing, cooperative-cancel bool, pause event, retry counters; state itself lives in `_state_v2` |
| Agent loop             | `core/agent.py:256-514`                          | compile_context → stream → dedup/stuck → execute → cancel/ask_user checks      |
| Stuck detection        | `core/agent.py:113-405`                          | Seven weighted signals; score > 0.3 with repeat_count ≥ 3 triggers help/break |
| Compaction             | `core/context/compaction.py`, `core/context/compiler.py` | Non-destructive 3-phase view: prune → orphan-filter → `role="compaction"` summary marker; metadata in `messages.metadata` (v5+) |
| LLM router             | `core/llm/router.py`, `core/llm/client.py`       | Ollama + OpenRouter + OpenAI-compatible; per-provider semaphores; typed `FailoverError`; remote rate-limit → Ollama fallback; CONTEXT_OVERFLOW → compaction-retry |
| Scout                  | `core/scout/runner.py`                           | Fresh-context fast model, 6-round budget, emits `ScoutReport` + `deliverables_plan` |
| Tool registry          | `core/tools/registry.py`, `core/extensions/`     | ~13 core + discoverable extensions; `active_tools` slice per turn              |
| Safety gate            | `core/tools/executor.py:302-320`                 | `safety_level` check; workers not exempt; `denied_session_types` gates flat hierarchy |
| Reflect                | `core/reflect.py`, `sessions/hooks.py`           | verdict ∈ {pass, retry, escalate}; retry bounded by `reflect_max_retries[_worker]` |
| Reflect retry loop     | `sessions/manager.py:272-295`                    | re-enters SCOUTING→PROCESSING within one user turn until verdict passes or cap |
| ask_user               | `core/tools/builtin/dialog_tools.py`, `api/routers/questions.py` | Transitions to `AWAITING_USER`, terminates turn; answer = new prompt           |
| Snooze                 | `core/snooze.py`                                 | Process-global; cancelled by user msg; gates on every session being IDLE       |
| Reaper                 | `sessions/manager.py` `reap_idle_sessions`       | Per-state timeouts (§0.7); unsticks stuck PROCESSING, reaps idle SCOUTING      |
| Workers — spawn        | `orchestration:49`                               | `_spawn_lock` atomic slot; LLM-slot warning; attachment-visibility hint         |
| Workers — lifecycle    | `orchestration:359-1155`                         | `check_workers`, `await_workers`, `get_worker_result`, `get_worker_transcript`, `message_worker`, `cancel_worker`, `pause_worker`, `resume_worker`, `retry_worker` |
| Cross-pollination      | `orchestration:1157`                             | Reflect=pass findings from completed workers injected as `system` msg into running siblings; dedup via `session_messages` table |
| Events                 | `core/events.py`, `sessions/state.py:153-173`    | Single SSE stream with `_seq` gap detection                                    |
| Deletion               | `sessions/manager.py:83-101`                     | `delete_session` cancels task, cascades workers, removes summary files         |

---

## Appendix: Reading this doc against the code

Every claim above cites the file and line range it was drawn from. When these drift out of date, the reliable sources of truth are, in order:

1. `sessions/state_v2.py` — the state enum, the transition graph, and the one mutator
2. `sessions/state.py` — the `AgentSession` fields and the lock/event plumbing
3. `sessions/manager.py` — the turn driver, including the retry loop and post-hooks gating
4. `core/agent.py` — the tool-round loop, cancel/ask_user/pause interactions
5. `core/extensions/orchestration/__init__.py` — worker spawn, check, await, get_worker_result

If you find a mismatch between this doc and the code, the code wins — please update this doc in the same change.
