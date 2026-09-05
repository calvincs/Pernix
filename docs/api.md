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

Retrieve your token from Settings → Environment & network → Remote Access (**Show Token**) or from `GET /api/settings/auth-token`. Rotate it with `POST /api/settings/auth-token/regenerate`. In network mode both require a valid Bearer token like every other endpoint — the old localhost-only restriction on them was deliberately removed.

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
GET /api/sessions?limit=50&offset=0&archived=false&exclude_types=canary,worker
```
One page of sessions, most recent first. Alongside `items`/`count`/`spaces` the response carries `total` (every session in the population being listed) and `has_more` (`offset + limit < total`) — the pair is what lets a client offer the page *behind* the one it is showing instead of stopping at a recency horizon. `has_more` is measured against the requested window rather than the rows returned, because space sessions are unioned back in past that horizon and can make a page longer than `limit`.

**Archived sessions are absent by default** — leaving this list is what archiving *is*. `?archived=1` returns the same shape over that set instead, and `total`/`has_more` then count only it. Both answers also carry `archived_count` (how many sessions are archived in total) so a client can offer "Archived (N)" without a second round trip, and `archived` (which population it just returned). The archived page does **not** union space sessions back in: an archived session has left its space group.

**`exclude_types`** is a comma-separated list of session types to leave out of the page entirely: `normal`, `worker`, `cron`, `rlm`, `snooze`, `canary`. Unknown names are ignored rather than rejected, and the applied list comes back as `excluded_types`. The filter is applied in SQL *before* the `LIMIT` — including in the never-roll-off space union — so the page **refills** with what is left rather than merely getting shorter, and `total`/`has_more` count the same narrowed population. This is what keeps a machine-heavy instance usable: where the 500 most recently updated sessions are mostly canary self-checks, workers and cron runs, excluding those types is the difference between a third of the user's chats fitting on page one and all of them. It works on `?archived=1` too.

Both answers also carry **`type_counts`** — how many *live* sessions wear each type, over the whole unfiltered, unarchived population, e.g. `{"normal": 310, "worker": 47, "cron": 33, "rlm": 23, "snooze": 14, "canary": 277}`. It is deliberately *not* narrowed by `exclude_types`: a client offering the filter has to keep naming what it is hiding, or the control that turns a type back on reads zero. The six known types are always present; a type this build does not recognise is reported under its own name.

### Get Session (with messages)
```
GET /api/sessions/{session_id}?limit=200&before_id=<message_id>
```
Returns the full session object including its messages, oldest first. The row carries `archived_at` (`null` when live) alongside `read_only` and `read_only_reason` — and this is the only lookup that finds a session the list no longer contains, which is exactly what an archived one is. Pass `?limit=N` to get only the newest N plus `total_messages` (the session's whole count) and `has_more`.

`before_id` pages further back: the newest `limit` messages **older** than that message id, and nothing the client already holds — which is what makes "load earlier" a prepend rather than a re-render of the whole transcript. A `before_id` with no `limit` gets the default page size (200) instead of the whole transcript. `has_more` is computed from the oldest row returned, so it stays correct on the first page and every page after it.

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

### Get Turns

```
GET /api/sessions/{session_id}/turns?before_turn=<turn_id>&limit=20
```

One record per turn, newest first, holding everything the agent produced inside it. This is the read model behind the State timeline: the same join the modal used to do in the browser after downloading the whole state log *and* the whole transcript — every tool result included — just to draw a header saying "13 tools, 4 errors". Nothing new is captured; every field is read back from `session_state_log`, `messages` and `token_usage`.

`limit` is clamped to 1–100. `before_turn` pages backward (turns older than that turn id); `has_more` says whether an older page exists. 404 on an unknown session; a known session with nothing logged yet returns an empty page.

```json
{ "session_id": "8af5b75db1b1", "count": 10, "has_more": true, "turns": [ {
    "turn_id": 17, "parent_turn_id": null, "retry_index": 0, "running": false,
    "started_at": "…", "ended_at": "…|null", "elapsed_ms": 115684,
    "termination_reason": "complete|null",
    "reflect_count": 0, "eval_count": 0, "compaction_count": 0,
    "phases":  [ {"state","started_at","ended_at|null","elapsed_ms","reason_in","reason_out|null"} ],
    "tool_calls": [ {"message_id","call_id","name","args_summary","latency_ms","was_error","started_at"} ],
    "scout":  {"approach","tools","tool_rationale","memory","model","scout_model",
               "latency_ms","from_cache","from_fallback","reused_prior", …} | null,
    "reflect": [ {"attempt","verdict","reasoning","diagnostic","what_worked"} ],
    "eval":    [ {"attempt","gates":[{"name","command","passed","exit_code","output_tail"}]} ],
    "compactions": [ {"summary","compacted_up_to","original_count","at"} ],
    "notices": [ {"text","at"} ],
    "tokens": {"prompt","completion","total","calls","cost_estimate","models"},
    "model": "…|null",
    "invariant_violations": []
} ] }
```

**A turn** runs from the transition that opened it — `prompt-arrived`, or `answer-received` / `workers-complete` after a question or a worker wait, which also set `parent_turn_id` — to the transition that parks the machine again. `running` is true only for the session's newest turn while it has no closing transition; an older turn without one was abandoned by a crash and says so in `invariant_violations` (`"turn-never-closed"`) rather than ticking forever.

**Phases** are the states the turn passed through, in order, with the wall-clock time spent in each. Retries repeat them (`scouting → processing → finalizing → scouting → …`) and a compaction round trip shows as its own `compacting` phase. They sum exactly to `elapsed_ms`, which the old client-side header did not: it added up every log row's `elapsed_ms` including the opening row's, which measures how long the session sat *idle before the prompt* — on one real turn that was 71 minutes of idle reported as turn time.

**`was_error`** is the executor's own verdict, stamped on the tool row (`metadata.was_error`): the call raised, timed out, returned nothing, or returned an `Error:`. For rows written before that stamp existed it falls back to the transcript's old heuristic. It is deliberately narrower than that heuristic, which also flagged any result merely *containing* a traceback — 336 rows on the owner's box, nearly all successful `bash` calls whose script printed one.

**`tokens.cost_estimate`** is null, not `0`, when no usage row priced itself — an unpriced local model has no cost, which is not the same as a free one. **`model`** is the model most assistant rows in the turn recorded; null for turns saved before assistant rows carried it.

**Messages are joined by time window**, not by `metadata.parent_user_msg_id`: that stamp names the turn root a row was written under, but `current_turn_user_msg_id` survives turns that never refresh it — on session `e058985e52df` one user message is stamped as the parent of four different turns — so keying on it collapses their work together. In the gap between two turns the discriminator is role: a `user` row there is the prompt that opened the next turn, everything else is the previous turn's post-hook tail.

Malformed JSON in a message never fails the request. A scout, reflect or eval body that will not parse comes back as `{"raw": "<head of the content>"}`; a compaction summary that is prose rather than the usual fenced JSON block comes back as that text.

`/state-log` and the message endpoints are unchanged — this is an additional view over the same rows.

### Search Sessions
```
GET /api/sessions/search?q=<query>&limit=20
```
FTS5 full-text search across all sessions' message content. Archived sessions are deliberately still findable here — staying searchable is the promise archiving makes — so each hit carries `archived: true|false` alongside `title`, `session_type` and `space_id`.

### Update Session Metadata
```
PATCH /api/sessions/{session_id}
```
```json
{ "title": "New title", "pinned": true, "space_id": null, "archived": false, "model_override": "qwen3:32b" }
```
Any subset of the five keys; absent keys are left unchanged.

`archived: true` stamps `archived_at` with now, `false` clears it. An archived session leaves the session list and its space group, keeps every message, stays searchable, and opens read-only (`read_only_reason` becomes *"Archived — restore it to continue"*). Deleting stays a separate, explicit act.

**Nothing here bumps `updated_at`.** Recency ordering is what the sidebar's time buckets and the idle horizon are computed from, so archiving must not reshuffle the list and restoring must put a session back exactly where it was. Archiving and restoring also emit a `session.archived` SSE event on that session's stream, carrying `archived`, `archived_at`, `read_only` and `read_only_reason`.

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

### Archive Idle Sessions
```
POST /api/sessions/archive-idle
```
```json
{ "days": 30, "space_id": null, "dry_run": false }
```
Archives ordinary chats idle for more than `days` — or, with `dry_run`, says what it would. All three keys are optional: `days` defaults to `session_archive_idle_days` and must be a non-negative integer (anything else is a `400`); `space_id` narrows the sweep to one space (a `404` if it does not exist); omit it to sweep the whole table.

A candidate is `session_type` `normal`, not already archived, not pinned, last updated before the cutoff. Space sessions **are** included: the v33 rule that spares them from every DELETE sweep is about never losing a transcript, and nothing here deletes one.

A dry run computes exactly the same set as the real run, so the count in a confirmation dialog is a promise this endpoint keeps.

```json
{
  "count": 74,
  "ids": ["a1b2c3d4e5f6", "..."],
  "sample": [
    { "id": "a1b2c3d4e5f6", "title": "Old chat", "updated_at": "2026-07-01T09:12:44+00:00", "space_id": null }
  ],
  "days": 30,
  "dry_run": false
}
```
`sample` is the first ten. Sessions archived for longer than `session_delete_archived_days` are hard-deleted by a snooze sweep; that knob is `0` (never) by default.

### Purge Old Sessions
```
POST /api/sessions/purge
```
```json
{ "keep_days": 7, "keep_min": 5, "dry_run": false }
```
Bulk-deletes stale ordinary sessions. All three keys are optional and shown with their defaults; `keep_days` and `keep_min` must be non-negative integers (anything else is a `400`). `keep_days: 0` means "everything already idle".

A **candidate** is a session that is all of: `session_type` `normal`, not pinned, not in a space, and last updated before the cutoff. The scan covers the whole table, not a recency window. The newest `keep_min` candidates are always kept; the rest are deleted (or, with `dry_run: true`, only counted).

Sessions older than the cutoff that are *not* candidates are counted under `skipped`, each one under the first rule that spared it — `other_types`, then `pinned`, then `in_space`. Typed sessions (canary, worker, cron, rlm, snooze) are never purged here; each has its own retention horizon.

Response (identical shape in both modes):
```json
{
  "dry_run": false,
  "keep_days": 7,
  "keep_min": 5,
  "cutoff": "2026-08-26T12:00:00+00:00",
  "candidates": 312,
  "would_delete": 307,
  "purged": 307,
  "sample": [
    { "id": "a1b2c3d4e5f6", "title": "Old chat", "updated_at": "2026-07-01T09:12:44+00:00", "message_count": 18 }
  ],
  "skipped": { "pinned": 4, "in_space": 11, "other_types": 96 }
}
```

| Key | Meaning |
|---|---|
| `dry_run` | Echo of the request — `true` means nothing was deleted |
| `keep_days` / `keep_min` | The validated values actually applied |
| `cutoff` | ISO-8601 UTC timestamp; a session is stale when `updated_at` is before it |
| `candidates` | How many sessions matched the candidate rules |
| `would_delete` | `candidates` minus the `keep_min` newest — the set the real run acts on |
| `purged` | How many were deleted; always `0` when `dry_run` is `true` |
| `sample` | The first 10 of the delete set (`id`, `title`, `updated_at`, `message_count`), newest first — enough to show a user before they commit |
| `skipped` | Counts of the older-than-cutoff sessions each rule spared |

A dry run and the real run compute the same set from the same query, so `would_delete` is a promise the real run keeps.

---

## Spaces

Named, colored groups of long-lived sessions that share directives, memory, workspace and a kernel — see [guides/spaces.md](guides/spaces.md).

### List Spaces
```
GET /api/spaces
```
Returns `{"items": [...]}`; each space carries `id`, `slug`, `label`, `color`, `sort_order`, `created_at`, `updated_at` and `session_count` (live — unarchived — member sessions only).

### Create a Space
```
POST /api/spaces
```
```json
{ "label": "Research", "color": "#7c9cff" }
```
`label` is required (max 120 chars, `400` if empty); `color` must be `#rrggbb` or is defaulted. The slug is derived from the label and is immutable — it names the memory-file prefix, the directive directory and the workspace home. `409` if the derived slug collides with an existing space.

