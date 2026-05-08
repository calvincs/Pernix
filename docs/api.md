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

Retrieve your token from the Settings UI or from `GET /api/settings/auth-token` (localhost-only). Rotate it with `POST /api/settings/auth-token/regenerate` (localhost-only).

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
Returns the full session object including all messages.

### Get Session Status
```
GET /api/sessions/{session_id}/status
```
Returns lightweight status: state, active model, current turn ID, retry index, termination reason. Useful for polling.

### Get State Log
```
GET /api/sessions/{session_id}/state-log
```
Returns the append-only state machine transition history for the session. Every state change is recorded with a timestamp, the triggering event, and optional metadata.

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
{ "keep_minimum": 10 }
```
Bulk-deletes sessions older than the threshold, keeping at least `keep_minimum` recent sessions.

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

`idempotency_key` is optional. If you provide one, a duplicate submission with the same key within the same session is silently ignored (the original is still processed). Useful for retrying a failed HTTP request without double-submitting.

### Inject a Message
```
POST /api/chat/inject
```
```json
{
  "session_id": "abc123",
  "message": "Additional context to inject",
  "role": "user"
}
```
Injects a message directly into the running context without triggering a new agent turn. Used to supply additional information mid-turn.

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
Triggers context compaction immediately (rather than waiting for `compaction_threshold` to be reached).

### Get Partial Response
```
GET /api/partial/{session_id}
```
Returns whatever streamed text has accumulated for the current turn, before the turn completes. Useful for showing in-progress responses to clients that aren't using SSE.

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

**Reconnection:** Include `Last-Event-ID: <last_seq>` on reconnect to receive any events you missed. The client should reconcile by checking for gaps in `_seq` (a monotonically increasing sequence number on every event).

### Connecting (JavaScript example)
```javascript
const evtSource = new EventSource(`/api/sessions/${sessionId}/events`);
evtSource.onmessage = (e) => {
  const event = JSON.parse(e.data);
  console.log(event.type, event);
};
```

### Event Catalog

Every event includes `_seq` (sequence number), `session_id`, and `timestamp`.

#### Session Lifecycle
| Event | Description |
|---|---|
| `session.state_changed` | State machine transition. Fields: `from_state`, `to_state`, `trigger`. |
| `session.queued` | A message was queued because the session is busy. Fields: `queue_depth`. |
| `session.queue_full` | Message rejected — queue is at `max_pending_messages`. |
| `session.message_combined` | A rapid follow-up message was merged into the running turn's DB row (rapid-fire combining within a 3-second window). Fields: `message_id`. |
| `session.prompt_rejected` | Message rejected (e.g., session is cancelling). Fields: `reason`. |

#### Agent Turn
| Event | Description |
|---|---|
| `scout.start` | Scout (planning) phase began. |
| `scout.done` | Scout completed. Fields: `approach` (the plan summary). |
| `stream.token` | One streamed text token from the agent. Fields: `content`. |
| `stream.done` | The model finished generating for this round. Fields: `finish_reason`. |
| `turn.complete` | The full agent turn is finished. Fields: `message_id`, `reflect_verdict`. |
| `stream.error` | An error occurred during generation. Fields: `error`. |

#### Tool Calls
| Event | Description |
|---|---|
| `tool.call.start` | A tool invocation is starting. Fields: `name`, `call_id`, `input` (args). |
| `tool.call.result` | A tool returned a result. Fields: `name`, `call_id`, `output`, `duration_ms`, `error` (if failed). |

#### User Interaction
| Event | Description |
|---|---|
| `ask_user` | The agent is pausing to ask the user a question. Fields: `question_id`, `question`, `choices` (optional list). Answer via `POST /api/sessions/{id}/questions/{qid}/answer`. |

#### Context Management
| Event | Description |
|---|---|
| `context.compacted` | The conversation context was pruned. Fields: `tokens_before`, `tokens_after`. |
| `context.warning` | Context usage is approaching the critical threshold. Fields: `utilization`. |

#### Workers
| Event | Description |
|---|---|
| `worker.spawned` | A worker sub-agent was created. Fields: `worker_id`, `model`. |
| `worker.done` | A worker finished successfully. Fields: `worker_id`. |
| `worker.error` | A worker encountered an error. Fields: `worker_id`, `error`. |
| `worker.cancelled` | A worker was cancelled. Fields: `worker_id`. |

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
Returns the raw markdown content of the file.

### Search Memory
```
GET /api/memory/search?q=your+query&limit=10
```
BM25 full-text search across all memory entries. Returns entries with relevance scores.

### Memory Maintenance
```
POST /api/memory/maintenance
```
```json
{ "reindex": false }
```
Runs a health check on the memory index. Set `reindex: true` to rebuild the FTS5 index from scratch (useful if search results seem stale or wrong).

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
Content-Type: text/plain (or application/json, etc.)

<file contents>
```
Creates parent directories automatically.

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
Max 10MB. Filenames are sanitized. Blocked extensions: `.exe`, `.sh`, `.php`, `.bat`, `.dll`, `.msi`, `.scr`, `.cmd`, `.com`. Returns the saved path.

