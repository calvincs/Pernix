# API Reference

Pernix exposes a REST API for programmatic access. The web UI itself is built entirely on this API, so anything the UI can do, the API can do.

> **Try it interactively first.** Pernix is built on FastAPI, which means a complete, live, click-to-try Swagger UI is auto-generated for every endpoint. Open it while the server is running:
>
> - **Swagger UI:** [http://localhost:8090/docs](http://localhost:8090/docs) — try requests directly in the browser
> - **ReDoc:** [http://localhost:8090/redoc](http://localhost:8090/redoc) — a clean, reference-style view of every schema
>
> The reference below documents the same endpoints in narrative form. Most users will find the Swagger UI faster for exploration; this document is the right place for offline reading and stable links.

---

## Base URL

| Mode | Base URL |
|---|---|
| Local (default) | `http://localhost:8090` |
| Network mode | `https://<host>:<port>` |

---

## Authentication

**Local mode:** No authentication required.

**Network mode:** Every request (except health check, static files, and the root page) must include a valid Bearer token.

```bash
# Header (preferred)
curl -H "Authorization: Bearer <your-token>" https://host:8090/api/sessions

# Cookie (set automatically after first login)
curl -b "pernix_auth=<your-token>" https://host:8090/api/sessions

# Query parameter (for QR login links)
curl "https://host:8090/api/sessions?token=<your-token>"
```

Retrieve your token from the Settings UI or from `GET /api/settings/auth-token`. Rotate it with `POST /api/settings/auth-token/regenerate`. In network mode both require a valid Bearer token like every other endpoint — the old localhost-only restriction on them was deliberately removed.

---

## Sessions

Sessions are the primary object in Pernix. Each session has a persistent conversation history, its own state machine, and can queue multiple messages.

### Create a Session
```
POST /api/sessions
```
```json
{
  "title": "My session",
  "session_type": "normal"
}
```
Returns the new session object including `session_id`.

### List Sessions
```
GET /api/sessions?limit=50&offset=0
```
Returns a paginated list of sessions, most recent first.

### Get Session (with messages)
```
GET /api/sessions/{session_id}
```
Returns the full session object including all messages. Pass `?limit=N` to get only the newest N plus `total_messages` / `has_more`.

### Get Session Status
```
GET /api/sessions/{session_id}/status
```
Returns lightweight status: `state` (the 10-value enum), `compat_status` (legacy 3-value), model override (null unless one was set), current turn ID, retry index, termination reason. Useful for polling.

### Get State Log
```
GET /api/sessions/{session_id}/state-log
```
Returns the append-only state machine transition history for the session. Every state change is recorded with a timestamp, the triggering event, and optional metadata. Accepts `since_id`, `before_id`, `limit` (1–5000), and `tail`.

### Search Sessions
```
GET /api/sessions/search?q=<query>&limit=20
```
FTS5 full-text search across all sessions' message content.

### Update Session Metadata
```
PATCH /api/sessions/{session_id}
```
```json
{ "title": "New title", "pinned": true, "model_override": "qwen3:32b" }
```
Any subset of the three keys.

### Pause / Resume a Session
```
POST /api/sessions/{session_id}/pause
POST /api/sessions/{session_id}/resume
```
Pausing takes effect at the next round boundary; mid-tool-call work is not interrupted.

### Pending Message Queue
```
GET    /api/sessions/{session_id}/pending                   List queued (not yet processed) messages
DELETE /api/sessions/{session_id}/pending/{message_id}      Remove one queued message
```

### Delete a Session
```
DELETE /api/sessions/{session_id}
```
Deletes the session and cascades to any worker sessions it spawned.

### Purge Old Sessions
```
POST /api/sessions/purge
```
```json
{ "keep_days": 7, "keep_min": 5 }
```
Bulk-deletes sessions whose last activity is older than `keep_days`, always keeping at least `keep_min` of the stale candidates.

---

## Chat

### Send a Message
```
POST /api/chat
```
```json
{
  "session_id": "abc123",
  "message": "Summarize the file at workspace/report.txt",
  "system_prompt": "",
  "idempotency_key": "unique-request-id-optional"
}
```

Returns immediately with a confirmation. The agent processes the request asynchronously — monitor progress via the [SSE event stream](#real-time-events-sse).

If the session is currently processing, the message is queued and you will receive a `session.queued` event.

`idempotency_key` is optional. If you provide one, a duplicate submission with the same key is silently ignored (the original is still processed). The check is global — not per-session — so make keys unique per request, not per session. Useful for retrying a failed HTTP request without double-submitting.

### Inject a Message
```
POST /api/chat/inject
```
```json
{
  "session_id": "abc123",
  "message": "Additional context to inject"
}
```
Injects a user message directly into the running context mid-turn (the role is always `user`). If the session is not mid-turn (processing / awaiting workers / compacting), the message is handled as a normal prompt instead and the response is `{"status": "queued"}`.

### Retry Last Turn
```
POST /api/retry/{session_id}
```
Deletes the last partial or unsatisfactory response and re-runs the agent from the last user message. Useful when a response was cut off or unsatisfactory.

### Cancel Current Turn
```
POST /api/sessions/{session_id}/cancel
```
Cancels the currently running agent turn. Clears the pending message queue and cascades cancellation to any worker sessions.

### Clear Messages
```
POST /api/sessions/{session_id}/clear
```
Erases all messages in the session while preserving the session's metadata (title, settings overrides). Useful for starting fresh in an existing session.

### Manually Compact Context
```
POST /api/compact/{session_id}
```
Triggers context compaction immediately (rather than waiting for `compaction_threshold` to be reached). The session must be `idle_ready` — a busy session returns `409`.

### Get Partial Response
```
GET /api/partial/{session_id}
```
Returns `{has_partial, message_id, content_preview}` — a short preview of the last persisted partial response. It is not a live token stream; use SSE for that.

### Get Token Usage
```
GET /api/usage/{session_id}
```
Returns cumulative token usage statistics for the session (input/output tokens, costs if available).

---

## Real-Time Events (SSE)

The preferred way to monitor an ongoing agent turn is via Server-Sent Events:

```
GET /api/sessions/{session_id}/events
```

This is a persistent, long-lived HTTP connection. The server pushes events as they occur.

**Reconnection:** Include `Last-Event-ID: <last_seq>` on reconnect (or a `?last_event_id=` query parameter) to receive any events you missed. The client should reconcile by checking for gaps in `seq` (a monotonically increasing sequence number on every event).

### Connecting (JavaScript example)
```javascript
const evtSource = new EventSource(`/api/sessions/${sessionId}/events`);
// Every event is sent with a named `event:` line, so `onmessage` never fires.
// Register a listener per event type you care about:
for (const type of ["stream.token", "tool.call", "session.state_changed", "turn.complete"]) {
  evtSource.addEventListener(type, (e) => console.log(type, JSON.parse(e.data)));
}
```

### Event Catalog

Every event includes `seq` (sequence number), `session_id`, and `timestamp`. The catalog below covers the primary events; the complete list the UI consumes — including `session.title`, `stream.retry`, `stream.length_continuation`, `stream.budget_exhausted`, `message.injected`, `model.override`, `partial.saved`, `reflect.deferred`, `browse.start`/`browse.done`, `job.started`/`job.completed`/`job.error`, and the `snooze.*` family — is enumerated in `static/js/sse.js` (`EVENT_TYPES`).

#### Session Lifecycle
| Event | Description |
|---|---|
| `session.state_changed` | State machine transition. Fields: `from_state`, `to_state`, `trigger`. |
| `session.queued` | A message was queued because the session is busy. Fields: `queue_depth`. |
| `session.queue_full` | Message rejected — queue is at `max_pending_messages`. |
| `session.message_combined` | A rapid follow-up message was merged into the running turn's DB row (rapid-fire combining within a 3-second window). Fields: `message_id`. |
| `session.message_combine_skipped` | The merge above was refused — the turn had already answered — so a new turn was queued instead. Fields: `message_id`, `reason`. |
| `session.prompt_rejected` | Message rejected (e.g., session is cancelling). Fields: `reason`. |

#### Agent Turn
| Event | Description |
|---|---|
| `scout.start` | Scout (planning) phase began. |
| `scout.done` | Scout completed. Fields include `approach` (the plan summary), `tools`, `skills`, `memory`, `model`, `from_cache`, `latency_ms`, `scout_model` (the model scout actually ran on). |
| `stream.token` | One streamed text token from the agent. Fields: `content`. |
| `stream.done` | The model finished generating for this round. Fields: `finish_reason`. |
| `stream.fallback` | Retries were exhausted and the turn switched to the Backup model. Fields: `model`. |
| `turn.complete` | The full agent turn is finished (fires after post-hooks). |
| `stream.error` | An error occurred during generation. Fields: `error`. |

#### Tool Calls
| Event | Description |
|---|---|
| `tool.start` | A tool invocation is starting. Fields: `name`, `arguments` (summarized). |
| `tool.call` | A tool returned a result. Fields: `name`, `arguments`, `result`, `full_result`, `truncated`, `was_error`, `latency_ms`. |

#### User Interaction
| Event | Description |
|---|---|
| `dialog.question` | The agent is pausing to ask the user a question. Fields: `question_id`, `question`, `context`, `urgency`, `session_title`. Answer via `POST /api/questions/{question_id}/answer`. |
| `dialog.notification` | Broadcast to every connected browser, not just those viewing the session. Also carries goal budget-limited alerts. Fields: `notification_id`, `title`, `body`, `urgency`, `source_session_id`. |
| `dialog.answered` / `dialog.dismissed` | The question was resolved. |

#### Context Management
| Event | Description |
|---|---|
| `context.compacting` | Compaction is starting. |
| `context.compacted` | The conversation context was compacted. Fields: `summarized_messages`, `summary_tokens`. |
| `context.view_pruned` | Budget-gated view pruning stubbed oversized tool results out of the compiled view for this turn. Stored messages are untouched. Fields: `stubbed` (count). |
| `context.reset` | The conversation context was cleared. |

#### Reflect, Eval, and Gates
| Event | Description |
|---|---|
| `reflect.start` / `reflect.done` | The post-turn quality gate ran. The verdict rides on `reflect.done`. |
| `reflect.skipped` | The gate was skipped (disabled, or the turn was shorter than `reflect_min_messages`). |
| `reflect.retry` | A retry attempt is starting. Fields: `attempt`, `max`, `reasoning`. |
| `reflect.exhausted` | `reflect_max_retries` was reached. Fields: `reasoning`. |
| `reflect.escalate` | Reflect cannot fix this automatically. Fields: `missing`, `reasoning`. |
| `reflect.budget_exhausted` | Reflect was skipped for lack of LLM time headroom. Fields: `remaining_s`, `needed_s`. |
| `reflect.circuit_breaker` | A further retry was refused because the last two attempts failed identically. Fields: `attempts`, `signature`, `reasoning`. |
| `eval.start` / `eval.pass` / `eval.done` / `eval.retry` / `eval.exhausted` | The optional autonomous evaluation pass. |
| `gates.done` | Deterministic goal gates finished. Fields: `attempt`, `total`, `failed`, `names_failed`. |

#### Goals
| Event | Description |
|---|---|
| `goal.continuation` | An auto-continuation turn was enqueued for the active goal. Fields: `goal_id`, `ordinal`, `budget`. |
| `goal.budget_exceeded` | The in-turn budget checkpoint tripped mid-round; the turn ends with `termination_reason="budget_exhausted"`. Fields: `reason`. |

Continuation-budget *exhaustion* has no dedicated event — the goal moves to `budget_limited` and a high-urgency `dialog.notification` is broadcast.

#### Workers
| Event | Description |
|---|---|
| `worker.started` | A worker sub-agent was created and prompted. Fields: `worker_id`, `title`, `model`. |
| `worker.done` | A worker finished (any outcome — cancellation arrives as `termination_reason: "cancelled"`). Fields: `worker_id`, `termination_reason`, `error`. |
| `worker.failed` | A worker failed to start (spawn-time error; it never ran). Fields: `worker_id`, `error`. |

#### RLM runs
Emitted on the launching session's stream while `rlm_process` works — see [internals/rlm.md](internals/rlm.md).

| Event | Description |
|---|---|
| `rlm.started` | A run began. Fields: `run_id`, `ui_session_id`, `task_preview`, `source`, `root_model`, `sub_model`, `max_iterations`, `max_subcalls`, `timeout_seconds`. |
| `rlm.activity` | One trace event completed. Fields: `run_id`, `ui_session_id`, `kind` (root/cell/subcall/notice/synthesis), `iteration`, `detail`, `iterations`, `subcalls`. |
| `rlm.heartbeat` | ~10s liveness pulse during long steps. Fields: `run_id`, `ui_session_id`, `iterations`, `subcalls`, `in_flight`, `quiet_seconds`, `elapsed`. |
| `rlm.done` | The run ended. Fields: `run_id`, `ui_session_id`, `status`, `iterations`, `subcalls`, `duration`, `partial`, `error`. |

---

## Memory

Pernix maintains a persistent memory store — a collection of markdown files indexed for full-text search. The agent reads from and writes to this store automatically.

### List Memory Files
```
GET /api/memory/files
```
Returns a list of all memory files with entry counts and sizes.

### Read a Memory File
```
GET /api/memory/files/{filename}
```
Returns `{"name", "content"}` as JSON.

### Search Memory
```
GET /api/memory/search?q=your+query&limit=5&after=<epoch>
```
Full-text search across all memory entries — BM25, or hybrid BM25 + vector when `embedding_model` is set. `limit` defaults to 5; `after` filters to entries newer than the given epoch. Returns entries with relevance scores.

### Memory Maintenance
```
POST /api/memory/maintenance
```
Runs a health check on the memory index (no request body) and auto-fixes it — if the FTS5 index is out of sync with the markdown files, it is reindexed. Useful if search results seem stale or wrong.

---

## Workspace

The workspace is a sandboxed directory (`data/workspace/`) that the agent can read from and write to. You can also manage workspace files directly via the API or UI.

### List or Search Files
```
GET /api/workspace?path=subdir         # List a directory
GET /api/workspace?q=pattern           # Search filenames
```

### Read a File
```
GET /workspace/{path}
```
Serves the file with auto-detected content type. Works for HTML, images, JSON, text, etc. Useful for opening agent-generated HTML files in the browser.

### Write a File
```
PUT /workspace/{path}
Content-Type: application/json

{ "content": "<file contents>" }
```
The body is JSON with a `content` field (a raw text body is a 422). Creates parent directories automatically.

### Delete a File
```
DELETE /workspace/{path}
```

### Upload a File
```
POST /api/upload
Content-Type: multipart/form-data

file=@localfile.pdf
```
Max 250MB. Filenames are sanitized. Blocked extensions: `.exe`, `.sh`, `.php`, `.bat`, `.dll`, `.msi`, `.scr`, `.cmd`, `.com`. Returns the saved path.

### List Data Files
```
GET /api/datafiles
```
Returns a directory listing of `data/` excluding the SQLite database files and secret-bearing config (`settings.json`, `.env`, credential files). Useful for inspecting the agent's persistent state.

---

## Workers

Workers are sub-agents spawned from a parent session. Pause/resume operations apply at the next round boundary (mid-tool-call work is not interrupted).

### List Workers
```
GET /api/sessions/{session_id}/workers
```
Returns the session's worker children with their state and models.

### Pause a Worker
```
POST /api/sessions/{session_id}/workers/{worker_id}/pause
```

### Resume a Worker
```
POST /api/sessions/{session_id}/workers/{worker_id}/resume
```

---

## Context

### Inspect Compiled Context
```
GET /api/context/{session_id}
```
Returns the compiled message list that would be sent to the LLM for the next turn — useful for debugging compaction and pruning.

### Inspect Full Payload
```
GET /api/context/{session_id}/payload
```
Returns the complete request payload (messages + tool definitions + system prompt) the agent would send.

---

## Models

### List Available Models
```
GET /api/models
```
Returns all models known to Pernix — both locally available Ollama models and configured OpenRouter models. Includes metadata: context length, vision support, provider.

### List Ollama Models Only
```
GET /api/models/ollama
```

### Validate a Model Name
```
GET /api/models/validate?model=<name>
```
Verifies that a model is reachable and returns its capabilities.

### Switch Active Model
```
POST /api/models/switch
```
```json
{ "session_id": "abc123", "model": "anthropic/claude-sonnet-4.6" }
```
Sets a per-session model override (does not change global settings).

---

## Health & Settings

### Basic Health Check
```
GET /api/health
```
Returns `{"status": "healthy", ...}` with the release version, active session count, and current primary model.

### Detailed Diagnostics *(localhost-only)*
```
GET /api/health/detailed
```
Returns full diagnostics: provider health and latency, database status, tool registry, maintenance state, snooze state.

### Get Settings
```
GET /api/settings
```
Returns all current settings. API keys are redacted (shown as `_set: true/false`). Sensitive paths are omitted.

### Update Settings
```
POST /api/settings
Content-Type: application/json

{ "llm_model": "qwen3:32b", "max_tool_rounds": 15 }
```
Updates one or more settings. Partial updates are supported — only the provided keys are changed.

### Set API Keys
```
POST /api/settings/apikey
Content-Type: application/json

{ "key": "OPENROUTER_API_KEY", "value": "sk-or-v1-..." }
```
Persists an API key to the `.env` file. Set `value` to an empty string to clear a key.

### Auth Token
```
GET  /api/settings/auth-token              View the current Bearer token
POST /api/settings/auth-token/regenerate   Rotate the token
```

### Access QR Code *(network mode)*
```
GET /api/settings/access-qr
```
Returns a QR code image encoding `https://<LAN-IP>:<port>/#token=<token>`. Scan with a phone to log in. The token is in the URL fragment, which browsers do not transmit, so it never appears in a server access log.

### Environment Variables
```
GET /api/env-vars
```
Returns the names of detected environment variables (values redacted) — useful for verifying API keys are loaded correctly.

### Tool Approvals
```
GET    /api/settings/tool-approvals     List remembered dangerous-tool approvals
DELETE /api/settings/tool-approvals     Clear them
```

### Graceful Restart *(localhost-only)*
```
POST /api/admin/restart
```
Triggers a graceful server restart (uses `os.execv` to replace the process in-place). Useful after changing settings that require a restart.

### Trigger a Snooze Cycle *(localhost-only)*
```
POST /api/admin/snooze-cycle
```
Runs one Snooze maintenance cycle on demand, skipping the cadence and cooldown checks (active sessions still refuse it). Returns the cycle outcome plus post-cycle stats; if the idle gate blocked the run, an `idle_blockers` diagnostic explains why. Useful for debugging memory maintenance and Dream without waiting for idle time.

---

## Skills

### List Skills
```
GET /api/skills
```
Returns metadata for all installed skills (name, description, tags, version, enabled state).

### Get a Skill
```
GET /api/skills/{name}
```
Returns the full SKILL.md content.

### Skill Improvement Proposals

Written by reflect and refine when a skill visibly under-performs; a human
reviews each one before it touches a `SKILL.md`. Nothing is applied
automatically.

```
GET    /api/skills/proposals                 List proposals (default status=pending)
POST   /api/skills/proposals/{id}/approve    Mark approved (you edit the skill yourself)
POST   /api/skills/proposals/{id}/reject     Dismiss
POST   /api/skills/proposals/{id}/apply      Write the change into the target SKILL.md
```

Filter with `?skill_name=`, `?status=`, `?source_origin=` (`session` for
post-turn reflect, `refine` for the authoring pass).

> These lived under `/api/workflows/proposals` before the workflow engine was
> removed in 2026-08. Update any saved calls.

### Update a Skill
```
PUT /api/skills/{name}
Content-Type: text/plain

<full SKILL.md content>
```

### Enable / Disable a Skill
```
PATCH /api/skills/{name}
```
```json
{ "enabled": false }
```

### Delete a Skill
```
DELETE /api/skills/{name}
```

---

## Tools

### List Tools
```
GET /api/tools
```
Returns the tool registry — every tool the agent can call, with name, description, parameters, and safety level.

### Tool Health
```
GET /api/tools/health
```
Returns health status for tools that depend on external services (browser, web search providers).

### Toggle a Tool
```
POST /api/tools/toggle
```
```json
{ "name": "browse_web", "enabled": true }
```

### Set Tool Safety Level
```
POST /api/tools/set-safety
```
```json
{ "name": "bash", "safety_level": "dangerous" }
```

---

## RLM Runs

Read-only history and live inspection of RLM (recursive processing) runs — see [internals/rlm.md](internals/rlm.md). Listing works even when `rlm_enabled` is off, so past runs stay inspectable.

```
GET /api/rlm/runs?session_id=&limit=20        List runs, newest first (limit clamped to 100)
GET /api/rlm/runs/by-session/{ui_session_id}  Resolve a sidebar RLM view session to its run detail
GET /api/rlm/runs/{run_id}                    One run: DB row + manifest + nested children + answer (when finished)
GET /api/rlm/runs/{run_id}/trace?after=0      Parsed trace.jsonl events from byte offset `after`; returns
                                              events, next_offset, and live status/counters — poll with
                                              next_offset to tail a running trace (complete lines only)
```

---

## Jobs & Scheduling

Cron-style scheduled agent runs.

```
GET    /api/jobs              List all scheduled jobs
POST   /api/jobs              Create a job
DELETE /api/jobs/{name}       Delete a job
PUT    /api/jobs/{name}       Update a job
POST   /api/jobs/{name}/pause Pause a job
POST   /api/jobs/{name}/resume Resume a paused job
GET    /api/jobs/runs?limit=&offset=&job_name=   Paginated run history ({items, total, limit, offset})
DELETE /api/jobs/runs         Clear run history
GET    /api/jobs/status       Current scheduler status
GET    /api/jobs/events       SSE stream of job events
```

---

## Goals, Gates & Heartbeats

Read-side surfaces for the [autonomy substrate](internals/autonomy.md). Goals and gates are created by the agent's tools (`goal_create`, `add_gate`, …) when `goals_enabled` / `gates_enabled` are on; these endpoints let clients inspect them.

### Get the Session's Active Goal
```
GET /api/sessions/{session_id}/goal
```
Returns `{"goal": null}` when the session has no active goal, otherwise the goal row (objective, status, budgets, continuation counters) plus live `tokens_used` (worker spend included).

### List the Session's Gates
```
GET /api/sessions/{session_id}/gates
```
Returns the deterministic gates registered on the session: name, command, watch paths, scope, enabled state.

### User Heartbeat
One heartbeat per session, owned by **you** — the agent's `set_heartbeat`/`clear_heartbeat` tools operate on a separate `agent` namespace and can never see or modify this one. Requires `heartbeats_enabled`.

```
GET    /api/sessions/{session_id}/heartbeat    Read the user heartbeat (null when unset)
PUT    /api/sessions/{session_id}/heartbeat    Set/replace it
DELETE /api/sessions/{session_id}/heartbeat    Clear it
```

`PUT` body:
```json
{
  "instruction": "Report progress and stay on the migration task.",
  "every": "5m",
  "delivery": "steer"
}
```
`instruction` is required. `every` accepts durations (`30s`, `5m`, `2h`) or a 5-field cron expression (default `5m`). `delivery` is `steer` (inject into the running turn at the next round boundary — the default) or `follow_up` (queue as a prompt for the next idle moment); a parked session (awaiting workers/user) degrades `steer` to `follow_up`. Returns `{"ok": true, "job_id": ...}` or `{"error": ...}`.

---

## Canary Suite

Golden-task canaries — see [internals/canary-and-adaptive.md](internals/canary-and-adaptive.md). Listing works even when `canary_enabled` is off; triggering a run requires it on.

### List the Suite
```
GET /api/canary
```
Returns `enabled`, the heartbeat `schedule` and `heartbeat_per_night`, and every canary definition (name, tags, `covers`, flaky/`parked` flags, probe fields `max_runs`/`expires`, gate names, timeout, `last_reviewed`) with per-task stats over the retention window (`runs`, `passed`, `last_run` including its `outcome`).

### List Runs
```
GET /api/canary/runs?task=&batch_id=&limit=50
```
Run history, newest first (limit clamped to 500). Filter by task name or by the adaptive `batch_id` a post-batch sweep was tagged with.

### Trigger a Run
```
POST /api/canary/run
```
```json
{ "name": "fix-failing-test" }
```
Queues one canary by name, or a **full sweep** (every canary, parked included) with `"name": "*"`. Returns `{"queued": ...}`; `400` when `canary_enabled` is off, `404` for an unknown name.

### Create a Canary
```
POST /api/canary
```
```json
{ "raw": "---\nname: my-canary\nprompt: ...\ngates: [...]\n---\nnotes" }
```
Raw `CANARY.md` text (or a structured spec: `name`, `prompt`, `gates`, optional `files`/`tags`/`timeout`). Validated by a parse round-trip; gate commands are checked against the auto-admission allowlist proof and the verdicts returned as `warnings` — advisory, never a blocker. `400` on invalid content or a duplicate name.

### Read / Edit / Park / Review / Retire
```
GET    /api/canary/{name}            → full definition + raw_content
PUT    /api/canary/{name}            {"raw": "..."} — replace, validated; the frontmatter name must match
PATCH  /api/canary/{name}            {"parked": true|false}
POST   /api/canary/{name}/reviewed   → bumps last_reviewed to today
DELETE /api/canary/{name}            → moves to .retired/ (purged after canary_purge_after_days — reversible until then)
```

---

## Adaptive Layer

The governed policy store — see [internals/canary-and-adaptive.md](internals/canary-and-adaptive.md). Read endpoints work regardless of `adaptive_enabled`.

```
GET  /api/adaptive/entries?kind=&status=active&limit=200   Entries by kind/status (+ enabled/auto_apply flags).
                                                           Each row carries `usage` — the per-entry
                                                           usefulness counters (uses/successes/failures from
                                                           scout and reflect citations), null when never used
POST /api/adaptive/entries                                 Direct authorship: {kind, title, content, scope?}.
                                                           Immediately active, journaled, deliberately
                                                           unlinted — the human is the authority the content
                                                           lint substitutes for. 400 on validation/cap/dup
DEL  /api/adaptive/entries/{entry_id}                      Release valve: soft-delete one entry as actor
                                                           "human" (status -> deleted, version bumped,
                                                           journaled so it rolls back). 404 if unknown or
                                                           not active. Frees a per-kind cap slot that
                                                           producers can otherwise only ever fill
GET  /api/adaptive/events?batch_id=&entry_id=&limit=100    Append-only event journal (before/after snapshots)
GET  /api/adaptive/batches?status=&limit=100               Apply batches and their tripwire status
GET  /api/adaptive/proposals?status=pending&limit=100      Proposals by status: pending | approved |
                                                           auto_approved | auto_applied (dream memory
                                                           corrections, applied on promotion) | rejected |
                                                           expired | all. An
                                                           unknown status is a 400 that names the enum —
                                                           never a silent []. ?id=N fetches one row whatever
                                                           its status. Every row carries `summary` (producer,
                                                           what it is, target), `auto_approve_exempt`
                                                           (canary proposals wait for a human) and
                                                           `auto_approve_after` (when the veto window closes)
GET  /api/adaptive/proposals/{id}                          One proposal, any status; 404 if unknown
POST /api/adaptive/proposals/{id}/approve                  Apply-on-approve: executes the batch through the
                                                           same apply engine as auto-applies and enqueues a
                                                           batch-tagged canary sweep
POST /api/adaptive/proposals/{id}/reject                   Reject a pending proposal
POST /api/adaptive/rollback                                Roll back — body {"batch_id": ...} or {"event_id": ...};
                                                           walks events in reverse and restores exact snapshots
POST /api/adaptive/batches/{batch_id}/dismiss              Human dismiss of a tripwire flag: suspect → applied
                                                           and cleared_at stamped, which is what makes the
                                                           dismiss durable — the tripwire sweep skips
                                                           cleared batches, so the same evidence can never
                                                           re-flag it. 400 if the batch is not suspect
```

Neither the adaptive layer nor the canary suite emits SSE events. Both are polled through the endpoints above; the tripwire's only push signal is a high-urgency row in the notifications feed.

---

## Telos

Surfaces for the teleological layer — see [internals/telos.md](internals/telos.md). Read endpoints work even when `telos_enabled` is off.

```
GET  /api/telos                          Layer status summary
GET  /api/telos/questions                Open questions
GET  /api/telos/hypotheses               SOUP hypotheses
GET  /api/telos/claims                   Committed claims
GET  /api/telos/trace                    Append-only trace ledger
POST /api/telos/run                      Run the telos machinery on demand
POST /api/telos/alarms/{alarm_id}/ack    Acknowledge an alarm (silences the notification, keeps the ladder's place)
```

---

## MCP Servers

External tool servers speaking the Model Context Protocol. Config CRUD works
even while `mcp_enabled=false` (it edits `data/mcp_servers.json` directly);
live operations (connect, reload, test) need the running manager and return
`409` without it. Full guide: [mcp.md](mcp.md).

```
GET    /api/mcp/servers                  Configured servers merged with live status
                                         (state, tools, last error, server_info)
POST   /api/mcp/servers                  Add/update one server ({"name", "config"}) or
                                         import a pasted {"mcpServers": {...}} blob;
                                         connects immediately and reports per-server results
DELETE /api/mcp/servers/{name}           Disconnect, unregister its tools, delete config
POST   /api/mcp/servers/{name}/toggle    {"enabled": bool} — off unregisters tools, on reconnects
POST   /api/mcp/servers/{name}/reload    Full reconnect + tool re-discovery (re-reads config from disk)
POST   /api/mcp/test                     Dry-run connect (one server): nothing saved or
                                         registered; returns server_info + tool names, or the error
```

Entries use the ecosystem-standard `mcpServers` shape (Claude Code / Cursor /
VS Code configs paste verbatim); `${VAR}` placeholders in `headers`/`env`
expand from `.env` at connect time, and values that look like literal secrets
are rejected with a 400.

---

## Voice

Speech-to-text for the chat mic button — engines and their privacy labels are configured in Settings → Voice Input.

```
GET  /api/voice/status        Availability of the configured engine
POST /api/voice/transcribe    multipart audio in, {"text": ...} out, via the configured engine
```

---

## Kernel

### Kernel Status
```
GET /api/kernel/status
```
Returns whether this deployment has session kernels enabled plus live counts: `{"enabled": ..., "kernels": ..., "alive": ..., "max": ...}`. See [internals/autonomy.md](internals/autonomy.md).

---

## Push Notifications

Pernix supports Web Push notifications via the PWA service worker.

### Get VAPID Public Key
```
GET /api/push/vapid-public-key
```
Returns the VAPID public key needed by the service worker to create a push subscription.

### Register a Subscription
```
POST /api/push/subscribe
Content-Type: application/json

{
  "endpoint": "https://fcm.googleapis.com/...",
  "keys": { "p256dh": "...", "auth": "..." }
}
```
Registers a Web Push subscription. Pernix will send a push notification to this subscription when the agent uses `ask_user` to pause and wait for input.

### Remove a Subscription
```
DELETE /api/push/subscribe
Content-Type: application/json

{ "endpoint": "https://fcm.googleapis.com/..." }
```

---

## User Questions (ask_user)

When the agent needs input mid-turn (the `ask_user` tool), it emits a `dialog.question` SSE event and pauses. The UI displays a dialog; API clients should answer programmatically.

### List Pending Questions
```
GET /api/questions
```
Returns all open questions across all sessions.

### Answer a Question
```
POST /api/questions/{question_id}/answer
Content-Type: application/json

{ "answer": "Yes, proceed with the deletion." }
```
The agent resumes immediately after receiving the answer.

### Dismiss a Question
```
POST /api/questions/{question_id}/dismiss
```
Marks the question as dismissed without providing an answer (the agent receives an empty/dismiss signal).

### Notifications
```
GET    /api/notifications                       List unread agent notifications
POST   /api/notifications/{id}/dismiss          Mark a notification dismissed
GET    /api/notifications/events                SSE stream of notification events
POST   /api/notify                              Trigger a manual notification
```

---

## Complete Polling Example

Here is a minimal polling example using `curl`:

```bash
# 1. Create a session
SESSION=$(curl -s -X POST http://localhost:8090/api/sessions \
  -H "Content-Type: application/json" \
  -d '{"title":"API test"}' | jq -r '.session_id')

echo "Session: $SESSION"

# 2. Send a message
curl -s -X POST http://localhost:8090/api/chat \
  -H "Content-Type: application/json" \
  -d "{\"session_id\":\"$SESSION\",\"message\":\"What is 2+2?\"}"

# 3. Poll status until the turn completes.
#    Use `state` (the 10-value enum) — the legacy `compat_status` maps
#    awaiting_user/awaiting_workers to "idle" too, which can end a naive
#    loop while the session is parked on a question rather than finished.
while true; do
  STATE=$(curl -s "http://localhost:8090/api/sessions/$SESSION/status" | jq -r '.state')
  echo "State: $STATE"
  case "$STATE" in idle_ready|awaiting_user) break;; esac
  sleep 1
done

# 4. Read the response
curl -s "http://localhost:8090/api/sessions/$SESSION" | jq '.messages[-1].content'
```

For real-time streaming, connect to the SSE endpoint (`/api/sessions/{id}/events`) before sending the message and process `stream.token` events as they arrive.