### Update a Space
```
PATCH /api/spaces/{space_id}
```
```json
{ "label": "New name", "color": "#22c55e", "sort_order": 2 }
```
Any subset of the three keys; the slug never changes. Returns the updated space row.

### Delete a Space
```
DELETE /api/spaces/{space_id}?cascade=false
```
`cascade=false` (default) **detaches**: member sessions return to the ordinary list, memory files and the workspace folder stay, bound jobs unbind. `cascade=true` **deletes** every member session plus the space's memory files, workspace folder and bound jobs. Either way the directive overrides and the shared kernel go with the space — they are configuration, not user artifacts. Returns `{"space_id", "cascade", ...}` with `sessions_detached`/`jobs_unbound` or `sessions_deleted`/`memory_files_deleted`/`jobs_removed` depending on the mode.

### Directive Overrides
```
GET    /api/spaces/{space_id}/directives
PUT    /api/spaces/{space_id}/directives/{name}     { "content": "<full markdown>" }
DELETE /api/spaces/{space_id}/directives/{name}
```
`name` is `SOUL`, `RULES` or `SESSIONS`. GET returns `{"space_id", "files": {"SOUL": {"default": "...", "override": "...|null"}, "RULES": {...}, "SESSIONS": {...}}}` — the shared default text next to this space's override, if any. PUT writes an override (`content` non-empty, max 64,000 bytes); DELETE removes the override so the space reverts to the default. Undefined files fall back to the shared default; the compiler and scout both resolve through `core.spaces.directive_path`.