### List Data Files
```
GET /api/datafiles
```
Returns a directory listing of `data/` (excluding the SQLite database, certs, and other sensitive files). Useful for inspecting the agent's persistent state.

---

## Workers

Workers are sub-agents spawned from a parent session. Pause/resume operations apply at the next round boundary (mid-tool-call work is not interrupted).

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
Returns `{"status": "ok"}` when the server is running. Includes active session count and current primary model.

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

### Auth Token *(localhost-only)*
```
GET  /api/settings/auth-token              View the current Bearer token
POST /api/settings/auth-token/regenerate   Rotate the token
```

### Access QR Code *(localhost-only, network mode)*
```
GET /api/settings/access-qr
```
Returns a QR code image encoding `https://<LAN-IP>:<port>/?token=<token>`. Scan with a phone to log in.

### Environment Variables *(localhost-only)*
```
GET /api/env-vars
```
Returns the names of detected environment variables (values redacted) — useful for verifying API keys are loaded correctly.

### Graceful Restart *(localhost-only)*
```
POST /api/admin/restart
```
Triggers a graceful server restart (uses `os.execv` to replace the process in-place). Useful after changing settings that require a restart.

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

### Update a Skill
```
PUT /api/skills/{name}
Content-Type: text/plain

<full SKILL.md content>
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

## Workflows

Workflows are reusable multi-step pipelines defined in YAML. See `data/workflows/` for examples.

```
GET    /api/workflows                       List all workflows
POST   /api/workflows                       Create a workflow
GET    /api/workflows/{name}                Read a workflow
PUT    /api/workflows/{name}                Update a workflow
DELETE /api/workflows/{name}                Delete a workflow
POST   /api/workflows/validate              Validate workflow content
GET    /api/workflows/{name}/runs           List runs of a workflow
GET    /api/workflows/{name}/runs/{run_id}  Get a specific run
DELETE /api/workflows/{name}/runs/{run_id}  Delete a run
GET    /api/workflows/proposals             List pending workflow proposals
POST   /api/workflows/proposals/{id}/approve
POST   /api/workflows/proposals/{id}/reject
POST   /api/workflows/proposals/{id}/apply
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
GET    /api/jobs/runs         History of past runs
DELETE /api/jobs/runs         Clear run history
GET    /api/jobs/status       Current scheduler status
GET    /api/jobs/events       SSE stream of job events
```

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

When the agent needs input mid-turn, it emits an `ask_user` SSE event and pauses. The UI displays a dialog; API clients should answer programmatically.

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

## Complete Workflow Example

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

# 3. Poll status until turn completes
while true; do
  STATE=$(curl -s "http://localhost:8090/api/sessions/$SESSION/status" | jq -r '.state')
  echo "State: $STATE"
  [ "$STATE" = "idle" ] && break
  sleep 1
done

# 4. Read the response
curl -s "http://localhost:8090/api/sessions/$SESSION" | jq '.messages[-1].content'
```

For real-time streaming, connect to the SSE endpoint (`/api/sessions/{id}/events`) before sending the message and process `stream.token` events as they arrive.