---

## Space Suggestions

Off by default (`space_suggest_enabled`; Settings → Autonomy & idle work → **Space suggestions**). At idle, a background-model scan groups recent ordinary chats by the kind of work they are; a code gate keeps only the substantial groupings as pending suggestions the user accepts or declines — nothing is created or moved on its own. Guide: [guides/spaces.md](guides/spaces.md).

### List Suggestions
```
GET /api/space-suggestions?status=pending
```
`status` is `pending` (default) | `accepted` | `rejected` | `expired` | `all`; anything else is `400`. Returns `{"suggestions": [...], "status"}`. Each row carries `id`, `kind` (`new` | `existing`), `topic_key`, `label`, `color`, `why`, `existing_space_id`, `session_ids`, `directives` (drafted additions keyed by `SOUL`/`RULES`/`SESSIONS`, each `{"addition", "rationale"}`, or `null`), `status`, `space_id`, `created_at`, `resolved_at` — plus resolved `sessions` (`id`/`title`/`subtitle`/`updated_at`/`space_id`; a member whose session was since deleted just drops out) and `existing_space` (`id`/`label`/`color`, or `null` for a `new`-kind suggestion).

### Get One Suggestion
```
GET /api/space-suggestions/{suggestion_id}
```
Same shape as a list row, with each entry in `directives` further enriched with the shared `default` file text alongside the drafted `addition` — what the review sheet needs to show both. `404` if unknown.

### Scan Now
```
POST /api/space-suggestions/scan
```
```json
{ "dry_run": true }
```
Runs a scan immediately, outside its normal cadence. `dry_run` (default `true`) proposes without storing anything — this is what the settings pane's **Scan now** preview calls. Returns `{"scanned", "proposed", "kept", "dry_run"}` on a normal run, or `{"skipped": "<reason>"}` / `{"error": "..."}` when the scan declines to run or the model call fails. `409` if a scan is already in progress.

### Accept
```
POST /api/space-suggestions/{suggestion_id}/accept
```
```json
{ "directives": { "SOUL": "<full file content to write>" }, "session_ids": ["..."] }
```
Creates the space (kind `new`; `label`/`color` may override the draft) or targets the existing one (kind `existing`) — `409` if that space was deleted since the scan (the suggestion is expired instead of retried). `directives` here is the **full content** to write per file (what the sheet computed from default + addition, possibly edited), not the drafted `{addition, rationale}` shape the GET returns — and it is only ever written for a brand-new space, so an existing space's own overrides are never silently replaced. `session_ids` narrows which members to move (default: all of them); moving uses `set_session_meta`, not the ordinary update path, so accepting never bumps a chat's recency. A member move that fails is reported, not rolled back. Returns `{"status": "accepted", "space", "moved", "failed"}`. `404` unknown suggestion, `409` if not pending.

### Reject
```
POST /api/space-suggestions/{suggestion_id}/reject
```
Declines. The row stays — its `topic_key` is what suppresses the same grouping from being proposed again until it is cleared. Returns `{"status": "rejected"}`. `404`/`409` as above.

### Clear / Delete
```
DELETE /api/space-suggestions?status=rejected
DELETE /api/space-suggestions/{suggestion_id}
```
The bulk form clears every suggestion in one terminal status (`accepted` | `rejected` | `expired`; `pending` is refused with `400` — accept or decline instead). The single form forgets one row whatever its status, and re-arms a declined topic so the scan may propose it again. `404` if the single id is unknown.

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
Returns `{"name", "content", "mtime"}` as JSON. `mtime` is the markdown file's modification time — hand it straight back as `base_mtime` on the next save to get conflict detection.

### Write a Memory File
```
PUT /api/memory/files/{filename}
Content-Type: application/json

{ "content": "<full markdown>", "base_mtime": 1756800000.123456 }
```
Replaces the file's markdown and re-indexes it, so search stops matching text the file no longer contains. `content` is required and must be a string (max 5 MB, else 413); the file must already exist (404 otherwise).

`base_mtime` is optional. Send the value the GET returned and a save that would overwrite someone else's change — the agent, a maintenance sweep — is refused with `409 {"detail": "changed_on_disk", "mtime": <current>}` instead of silently winning. Omit it and the write is last-writer-wins, which is what every non-editor caller gets. Returns `{"saved": true, "name", "bytes", "mtime"}`; the returned `mtime` is the next save's `base_mtime`.

### Search Memory
```
GET /api/memory/search?q=your+query&limit=10&offset=0&after=<epoch>&space=<slug>
```
Full-text search across all memory entries — BM25, or hybrid BM25 + vector when `embedding_model` is set. `limit` defaults to 10 and is clamped to 100; `offset` pages the ranked results; `after` filters to entries newer than the given epoch; `space` (a slug) prioritizes that space's `pernix.space.<slug>.*` files.

Returns `results` (entries with relevance scores) plus `offset`, `limit`, `returned` (rows in this page) and `has_more` (whether a further page exists). There is deliberately no exact `total`: the hybrid ranker fuses two result sets and stops at a scan cap, so any count would be contradicted by the next query. The flag is the honest answer.

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

The response carries an **`X-File-Mtime`** header — the file's modification time in seconds to six decimal places, exposed to browsers via `Access-Control-Expose-Headers`. An editor keeps that value and hands it back as `base_mtime` on save; that is the whole conflict-detection contract.

### Write a File
```
PUT /workspace/{path}
Content-Type: application/json

{ "content": "<file contents>", "base_mtime": 1756800000.123456 }
```
The body is JSON with a `content` field (a raw text body is a 422). Creates parent directories automatically. Returns `{"saved": true, "path", "bytes", "mtime"}`.

`base_mtime` is optional and opt-in: send the `X-File-Mtime` value from the GET and a write whose base is stale returns `409 {"detail": "changed_on_disk", "mtime": <current>}` — the file is left alone and the client can diff or reload. Omit it and the write is last-writer-wins, so agent tools, `curl`, and older clients are unaffected. The same contract, byte for byte, backs `PUT /api/memory/files/{name}` and `PUT /api/skills/{name}`.

### Delete a File
```
DELETE /workspace/{path}
```

### Upload a File
```
POST /api/upload
Content-Type: multipart/form-data

file=@localfile.pdf
path=reports/2026          # optional
```
Max 250MB. `path` is an optional form field naming a directory relative to the workspace root — the folder the Explorer is currently showing. It is created if missing and goes through the same traversal check as every other workspace route; omitted, the upload lands at the root. Filenames are sanitized. Blocked extensions: `.exe`, `.sh`, `.php`, `.bat`, `.dll`, `.msi`, `.scr`, `.cmd`, `.com`. Returns the saved path.

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
Returns `{"status": "healthy", ...}` with the release version, build id, current primary model, maintenance stats, and two session counts:

| Key | Meaning |
|---|---|
| `sessions_active` | Sessions doing work right now — a turn running, scouting, compacting, finalizing, or holding a background task. Canary sessions are never counted. |
| `sessions_loaded` | Sessions held in memory, busy or not. A session stays loaded for about 30 minutes after its last turn before the reaper drops it, so this is a memory figure, not a load figure. |

`sessions_active` is always ≤ `sessions_loaded`. Watch the first for load and the second for footprint; a large gap just means recent conversations have not been reaped yet.

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

### Settings Schema
```
GET /api/settings/schema
```
The machine-readable half of the settings surface: every non-redacted settings field with the type, default, and bounds the settings API actually enforces, so a client never has to hardcode them. Returns `{"fields": {<key>: <record>}, "count": N}` with one record per key:

| Key | Meaning |
|---|---|
| `key` | The setting name (same as the map key) |
| `type` | `bool`, `int`, `float`, `str`, `list`, or `dict` — derived from the declared default |
| `default` | The shipped default value |
| `min` / `max` | The bounds `POST /api/settings` clamps to, or `null` when the field is unbounded |
| `step` | Suggested increment: `1` for ints, `0.05` for floats bounded to 0–1, `0.01` for other floats, `null` otherwise |
| `unit` | Display unit inferred from the key (`seconds`, `minutes`, `hours`, `days`, …), or `null` |
| `restart` | `true` when changing the field requires a server restart |
| `locked` | `true` when the field cannot be changed through the API at all |
| `risk` / `hint` | Always `null` — the client owns this copy; the fields exist so the merged record has one shape |

Redacted fields (API keys and other secrets) are absent entirely.

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

## Storage

One ledger for "why is this box full?": sessions by type, the database file's size and reclaimable space, both backup directories, and the retention sweeps that already run in the background. Backs Settings → Storage.

### Get the Ledger
```
GET /api/storage
```
```json
{
  "sessions": { "total": 1032, "by_type": {"normal": 310, "worker": 47, "...": 0}, "pinned": 12, "in_spaces": 84, "archived": 41 },
  "database": { "path": "/app/data/sessions.db", "bytes": 176160768, "wal_bytes": 0, "page_size": 4096, "reclaimable_bytes": 8912896 },
  "backups": { "dir": "data/backups", "count": 7, "bytes": 734003200, "keep": 7, "last_backup_at": "2026-09-02T18:37:03Z", "beyond_keep": [] },
  "legacy_backups": { "dir": "data/.backups", "...": "same shape as backups, or null if this instance never had one" },
  "sweeps": { "sessions_pruned": 307, "sessions_archived": 41, "...": 0, "last_cycle": "2026-09-02T22:00:00Z" }
}
```
`sessions.archived` is `null` on a pre-v34 database rather than `0` — a build that cannot archive is not the same fact as zero archived sessions. `database.reclaimable_bytes` is the SQLite freelist (`freelist_count * page_size`) — what a `POST /api/storage/optimize` would give back. `backups`/`legacy_backups.bytes` is the whole directory (snapshots plus any memory corpora beside them); `beyond_keep` lists the snapshots rotation would remove, each with `name`, `bytes`, `mtime`, `scheme`. `legacy_backups` is `null` on an instance that never wrote to the pre-rename `data/.backups` directory. `sweeps` is present only when the snooze runner has stats to report — every `*_pruned` counter it tracks, plus `sessions_archived` and `last_cycle`.

### Rotate Backups
```
POST /api/storage/backups/rotate
```
```json
{ "dir": "primary", "dry_run": true }
```
Applies the retention count (`backup_keep_count`) to every snapshot in one directory, under every naming scheme it was ever written with. `dir` is `primary` (`data/backups`, the one the schedule writes) or `legacy` (`data/.backups`, which deploys still write to and nothing used to rotate) — each keeps its own newest `keep`, not a shared budget. `400` on an unknown `dir`; `404` if `dir: legacy` is asked for on an instance with no legacy directory. Defaults to a dry run.

### Prune Archived Sessions
```
POST /api/storage/prune-archived
```
```json
{ "days": 90, "dry_run": true }
```
Hard-deletes sessions that have been archived for more than `days`. `days` defaults to `session_delete_archived_days` (`0` = never, so an omitted body normally does nothing). Defaults to a dry run — past this point the transcript is actually gone. Returns `{"count", "ids", "sample", "days", "dry_run"}`, the same shape `POST /api/sessions/archive-idle` uses.

### Compact the Database
```
POST /api/storage/optimize
```
`PRAGMA optimize`, then `VACUUM`, then a `wal_checkpoint(TRUNCATE)` so the freed pages actually leave the file on disk instead of waiting for the next checkpoint. Refused with `409` while any turn is running — `VACUUM` holds a write lock for the whole rebuild. Returns `{"bytes_before", "bytes_after"}`.

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
Returns the skill's metadata, rendered `instructions`, the raw `raw_content` of its SKILL.md, its `resources`, and `mtime` — the value to send back as `base_mtime` when saving.

### Skill Improvement Proposals

Written by reflect and refine when a skill visibly under-performs. A pending
proposal reaches `SKILL.md` one of two ways: you approve-then-apply it
yourself, or — past `skill_proposal_auto_apply_after_hours` (default 24; `0`
disables) — a snooze sweep applies it on its own once it passes machine
validation (skill exists and is enabled, change ≤ 4,000 chars, confidence ≥
0.6), day-capped (`skill_proposal_max_auto_applies_per_day`, default 5), with
a timestamped backup under `data/skill_backups/<skill>/` and status stamped
`auto_applied` — reject it before the window closes to veto.

```
GET    /api/skills/proposals                 List proposals (default status=pending)
POST   /api/skills/proposals/{id}/approve    Mark approved (you edit the skill yourself)
POST   /api/skills/proposals/{id}/reject     Dismiss
POST   /api/skills/proposals/{id}/apply      Write the change into the target SKILL.md
```

Filter with `?skill_name=`, `?status=` (`pending` | `approved` | `rejected` |
`applied` | `auto_applied`), `?source_origin=` (`session` for post-turn
reflect, `refine` for the authoring pass).

> These lived under `/api/workflows/proposals` before the workflow engine was
> removed in 2026-08. Update any saved calls.

### Update a Skill
```
PUT /api/skills/{name}
Content-Type: application/json

{ "content": "<full SKILL.md content>", "base_mtime": 1756800000.123456 }
```
Writes `content` verbatim to the skill's `SKILL.md` and rescans the registry. `base_mtime` is optional and behaves exactly as it does on `PUT /workspace/{path}`: send the `mtime` from `GET /api/skills/{name}` and a stale save is refused with `409 {"detail": "changed_on_disk", "mtime": <current>}`; omit it for last-writer-wins. Returns `{"ok": true, "mtime": <new>}`.

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
GET /api/rlm/runs?session_id=&limit=20&space_id=  List runs, newest first (limit clamped to 100).
                                              `space_id` returns every member session's runs and,
                                              when given, wins over `session_id`.
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
POST   /api/jobs/{name}/run   Fire the job once, now
POST   /api/jobs/{name}/validate   Re-validate the stored spec
POST   /api/jobs/{name}/test  Dry-run the prompt once in a throwaway workspace
GET    /api/jobs/runs?limit=&offset=&job_name=   Paginated run history ({items, total, limit, offset})
DELETE /api/jobs/runs         Clear run history
GET    /api/jobs/status       Current scheduler status
GET    /api/jobs/events       SSE stream of job events
```

**Run now.** `POST /api/jobs/{name}/run` fires the job through the scheduler's own dispatch, so a manual run *is* a run: same `cron_runs` row, same `job.started` / `job.completed` events, same entry in History. It does not touch the schedule — the missed-run grid is unaffected, and a paused job can still be run this way (that is the point of a manual trigger). Returns `{"status": "run_started", "name"}` immediately; the outcome arrives on `/api/jobs/events` like every other run's.

**Validate.** `POST /api/jobs/{name}/validate` re-checks a stored job's spec — cron expression parses, prompt is non-trivial, every entry in `allowed_tools` exists (hard errors); an unknown model is a warning. The result is persisted on the job and returned as `{"name", "validation"}`, which is what drives the valid / invalid / unvalidated badges in the jobs panel. Creating or editing a job validates it automatically; this endpoint is for re-checking one whose world may have changed underneath it (a renamed tool, a removed model).

**Test.** `POST /api/jobs/{name}/test` dry-runs the prompt once in a throwaway temp workspace under the job's own model and allow-list, writes **no** `cron_runs` row, and keeps the transcript as a `Job test: <name>` session. See [guides/scheduling-cron.md](guides/scheduling-cron.md).

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
GET  /api/adaptive/entries?kind=&status=active,trial&limit=200
                                                           Entries by kind/status (+ enabled/auto_apply
                                                           flags). status takes one status or a comma-list;
                                                           empty = every status. Default is the live set.
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

Speech-to-text for the chat mic button — engines and their privacy labels are configured in Settings → Integrations → Voice Input.

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
